"""Discover reconcile: keep the Jellyfin Discover placeholders in step with
what is worth requesting.

The desired set is the union of the released popular titles, the released
recommendations for every real-library title an allowed user favorited, and
the watchlist, minus anything already in the library. medialab-jellyfin owns
the folders; this loop only decides. A placeholder whose state is not
``available`` belongs to a request and is never removed here. Spec:
``docs/specs/jellyfin-in-app-requests.md``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from medialab_contracts import (
    DiscoverResponse,
    FavoriteScope,
    MediaType,
    PlaceholderInfo,
    PlaceholderState,
    PlaceholderWrite,
)

from medialab_orchestrator.clients import JellyfinClient, TorrentDownloaderClient
from medialab_orchestrator.core.config import config
from medialab_orchestrator.core.errors import AppException
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.store import WatchlistStore

PAUSED_POLL_RECHECK_SECONDS = 60.0

TitleKey = tuple[MediaType, int]


@dataclass(frozen=True)
class Candidate:
    media_type: MediaType
    tmdb_id: int
    title: str
    year: int | None

    @property
    def key(self) -> TitleKey:
        return (self.media_type, self.tmdb_id)


@dataclass
class ReconcilePlan:
    add: list[Candidate] = field(default_factory=list)
    remove: list[TitleKey] = field(default_factory=list)


def _year(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def _candidates(response: DiscoverResponse) -> list[Candidate]:
    return [
        Candidate(item.media_type, item.tmdb_id, item.title, _year(item.year))
        for item in response.items
    ]


def desired_placeholders(
    sources: Iterable[Iterable[Candidate]], library: Mapping[MediaType, set[int]]
) -> dict[TitleKey, Candidate]:
    """Every source's titles, first occurrence wins, minus the library."""
    desired: dict[TitleKey, Candidate] = {}
    for source in sources:
        for candidate in source:
            if candidate.tmdb_id in library.get(candidate.media_type, set()):
                continue
            desired.setdefault(candidate.key, candidate)
    return desired


def plan_reconcile(
    desired: Mapping[TitleKey, Candidate],
    current: Mapping[MediaType, list[PlaceholderInfo]],
    *,
    complete: bool,
) -> ReconcilePlan:
    """Add what is desired and missing. Remove an ``available`` placeholder no
    longer desired, but only when every source answered (``complete``): a
    TMDB outage must not empty Discover. Existing placeholders are left as
    they are; their state belongs to the request flow."""
    present = {
        (media_type, info.tmdb_id) for media_type, infos in current.items() for info in infos
    }
    plan = ReconcilePlan(add=[c for key, c in desired.items() if key not in present])
    if complete:
        plan.remove = [
            (media_type, info.tmdb_id)
            for media_type, infos in current.items()
            for info in infos
            if info.state is PlaceholderState.AVAILABLE
            and (media_type, info.tmdb_id) not in desired
        ]
    return plan


class DiscoverReconciler:
    """Reconciles once at startup, then every ``discover_refresh_seconds``.
    Settings are read from config on every use so a runtime change applies at
    the next refresh."""

    def __init__(
        self,
        *,
        watchlist: WatchlistStore,
        torrent_client: TorrentDownloaderClient,
        jellyfin_client: JellyfinClient,
    ) -> None:
        self._watchlist = watchlist
        self._torrent = torrent_client
        self._jellyfin = jellyfin_client

    async def run(self) -> None:
        """Reconcile, then sleep the interval, re-read every time; 0 pauses
        (checked again after ``PAUSED_POLL_RECHECK_SECONDS``)."""
        while True:
            interval = float(config.discover_refresh_seconds)
            if interval <= 0:
                await asyncio.sleep(PAUSED_POLL_RECHECK_SECONDS)
                continue
            await self.tick()
            await asyncio.sleep(interval)

    async def tick(self) -> None:
        """One reconcile. Never raises."""
        try:
            plan = await self.reconcile()
        except Exception as exc:  # the loop must survive any downstream failure
            app_logger.warning("Discover reconcile skipped: %s", exc)
            return
        app_logger.info(
            "Discover reconcile: %d added, %d removed.", len(plan.add), len(plan.remove)
        )

    async def reconcile(self) -> ReconcilePlan:
        """Read the library and the placeholders (either failing aborts before
        any write), gather the sources, apply the plan, refresh each changed
        library once."""
        library = {
            media_type: set((await self._jellyfin.library_tmdb_ids(media_type)).tmdb_ids)
            for media_type in MediaType
        }
        current = {
            media_type: (await self._jellyfin.placeholders(media_type)).placeholders
            for media_type in MediaType
        }
        sources, complete = await self._sources()
        plan = plan_reconcile(desired_placeholders(sources, library), current, complete=complete)
        await self._apply(plan)
        return plan

    async def _sources(self) -> tuple[list[list[Candidate]], bool]:
        """Watchlist, popular released, recommendations; ``False`` when any
        downstream read failed."""
        complete = True
        sources = [
            [
                Candidate(item.media_type, item.tmdb_id, item.title, _year(item.year))
                for item in self._watchlist.list()
            ]
        ]
        for media_type in MediaType:
            try:
                sources.append(_candidates(await self._torrent.discover(media_type, released=True)))
            except AppException as exc:
                complete = False
                app_logger.warning("Discover reconcile: popular %s failed: %s", media_type, exc)
        recommendations, recommendations_complete = await self._recommendations()
        sources.append(recommendations)
        return sources, complete and recommendations_complete

    async def _recommendations(self) -> tuple[list[Candidate], bool]:
        limit = int(config.discover_recommendations_per_title)
        users = config.jellyfin_request_user_ids
        if limit <= 0 or not users:
            return [], True
        complete = True
        seeds: dict[TitleKey, None] = {}
        for user_id in sorted(users):
            try:
                favorites = await self._jellyfin.favorites(user_id, FavoriteScope.LIBRARY)
            except AppException as exc:
                complete = False
                app_logger.warning("Discover reconcile: favorites of %s failed: %s", user_id, exc)
                continue
            seeds.update(dict.fromkeys((f.media_type, f.tmdb_id) for f in favorites))
        found: list[Candidate] = []
        for media_type, tmdb_id in seeds:
            try:
                response = await self._torrent.recommendations(
                    media_type, tmdb_id, released=True, limit=limit
                )
            except AppException as exc:
                complete = False
                app_logger.warning(
                    "Discover reconcile: recommendations for %s %d failed: %s",
                    media_type,
                    tmdb_id,
                    exc,
                )
                continue
            found.extend(_candidates(response))
        return found, complete

    async def _apply(self, plan: ReconcilePlan) -> None:
        """One failed write is logged and skipped; the rest still apply."""
        changed: set[MediaType] = set()
        for candidate in plan.add:
            body = PlaceholderWrite(title=candidate.title, year=candidate.year)
            try:
                await self._jellyfin.put_placeholder(
                    candidate.media_type, candidate.tmdb_id, body, refresh=False
                )
            except AppException as exc:
                app_logger.warning("Discover reconcile: add %s failed: %s", candidate.key, exc)
                continue
            changed.add(candidate.media_type)
        for media_type, tmdb_id in plan.remove:
            try:
                await self._jellyfin.delete_placeholder(media_type, tmdb_id, refresh=False)
            except AppException as exc:
                app_logger.warning(
                    "Discover reconcile: remove %s %d failed: %s", media_type, tmdb_id, exc
                )
                continue
            changed.add(media_type)
        for media_type in MediaType:
            if media_type in changed:
                await self._jellyfin.refresh_discover(media_type)
