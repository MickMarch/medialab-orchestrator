"""The follow poll: what a followed show still wants, and the tick that
fetches it.

A wanted episode is on or after the follow's start point, aired at least
``follow_delay_hours`` ago, not in the library, not the scope of a queued job,
and never submitted by the follow before (``submitted`` and ``ignored`` both
block; Retry clears the record). Every tick walks the unpaused follows and
groups what each wants by season. A complete season nothing has been fetched
from is asked for as one pack; a missing pack parks the season until the
user decides. Everything else is asked for episode by episode in air order.
Every submission goes through the same path as ``POST /download``. See
``docs/specs/watchlist.md`` and ``docs/specs/follow-season-packs.md``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from medialab_contracts import (
    EpisodeState,
    FollowStartMode,
    FollowState,
    MediaType,
    SeasonFollowMode,
    ShowBrowseResponse,
    WatchlistItem,
    WatchlistKind,
)

from medialab_orchestrator.clients import JellyfinClient, TorrentDownloaderClient
from medialab_orchestrator.core.config import config
from medialab_orchestrator.core.errors import AppException
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.schemas.jobs import DownloadRequest
from medialab_orchestrator.services.download import create_submitted_job, submit_job
from medialab_orchestrator.services.notify import (
    follow_notice,
    pack_not_found_notice,
    post_discord,
)
from medialab_orchestrator.services.shows import browse_show
from medialab_orchestrator.store import JobStore, PipelineJob, WatchlistStore
from medialab_orchestrator.store.watchlist import EpisodeKeyTuple, SubmissionRecord

PAUSED_POLL_RECHECK_SECONDS = 60.0
SPECIALS_SEASON = 0

# Keys of the downloader's bare torrent result.
_RESULT_NAME_KEY = "fileName"
_RESULT_URL_KEY = "fileUrl"

Submissions = dict[EpisodeKeyTuple, SubmissionRecord]


# Season modes under which the poll asks for the pack.
_PACK_SEARCH_MODES = frozenset(
    {
        SeasonFollowMode.PACK,
        SeasonFollowMode.PACK_RETRY_TIMEOUT,
        SeasonFollowMode.PACK_RETRY_SEEDERS,
    }
)


def episode_code(season: int, episode: int) -> str:
    """``S02E05`` for season 2 episode 5."""
    return f"S{season:02d}E{episode:02d}"


def season_code(season: int) -> str:
    """``S02`` for season 2: a whole-season submission."""
    return f"S{season:02d}"


@dataclass(frozen=True)
class PackProfile:
    """The seeder floor and search timeout one pack attempt runs with."""

    min_seeders: int
    timeout_seconds: int


def pack_profile(mode: SeasonFollowMode) -> PackProfile:
    """The standard pack profile, or the one relaxation the user chose."""
    min_seeders = int(config.follow_pack_minimum_seeders)
    timeout = int(config.follow_pack_timeout_seconds)
    if mode is SeasonFollowMode.PACK_RETRY_TIMEOUT:
        timeout = int(config.follow_pack_retry_timeout_seconds)
    elif mode is SeasonFollowMode.PACK_RETRY_SEEDERS:
        min_seeders = int(config.follow_pack_retry_minimum_seeders)
    return PackProfile(min_seeders=min_seeders, timeout_seconds=timeout)


def _at_or_after_start(episode: EpisodeState, follow: FollowState) -> bool:
    start = follow.start
    if start.mode is FollowStartMode.BEGINNING:
        return True
    if start.mode is FollowStartMode.NEW_ONLY:
        return episode.air_date is not None and episode.air_date >= follow.followed_at.date()
    assert start.season is not None and start.episode is not None
    return (episode.season, episode.episode) >= (start.season, start.episode)


def _aired_long_enough(episode: EpisodeState, *, now: datetime, delay_hours: int) -> bool:
    """The air date (taken as the start of that day, UTC) plus the delay has passed."""
    if episode.air_date is None:
        return False
    aired_at = datetime.combine(episode.air_date, time.min, tzinfo=UTC)
    return aired_at + timedelta(hours=delay_hours) <= now


def is_wanted(
    episode: EpisodeState,
    follow: FollowState,
    submissions: Submissions,
    *,
    now: datetime,
    delay_hours: int,
) -> bool:
    return (
        episode.season != SPECIALS_SEASON
        and _at_or_after_start(episode, follow)
        and _aired_long_enough(episode, now=now, delay_hours=delay_hours)
        and not episode.in_library
        and episode.queued_job_id is None
        and (episode.season, episode.episode) not in submissions
    )


def season_episodes(browse: ShowBrowseResponse, season: int) -> list[EpisodeState]:
    return [episode for episode in browse.episodes if episode.season == season]


def pack_eligible(
    browse: ShowBrowseResponse,
    season: int,
    submissions: Submissions,
    *,
    now: datetime,
    delay_hours: int,
) -> bool:
    """Whether the season may be fetched as one pack: it is complete and
    nothing of it is in the library, queued, or recorded as submitted.
    Complete means TMDB's next episode is not in this season, the season lists
    as many episodes as TMDB counts for it, and every one aired at least the
    delay ago. Specials never qualify."""
    if season == SPECIALS_SEASON:
        return False
    episodes = season_episodes(browse, season)
    if not episodes:
        return False
    if browse.next_episode is not None and browse.next_episode.season == season:
        return False
    counted = next((s.episode_count for s in browse.seasons if s.season == season), 0)
    if counted and len(episodes) < counted:
        return False
    return all(
        _aired_long_enough(episode, now=now, delay_hours=delay_hours)
        and not episode.in_library
        and episode.queued_job_id is None
        and (episode.season, episode.episode) not in submissions
        for episode in episodes
    )


def _seasons_in_order(wanted: list[EpisodeState]) -> list[int]:
    """Season numbers in the order the wanted episodes (already in air order) reach them."""
    return list(dict.fromkeys(episode.season for episode in wanted))


def wanted_episodes(
    browse: ShowBrowseResponse,
    follow: FollowState,
    submissions: Submissions,
    *,
    now: datetime,
    delay_hours: int,
) -> list[EpisodeState]:
    """The episodes the follow should fetch, oldest first."""
    wanted = [
        episode
        for episode in browse.episodes
        if is_wanted(episode, follow, submissions, now=now, delay_hours=delay_hours)
    ]
    # Every wanted episode has an air date; season and episode break same-day ties.
    return sorted(wanted, key=lambda e: (e.air_date or date.min, e.season, e.episode))


def annotate_follow(
    browse: ShowBrowseResponse,
    follow: FollowState,
    submissions: Submissions,
    *,
    now: datetime,
    delay_hours: int,
) -> ShowBrowseResponse:
    """The browse view with ``submitted`` and ``wanted`` filled per episode."""
    episodes = [
        episode.model_copy(
            update={
                "submitted": _submission_state(submissions, episode),
                "wanted": is_wanted(episode, follow, submissions, now=now, delay_hours=delay_hours),
            }
        )
        for episode in browse.episodes
    ]
    return browse.model_copy(update={"episodes": episodes})


def _submission_state(submissions: Submissions, episode: EpisodeState) -> Any:
    record = submissions.get((episode.season, episode.episode))
    return record[0] if record is not None else None


class FollowPoller:
    """Ticks over every unpaused follow. Settings are read from config on every
    use so a runtime change applies at the next tick."""

    def __init__(
        self,
        *,
        store: JobStore,
        watchlist: WatchlistStore,
        torrent_client: TorrentDownloaderClient,
        jellyfin_client: JellyfinClient,
    ) -> None:
        self._store = store
        self._watchlist = watchlist
        self._torrent = torrent_client
        self._jellyfin = jellyfin_client

    async def run(self) -> None:
        """Tick forever; the interval is re-read before every sleep and 0 pauses
        the poll (checked again after ``PAUSED_POLL_RECHECK_SECONDS``)."""
        while True:
            interval = float(config.follow_poll_interval_seconds)
            if interval <= 0:
                await asyncio.sleep(PAUSED_POLL_RECHECK_SECONDS)
                continue
            await asyncio.sleep(interval)
            await self.tick()

    async def tick(self) -> None:
        """One pass over every unpaused follow. Never raises."""
        for item in self._watchlist.list(MediaType.SHOW, WatchlistKind.FOLLOWING):
            if item.follow is None or item.follow.paused:
                continue
            try:
                await self.check_show(item)
            except Exception as exc:  # one bad show must not stop the sweep
                app_logger.warning("Follow poll: show %d raised: %s", item.tmdb_id, exc)

    async def check_show(self, item: WatchlistItem) -> list[str]:
        """Fetch the wanted episodes of one follow, up to the per-tick cap, and
        return the codes submitted. A downloader error stops the show for this
        tick; the check is stamped either way. Errors fetching the show view
        propagate."""
        assert item.follow is not None
        follow = item.follow
        now = datetime.now(UTC)
        submitted: list[str] = []
        try:
            browse = await browse_show(
                item.tmdb_id,
                torrent=self._torrent,
                jellyfin=self._jellyfin,
                store=self._store,
                watchlist=self._watchlist,
            )
            submissions = self._watchlist.submissions(item.tmdb_id)
            delay_hours = int(config.follow_delay_hours)
            wanted = wanted_episodes(browse, follow, submissions, now=now, delay_hours=delay_hours)
            await self._submit_wanted(
                browse, follow, wanted, submitted, submissions, now=now, delay_hours=delay_hours
            )
        finally:
            self._watchlist.mark_checked(
                item.tmdb_id, now, last_submitted=submitted[-1] if submitted else None
            )
        return submitted

    async def _submit_wanted(
        self,
        browse: ShowBrowseResponse,
        follow: FollowState,
        wanted: list[EpisodeState],
        submitted: list[str],
        submissions: Submissions,
        *,
        now: datetime,
        delay_hours: int,
    ) -> None:
        """Season by season in air order: the pack where the season qualifies
        and its mode asks for one, otherwise the episodes one at a time. A
        season parked at ``pack_not_found`` is skipped. A pack counts as one
        submission against the per-tick cap."""
        cap = int(config.follow_max_submissions_per_tick)
        states = self._watchlist.season_states(browse.tmdb_id)
        try:
            for season in _seasons_in_order(wanted):
                if len(submitted) >= cap:
                    return
                state = states.get(season)
                mode = state.mode if state is not None else SeasonFollowMode.PACK
                if mode is SeasonFollowMode.PACK_NOT_FOUND:
                    continue
                if mode in _PACK_SEARCH_MODES and pack_eligible(
                    browse, season, submissions, now=now, delay_hours=delay_hours
                ):
                    await self._try_pack(browse, follow, season, mode, submitted)
                    continue
                for episode in wanted:
                    if episode.season != season:
                        continue
                    if len(submitted) >= cap:
                        return
                    await self._try_episode(browse, follow, episode, submitted)
        except AppException as exc:
            app_logger.warning(
                "Follow poll: show %d stopped for this tick: %s", browse.tmdb_id, exc.detail
            )

    async def _try_episode(
        self,
        browse: ShowBrowseResponse,
        follow: FollowState,
        episode: EpisodeState,
        submitted: list[str],
    ) -> None:
        result = await self._torrent.pick_torrent(
            browse.title,
            season=episode.season,
            episode=episode.episode,
            resolution=follow.resolution,
            min_seeders=int(config.follow_minimum_seeders),
        )
        if result is None:
            return
        code = episode_code(episode.season, episode.episode)
        job = await self._submit(browse, result, season=episode.season, episode=episode.episode)
        self._watchlist.record_submission(browse.tmdb_id, episode.season, episode.episode, job.id)
        await self._announce(browse, code, job)
        submitted.append(code)

    async def _try_pack(
        self,
        browse: ShowBrowseResponse,
        follow: FollowState,
        season: int,
        mode: SeasonFollowMode,
        submitted: list[str],
    ) -> None:
        """One pack attempt with the profile the mode asks for. Found: one
        season job, a submission for every episode of the season, the row back
        to ``pack`` with the job. Missing: the row parks at ``pack_not_found``
        and the user is told."""
        profile = pack_profile(mode)
        code = season_code(season)
        result = await self._torrent.pick_torrent(
            browse.title,
            season=season,
            resolution=follow.resolution,
            min_seeders=profile.min_seeders,
            timeout_seconds=profile.timeout_seconds,
        )
        if result is None:
            self._watchlist.set_season_mode(
                browse.tmdb_id, season, SeasonFollowMode.PACK_NOT_FOUND, tried=True
            )
            app_logger.info("Follow poll: %s %s: no season pack found", browse.title, code)
            if config.discord_notify_webhook_url:
                await post_discord(
                    config.discord_notify_webhook_url, pack_not_found_notice(browse.title, code)
                )
            return
        job = await self._submit(browse, result, season=season, episode=None)
        for episode in season_episodes(browse, season):
            self._watchlist.record_submission(browse.tmdb_id, season, episode.episode, job.id)
        self._watchlist.set_season_mode(
            browse.tmdb_id, season, SeasonFollowMode.PACK, tried=True, job_id=job.id
        )
        await self._announce(browse, code, job)
        submitted.append(code)

    async def _submit(
        self,
        browse: ShowBrowseResponse,
        result: dict[str, Any],
        *,
        season: int,
        episode: int | None,
    ) -> PipelineJob:
        payload = DownloadRequest(
            source_url=str(result[_RESULT_URL_KEY]),
            media_type=MediaType.SHOW,
            tmdb_id=browse.tmdb_id,
            release_name=str(result.get(_RESULT_NAME_KEY, "")),
            season=season,
            episode=episode,
        )
        job = create_submitted_job(self._store, payload)
        return await submit_job(job, payload, store=self._store, torrent=self._torrent)

    async def _announce(self, browse: ShowBrowseResponse, code: str, job: PipelineJob) -> None:
        app_logger.info("Follow poll: %s %s submitted as job %s", browse.title, code, job.id)
        if config.discord_notify_webhook_url:
            await post_discord(
                config.discord_notify_webhook_url,
                follow_notice(browse.title, code, job.release_name),
            )
