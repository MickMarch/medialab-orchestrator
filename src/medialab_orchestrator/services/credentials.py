"""Credential health aggregation: the workers' per-key states plus the bot's login report.

Each worker checks the credentials it owns and reports them on its health
response; the bot has no HTTP surface and posts its login result here. This
module merges the maps for ``GET /health``, remembers the last seen status of
each credential on disk, and posts one Discord notice per transition into or
out of ``invalid`` so a dead key is announced once, not once per poll.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from medialab_contracts import (
    CREDENTIAL_DISCORD_TOKEN,
    CREDENTIAL_JELLYFIN_API_KEY,
    CREDENTIAL_NAMES,
    CREDENTIAL_QB_API_KEY,
    CREDENTIAL_TMDB_API_KEY,
    CredentialState,
    CredentialStatus,
)
from pydantic import ValidationError

from medialab_orchestrator.clients import JellyfinClient, TorrentDownloaderClient
from medialab_orchestrator.core.config import config
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.services.notify import post_discord

HEALTH_CREDENTIALS_KEY = "credentials"
DOWNLOADER_CREDENTIALS: tuple[str, ...] = (CREDENTIAL_TMDB_API_KEY, CREDENTIAL_QB_API_KEY)
JELLYFIN_CREDENTIALS: tuple[str, ...] = (CREDENTIAL_JELLYFIN_API_KEY,)
REPORTABLE_CREDENTIALS: frozenset[str] = frozenset({CREDENTIAL_DISCORD_TOKEN})
"""Names a client may POST a state for; everything else comes from a worker."""
PAUSED_RECHECK_SECONDS = 60.0
LEDGER_LAST_SEEN_KEY = "last_seen"
LEDGER_BOT_REPORT_KEY = "bot_report"
UNREACHABLE_DETAIL = "worker unreachable"

Notifier = Callable[[str], Awaitable[None]]


def invalid_notice(name: str, detail: str) -> str:
    suffix = f" ({detail})" if detail else ""
    return (
        f"Credential problem: {name} was rejected by its service{suffix}. "
        f"Run setup.cmd --fix {name}."
    )


def restored_notice(name: str) -> str:
    return f"Credential restored: {name} works again."


class CredentialLedger:
    """Last seen status per credential and the bot's last report, as one JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._last_seen: dict[str, str] = {}
        self._bot_report: CredentialState | None = None
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._last_seen = dict(data.get(LEDGER_LAST_SEEN_KEY, {}))
            report = data.get(LEDGER_BOT_REPORT_KEY)
            self._bot_report = CredentialState.model_validate(report) if report else None
        except (json.JSONDecodeError, ValidationError, OSError) as error:
            app_logger.warning("Credential ledger unreadable, starting fresh: %s", error)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            LEDGER_LAST_SEEN_KEY: self._last_seen,
            LEDGER_BOT_REPORT_KEY: (
                self._bot_report.model_dump(mode="json") if self._bot_report else None
            ),
        }
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @property
    def last_seen(self) -> dict[str, str]:
        return dict(self._last_seen)

    @property
    def bot_report(self) -> CredentialState | None:
        return self._bot_report

    def remember(self, states: dict[str, CredentialState]) -> None:
        self._last_seen = {name: state.status.value for name, state in states.items()}
        self._save()

    def record_bot_report(self, state: CredentialState) -> None:
        self._bot_report = state
        self._save()


def _states_from_health(body: Any, owned: tuple[str, ...]) -> dict[str, CredentialState]:
    """A worker's credential map, or unreachable/unknown placeholders for what it owns."""
    if body is None:
        return {
            name: CredentialState(status=CredentialStatus.UNREACHABLE, detail=UNREACHABLE_DETAIL)
            for name in owned
        }
    raw = body.get(HEALTH_CREDENTIALS_KEY) if isinstance(body, dict) else None
    states: dict[str, CredentialState] = {}
    for name in owned:
        try:
            states[name] = (
                CredentialState.model_validate(raw[name])
                if raw and name in raw
                else CredentialState()
            )
        except ValidationError:
            states[name] = CredentialState()
    return states


class CredentialMonitor:
    def __init__(
        self,
        *,
        torrent_client: TorrentDownloaderClient,
        jellyfin_client: JellyfinClient,
        ledger: CredentialLedger,
        notifier: Notifier = post_discord,  # type: ignore[assignment]
    ) -> None:
        self._torrent = torrent_client
        self._jellyfin = jellyfin_client
        self._ledger = ledger
        self._notifier = notifier

    async def aggregate(
        self, torrent_health: Any = None, jellyfin_health: Any = None, *, fetch: bool = True
    ) -> dict[str, CredentialState]:
        """Every credential's state, in display order. Pass prefetched health bodies to
        avoid a second round trip; with ``fetch`` the monitor asks the workers itself."""
        if fetch:
            torrent_health = await self._torrent.health()
            jellyfin_health = await self._jellyfin.health()
        states = {
            **_states_from_health(torrent_health, DOWNLOADER_CREDENTIALS),
            **_states_from_health(jellyfin_health, JELLYFIN_CREDENTIALS),
            CREDENTIAL_DISCORD_TOKEN: self._ledger.bot_report or CredentialState(),
        }
        return {name: states[name] for name in CREDENTIAL_NAMES if name in states}

    def record_bot_report(self, state: CredentialState) -> None:
        self._ledger.record_bot_report(state)

    async def notify_transitions(self, states: dict[str, CredentialState]) -> list[str]:
        """Post one notice per credential that entered or left ``invalid`` since last seen.
        Returns the notices posted. Remembers the new states either way."""
        previous = self._ledger.last_seen
        notices: list[str] = []
        for name, state in states.items():
            was_invalid = previous.get(name) == CredentialStatus.INVALID.value
            is_invalid = state.status is CredentialStatus.INVALID
            if is_invalid and not was_invalid:
                notices.append(invalid_notice(name, state.detail))
            elif was_invalid and not is_invalid and state.status is CredentialStatus.OK:
                notices.append(restored_notice(name))
        self._ledger.remember(states)
        webhook = config.discord_notify_webhook_url
        if webhook:
            for notice in notices:
                await self._notifier(webhook, notice)  # type: ignore[call-arg]
        return notices

    async def tick(self) -> dict[str, CredentialState]:
        states = await self.aggregate()
        await self.notify_transitions(states)
        return states

    async def run(self) -> None:
        """Tick on the health poll cadence; 0 pauses, rechecked every minute."""
        while True:
            interval = float(config.health_poll_interval_seconds)
            if interval <= 0:
                await asyncio.sleep(PAUSED_RECHECK_SECONDS)
                continue
            await asyncio.sleep(interval)
            try:
                await self.tick()
            except Exception as error:  # noqa: BLE001 - the loop must survive any single tick
                app_logger.warning("Credential tick failed: %s", error)
