"""Jellyfin in-app requests: favorites on Discover placeholders become
downloads.

Every ``discover_poll_seconds`` one tick: read the allowed users' Discover
favorites, file new requests and cancel abandoned ones, act on what is due,
then derive each open request's placeholder state from its jobs and the
library. A movie request is an automatic movie pick, retried every
``jellyfin_request_retry_hours`` while nothing is found. A show request is a
follow from the beginning, checked at once; the follow poll then fetches a
pack per complete season and single episodes for the rest. A request closes
when its title reaches the library. Spec:
``docs/specs/jellyfin-in-app-requests.md``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from medialab_contracts import (
    FavoriteScope,
    FollowRequest,
    FollowStart,
    FollowStartMode,
    MediaType,
    PlaceholderInfo,
    PlaceholderState,
    PlaceholderWrite,
    WatchlistAddRequest,
    WatchlistKind,
)

from medialab_orchestrator.clients import JellyfinClient, TorrentDownloaderClient
from medialab_orchestrator.core.config import config
from medialab_orchestrator.core.errors import AppException
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.services.download import submit_pick
from medialab_orchestrator.services.follow import FollowPoller
from medialab_orchestrator.services.metadata import resolve_title_year
from medialab_orchestrator.store import JobStatus, JobStore, WatchlistStore
from medialab_orchestrator.store.requests import JellyfinRequest, RequestStatus, RequestStore

PAUSED_POLL_RECHECK_SECONDS = 60.0
# After a wake, wait this long so a burst of webhook events is one tick.
WAKE_SETTLE_SECONDS = 2.0

TitleKey = tuple[MediaType, int]

# A job in one of these is on its way to the library.
ACTIVE_JOB_STATUSES = frozenset(
    {
        JobStatus.DOWNLOAD_SUBMITTED,
        JobStatus.DOWNLOADING,
        JobStatus.STOP_SEEDING,
        JobStatus.RESOLVE_META,
        JobStatus.RENAME,
        JobStatus.SCAN,
    }
)
# A movie whose every job ended here was abandoned by a human: pick again.
ABANDONED_JOB_STATUSES = frozenset({JobStatus.DELETED, JobStatus.DISMISSED})
# Before anything was submitted, dropping every favorite cancels the request.
CANCELLABLE_STATUSES = frozenset({RequestStatus.REQUESTED, RequestStatus.NOT_FOUND})


def movie_query(title: str, year: int | None) -> str:
    """Movie release names carry the year; the web search builds it the same way."""
    return f"{title} {year}" if year else title


def retry_due(request: JellyfinRequest, *, now: datetime, retry_hours: int) -> bool:
    if request.last_tried_at is None:
        return True
    return request.last_tried_at + timedelta(hours=retry_hours) <= now


class RequestPoller:
    """Ticks every ``discover_poll_seconds``; settings are read from config on
    every use so a runtime change applies at the next tick."""

    def __init__(
        self,
        *,
        store: JobStore,
        watchlist: WatchlistStore,
        requests: RequestStore,
        torrent_client: TorrentDownloaderClient,
        jellyfin_client: JellyfinClient,
        follow_poller: FollowPoller,
    ) -> None:
        self._store = store
        self._watchlist = watchlist
        self._requests = requests
        self._torrent = torrent_client
        self._jellyfin = jellyfin_client
        self._follow = follow_poller
        self._wake = asyncio.Event()

    def wake(self) -> None:
        """Cut the current wait short (a Jellyfin webhook event)."""
        self._wake.set()

    async def run(self) -> None:
        """Tick forever; the interval is re-read before every sleep and 0
        pauses requests (checked again after ``PAUSED_POLL_RECHECK_SECONDS``,
        wakes ignored). A wake ends the wait early."""
        while True:
            interval = float(config.discover_poll_seconds)
            if interval <= 0:
                self._wake.clear()
                await asyncio.sleep(PAUSED_POLL_RECHECK_SECONDS)
                continue
            await self._wait(interval)
            await self.tick()

    async def _wait(self, interval: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=interval)
        except TimeoutError:
            return
        await asyncio.sleep(WAKE_SETTLE_SECONDS)
        self._wake.clear()

    async def tick(self, *, now: datetime | None = None) -> None:
        """One pass. Never raises."""
        try:
            await self.poll(now=now or datetime.now(UTC))
        except Exception as exc:  # the loop must survive any downstream failure
            app_logger.warning("Request poll skipped: %s", exc)

    async def poll(self, *, now: datetime) -> None:
        """Library and placeholder reads failing abort the pass before any
        change; one request failing is logged and the rest go on."""
        library = {
            media_type: set((await self._jellyfin.library_tmdb_ids(media_type)).tmdb_ids)
            for media_type in MediaType
        }
        placeholders = {
            (media_type, info.tmdb_id): info
            for media_type in MediaType
            for info in (await self._jellyfin.placeholders(media_type)).placeholders
        }
        favorites, complete = await self._favorites()
        await self._file(favorites, placeholders)
        for request in self._cancel(favorites) if complete else []:
            await self._free_placeholder(request, placeholders)
        for request in self._requests.list_open():
            try:
                request = await self._act(request, library, now=now)
                await self._show_state(request, placeholders)
            except AppException as exc:
                app_logger.warning(
                    "Request %s %d: %s", request.media_type, request.tmdb_id, exc.detail
                )

    async def _favorites(self) -> tuple[dict[TitleKey, set[str]], bool]:
        """Who favorites which placeholder; ``False`` when a user's favorites
        could not be read, so nobody is cancelled on missing data."""
        holders: dict[TitleKey, set[str]] = {}
        complete = True
        for user_id in sorted(config.jellyfin_request_user_ids):
            try:
                events = await self._jellyfin.favorites(user_id, FavoriteScope.DISCOVER)
            except AppException as exc:
                complete = False
                app_logger.warning("Request poll: favorites of %s failed: %s", user_id, exc)
                continue
            for event in events:
                holders.setdefault((event.media_type, event.tmdb_id), set()).add(user_id)
        return holders, complete

    async def _file(
        self, favorites: dict[TitleKey, set[str]], placeholders: dict[TitleKey, PlaceholderInfo]
    ) -> None:
        """A favorite on a placeholder with no open request files one under
        the canonical TMDB title and year (a placeholder listing carries no
        year); a lookup failure leaves it for the next tick. A favorite on an
        open request joins its requesters."""
        open_requests = {(r.media_type, r.tmdb_id): r for r in self._requests.list_open()}
        for key, users in favorites.items():
            existing = open_requests.get(key)
            if existing is not None:
                if not users <= existing.users:
                    self._requests.update(*key, users=existing.users | users)
                continue
            if key not in placeholders:
                continue
            try:
                title, year = await resolve_title_year(self._torrent, *key)
            except AppException as exc:
                app_logger.warning("Request poll: %s %d not filed yet: %s", *key, exc.detail)
                continue
            title = title or placeholders[key].title
            self._requests.file(*key, title=title, year=year or None, users=users)
            app_logger.info("Request filed: %s %d %s by %s", *key, title, sorted(users))

    def _cancel(self, favorites: dict[TitleKey, set[str]]) -> list[JellyfinRequest]:
        """A requester whose favorite is gone leaves the request; the last one
        leaving before anything was submitted cancels it. Run only when every
        user's favorites were read, so nobody is cancelled on missing data."""
        cancelled: list[JellyfinRequest] = []
        for request in self._requests.list_open():
            key = (request.media_type, request.tmdb_id)
            remaining = request.users & favorites.get(key, set())
            if remaining == request.users:
                continue
            if remaining or request.status not in CANCELLABLE_STATUSES:
                self._requests.update(*key, users=remaining)
                continue
            cancelled.append(
                self._requests.update(*key, status=RequestStatus.CANCELLED, users=remaining)
            )
            self._watchlist.remove_saved(*key)
            app_logger.info("Request cancelled: %s %d %s", *key, request.title)
        return cancelled

    async def _free_placeholder(
        self, request: JellyfinRequest, placeholders: dict[TitleKey, PlaceholderInfo]
    ) -> None:
        """Back to ``available``, so reconcile manages it again."""
        key = (request.media_type, request.tmdb_id)
        info = placeholders.get(key)
        if info is None or info.state is PlaceholderState.AVAILABLE:
            return
        try:
            await self._jellyfin.put_placeholder(
                *key, PlaceholderWrite(title=request.title, year=request.year)
            )
        except AppException as exc:
            app_logger.warning("Request %s: placeholder not reset: %s", request.title, exc.detail)

    async def _act(
        self, request: JellyfinRequest, library: dict[MediaType, set[int]], *, now: datetime
    ) -> JellyfinRequest:
        """Close a request whose title is in the library; otherwise move a
        due one forward."""
        key = (request.media_type, request.tmdb_id)
        if request.tmdb_id in library[request.media_type]:
            return self._requests.update(*key, status=RequestStatus.CLOSED)
        if request.media_type is MediaType.SHOW:
            if request.status is RequestStatus.REQUESTED:
                return await self._follow_show(request)
            return request
        if request.status is RequestStatus.SUBMITTED and self._abandoned(request):
            return self._requests.update(*key, status=RequestStatus.NOT_FOUND, last_tried_at=now)
        due = request.status is RequestStatus.REQUESTED or (
            request.status is RequestStatus.NOT_FOUND
            and retry_due(request, now=now, retry_hours=int(config.jellyfin_request_retry_hours))
        )
        return await self._pick_movie(request, now=now) if due else request

    def _abandoned(self, request: JellyfinRequest) -> bool:
        jobs = self._store.list_jobs_for_title(request.media_type, request.tmdb_id)
        return bool(jobs) and all(job.status in ABANDONED_JOB_STATUSES for job in jobs)

    def _save(self, request: JellyfinRequest) -> None:
        self._watchlist.add(
            request.media_type,
            request.tmdb_id,
            WatchlistAddRequest(
                title=request.title, year=str(request.year) if request.year else None
            ),
        )

    async def _pick_movie(self, request: JellyfinRequest, *, now: datetime) -> JellyfinRequest:
        """A saved watchlist entry makes the request visible in web and the
        bot; the DONE hook removes it."""
        key = (request.media_type, request.tmdb_id)
        self._save(request)
        result = await self._torrent.pick_torrent(
            movie_query(request.title, request.year),
            media_type=MediaType.MOVIE,
            resolution=config.jellyfin_request_resolution,
            min_seeders=int(config.follow_minimum_seeders),
        )
        if result is None:
            app_logger.info("Request %s: no movie release found yet", request.title)
            return self._requests.update(*key, status=RequestStatus.NOT_FOUND, last_tried_at=now)
        job = await submit_pick(
            result,
            media_type=MediaType.MOVIE,
            tmdb_id=request.tmdb_id,
            store=self._store,
            torrent=self._torrent,
        )
        app_logger.info("Request %s submitted as job %s", request.title, job.id)
        return self._requests.update(*key, status=RequestStatus.SUBMITTED, last_tried_at=now)

    async def _follow_show(self, request: JellyfinRequest) -> JellyfinRequest:
        """Follow from the beginning unless the show is already followed (the
        user's own follow wins), then check it at once instead of waiting
        for the follow poll."""
        current = self._watchlist.get(MediaType.SHOW, request.tmdb_id)
        if current is None or current.kind is not WatchlistKind.FOLLOWING:
            self._save(request)
            current = self._watchlist.follow(
                request.tmdb_id,
                FollowRequest(
                    start=FollowStart(mode=FollowStartMode.BEGINNING),
                    resolution=config.jellyfin_request_resolution,
                ),
            )
        updated = self._requests.update(
            MediaType.SHOW, request.tmdb_id, status=RequestStatus.SUBMITTED
        )
        try:
            await self._follow.check_show(current)
        except AppException as exc:
            app_logger.warning("Request %s: first follow check failed: %s", request.title, exc)
        return updated

    def derived_state(self, request: JellyfinRequest) -> PlaceholderState:
        if request.status is RequestStatus.NOT_FOUND:
            return PlaceholderState.NOT_FOUND
        if request.status is RequestStatus.SUBMITTED and any(
            job.status in ACTIVE_JOB_STATUSES
            for job in self._store.list_jobs_for_title(request.media_type, request.tmdb_id)
        ):
            return PlaceholderState.DOWNLOADING
        return PlaceholderState.REQUESTED

    async def _show_state(
        self, request: JellyfinRequest, placeholders: dict[TitleKey, PlaceholderInfo]
    ) -> None:
        """Write the placeholder only when its state changes; a closed
        request's placeholder is removed."""
        key = (request.media_type, request.tmdb_id)
        info = placeholders.get(key)
        if request.status is RequestStatus.CLOSED:
            if info is not None:
                await self._jellyfin.delete_placeholder(*key)
            return
        state = self.derived_state(request)
        if info is not None and info.state is state:
            return
        await self._jellyfin.put_placeholder(
            *key, PlaceholderWrite(title=request.title, year=request.year, state=state)
        )
