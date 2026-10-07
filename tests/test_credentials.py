"""Credential health aggregation, transition notices, the ledger, and the report route."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from medialab_contracts import (
    CREDENTIAL_DISCORD_TOKEN,
    CREDENTIAL_JELLYFIN_API_KEY,
    CREDENTIAL_NAMES,
    CREDENTIAL_QB_API_KEY,
    CREDENTIAL_TMDB_API_KEY,
    CredentialState,
    CredentialStatus,
)

from medialab_orchestrator.core.deps import AppContext
from medialab_orchestrator.services import credentials as module
from medialab_orchestrator.services.credentials import (
    CredentialLedger,
    CredentialMonitor,
    invalid_notice,
    restored_notice,
)

WEBHOOK = "https://discord.test/hook"


def _health(**states: str) -> dict:
    return {"status": "online", "credentials": {k: {"status": v} for k, v in states.items()}}


@pytest.fixture
def ledger(tmp_path: Path) -> CredentialLedger:
    return CredentialLedger(tmp_path / "credentials.json")


@pytest.fixture
def notices() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def monitor(
    torrent_client: AsyncMock,
    jellyfin_client: AsyncMock,
    ledger: CredentialLedger,
    notices: list[tuple[str, str]],
) -> CredentialMonitor:
    async def notifier(url: str, content: str) -> None:
        notices.append((url, content))

    return CredentialMonitor(
        torrent_client=torrent_client,
        jellyfin_client=jellyfin_client,
        ledger=ledger,
        notifier=notifier,
    )


class TestAggregate:
    def test_merges_worker_maps_in_display_order(
        self, monitor: CredentialMonitor, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ) -> None:
        torrent_client.health.return_value = _health(tmdb_api_key="ok", qb_api_key="invalid")
        jellyfin_client.health.return_value = _health(jellyfin_api_key="ok")
        states = asyncio.run(monitor.aggregate())
        assert list(states) == list(CREDENTIAL_NAMES)
        assert states[CREDENTIAL_QB_API_KEY].status is CredentialStatus.INVALID
        assert states[CREDENTIAL_DISCORD_TOKEN].status is CredentialStatus.UNKNOWN

    def test_unreachable_worker_marks_its_credentials_unreachable(
        self, monitor: CredentialMonitor, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ) -> None:
        torrent_client.health.return_value = None
        jellyfin_client.health.return_value = _health(jellyfin_api_key="ok")
        states = asyncio.run(monitor.aggregate())
        assert states[CREDENTIAL_TMDB_API_KEY].status is CredentialStatus.UNREACHABLE
        assert states[CREDENTIAL_JELLYFIN_API_KEY].status is CredentialStatus.OK

    def test_worker_without_the_field_reports_unknown(
        self, monitor: CredentialMonitor, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ) -> None:
        torrent_client.health.return_value = {"status": "online"}
        jellyfin_client.health.return_value = {"status": "online"}
        states = asyncio.run(monitor.aggregate())
        assert all(states[n].status is CredentialStatus.UNKNOWN for n in CREDENTIAL_NAMES)

    def test_bot_report_is_included(
        self, monitor: CredentialMonitor, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ) -> None:
        torrent_client.health.return_value = _health()
        jellyfin_client.health.return_value = _health()
        monitor.record_bot_report(CredentialState(status=CredentialStatus.INVALID, detail="x"))
        states = asyncio.run(monitor.aggregate())
        assert states[CREDENTIAL_DISCORD_TOKEN].status is CredentialStatus.INVALID

    def test_prefetched_bodies_skip_the_round_trip(
        self, monitor: CredentialMonitor, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ) -> None:
        states = asyncio.run(
            monitor.aggregate(
                _health(tmdb_api_key="ok"), _health(jellyfin_api_key="ok"), fetch=False
            )
        )
        torrent_client.health.assert_not_awaited()
        assert states[CREDENTIAL_TMDB_API_KEY].status is CredentialStatus.OK


class TestTransitions:
    def test_notifies_once_per_transition_and_persists(
        self,
        monitor: CredentialMonitor,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
        notices: list[tuple[str, str]],
        ledger: CredentialLedger,
        mocker,
    ) -> None:
        mocker.patch.object(module.config, "discord_notify_webhook_url", WEBHOOK)
        jellyfin_client.health.return_value = _health(jellyfin_api_key="ok")
        torrent_client.health.return_value = _health(tmdb_api_key="invalid", qb_api_key="ok")
        asyncio.run(monitor.tick())
        asyncio.run(monitor.tick())
        assert notices == [(WEBHOOK, invalid_notice(CREDENTIAL_TMDB_API_KEY, ""))]
        assert ledger.last_seen[CREDENTIAL_TMDB_API_KEY] == "invalid"
        torrent_client.health.return_value = _health(tmdb_api_key="ok", qb_api_key="ok")
        asyncio.run(monitor.tick())
        assert notices[-1] == (WEBHOOK, restored_notice(CREDENTIAL_TMDB_API_KEY))
        assert len(notices) == 2

    def test_unreachable_is_not_a_transition(
        self,
        monitor: CredentialMonitor,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
        notices,
        mocker,
    ) -> None:
        mocker.patch.object(module.config, "discord_notify_webhook_url", WEBHOOK)
        torrent_client.health.return_value = None
        jellyfin_client.health.return_value = None
        asyncio.run(monitor.tick())
        assert notices == []

    def test_no_webhook_means_no_post_but_states_remembered(
        self,
        monitor: CredentialMonitor,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
        notices,
        ledger,
        mocker,
    ) -> None:
        mocker.patch.object(module.config, "discord_notify_webhook_url", None)
        torrent_client.health.return_value = _health(tmdb_api_key="invalid")
        jellyfin_client.health.return_value = _health()
        asyncio.run(monitor.tick())
        assert notices == []
        assert ledger.last_seen[CREDENTIAL_TMDB_API_KEY] == "invalid"

    def test_ledger_survives_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        first = CredentialLedger(path)
        first.remember({CREDENTIAL_TMDB_API_KEY: CredentialState(status=CredentialStatus.INVALID)})
        first.record_bot_report(CredentialState(status=CredentialStatus.OK))
        second = CredentialLedger(path)
        assert second.last_seen == {CREDENTIAL_TMDB_API_KEY: "invalid"}
        assert second.bot_report is not None
        assert second.bot_report.status is CredentialStatus.OK

    def test_corrupt_ledger_starts_fresh(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text("not json")
        assert CredentialLedger(path).last_seen == {}


class TestHealthAndReportRoutes:
    def test_health_carries_the_credential_map(
        self,
        app_client: TestClient,
        context: AppContext,
        monitor: CredentialMonitor,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
    ) -> None:
        context.credentials = monitor
        torrent_client.is_reachable.return_value = True
        jellyfin_client.is_reachable.return_value = True
        torrent_client.health.return_value = _health(tmdb_api_key="invalid", qb_api_key="ok")
        jellyfin_client.health.return_value = _health(jellyfin_api_key="ok")
        body = app_client.get("/api/v1/health").json()
        assert body["credentials"][CREDENTIAL_TMDB_API_KEY]["status"] == "invalid"
        assert set(body["credentials"]) == set(CREDENTIAL_NAMES)

    def test_bot_reports_its_login_result(
        self,
        app_client: TestClient,
        context: AppContext,
        monitor: CredentialMonitor,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
        ledger: CredentialLedger,
    ) -> None:
        context.credentials = monitor
        torrent_client.health.return_value = _health()
        jellyfin_client.health.return_value = _health()
        response = app_client.post(
            f"/api/v1/credentials/{CREDENTIAL_DISCORD_TOKEN}",
            json={"status": "invalid", "detail": "LoginFailure"},
        )
        assert response.status_code == 204
        assert ledger.bot_report is not None
        assert ledger.bot_report.status is CredentialStatus.INVALID

    def test_only_client_reported_names_are_accepted(
        self, app_client: TestClient, context: AppContext, monitor: CredentialMonitor
    ) -> None:
        context.credentials = monitor
        response = app_client.post(
            f"/api/v1/credentials/{CREDENTIAL_TMDB_API_KEY}", json={"status": "invalid"}
        )
        assert response.status_code == 422

    def test_report_requires_the_api_key(self, unauthed_client: TestClient) -> None:
        response = unauthed_client.post(
            f"/api/v1/credentials/{CREDENTIAL_DISCORD_TOKEN}", json={"status": "ok"}
        )
        assert response.status_code == 403
