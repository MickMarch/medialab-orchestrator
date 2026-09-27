"""Browse a show: its TMDB episode listing joined with library presence and
queued jobs.

One downloader episodes call, one TMDB detail call for the header, one
best-effort jellyfin episodes call and one store read. Library lookups are
decoration: a failure is logged and treated as an empty library.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from medialab_contracts import Episode, EpisodeState, MediaType, ShowBrowseResponse

from medialab_orchestrator.clients import JellyfinClient, TorrentDownloaderClient
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.services.discover import library_tmdb_ids, watchlist_flags
from medialab_orchestrator.services.metadata import extract_title_year
from medialab_orchestrator.store import JobStatus, JobStore, PipelineJob, WatchlistStore

# Jobs no longer heading for the library. FAILED is retryable, so it still
# counts as queued. Mirrors the health poll's notion.
_TERMINAL = frozenset({JobStatus.DONE, JobStatus.NEEDS_ATTENTION, JobStatus.DELETED})

_DETAIL_DATA_KEY = "data"
_DETAIL_POSTER_KEY = "poster_path"
_DETAIL_OVERVIEW_KEY = "overview"
_UNKNOWN_YEAR = 0

EpisodeKeyTuple = tuple[int, int]


def job_covers(job: PipelineJob, episode: Episode) -> bool:
    """Whether the job's search scope includes this episode: whole series,
    the episode's season, or the episode itself."""
    if job.season is None:
        return True
    if job.season != episode.season:
        return False
    return job.episode is None or job.episode == episode.episode


def queued_job_id(jobs: list[PipelineJob], episode: Episode) -> str | None:
    """The newest non-terminal job whose scope covers ``episode``."""
    return next(
        (job.id for job in jobs if job.status not in _TERMINAL and job_covers(job, episode)),
        None,
    )


def episode_state(
    episode: Episode,
    *,
    today: date,
    in_library: set[EpisodeKeyTuple],
    jobs: list[PipelineJob],
) -> EpisodeState:
    return EpisodeState(
        **episode.model_dump(),
        aired=episode.air_date is not None and episode.air_date <= today,
        in_library=(episode.season, episode.episode) in in_library,
        queued_job_id=queued_job_id(jobs, episode),
    )


async def library_episode_keys(jellyfin: JellyfinClient, tmdb_id: int) -> set[EpisodeKeyTuple]:
    """The (season, episode) keys in the library for the series, or empty on failure."""
    try:
        response = await jellyfin.library_episodes(tmdb_id)
    except Exception as exc:
        # Broad on purpose: the badge is decoration and must never fail the page.
        app_logger.warning("Library episode lookup for show %d failed: %s", tmdb_id, exc)
        return set()
    return {(key.season, key.episode) for key in response.episodes}


def _detail_field(detail: Any, key: str) -> Any:
    data = detail.get(_DETAIL_DATA_KEY) if isinstance(detail, dict) else None
    return data.get(key) if isinstance(data, dict) else None


async def browse_show(
    tmdb_id: int,
    *,
    torrent: TorrentDownloaderClient,
    jellyfin: JellyfinClient,
    store: JobStore,
    watchlist: WatchlistStore,
) -> ShowBrowseResponse:
    listing = await torrent.series_episodes(tmdb_id)
    detail = await torrent.tmdb_detail(MediaType.SHOW, tmdb_id)
    title, year = extract_title_year(MediaType.SHOW, detail)
    in_library = await library_episode_keys(jellyfin, tmdb_id)
    jobs = store.list_jobs_for_title(MediaType.SHOW, tmdb_id)
    today = datetime.now(UTC).date()
    return ShowBrowseResponse(
        tmdb_id=tmdb_id,
        title=title,
        year=str(year) if year != _UNKNOWN_YEAR else None,
        poster_path=_detail_field(detail, _DETAIL_POSTER_KEY),
        overview=_detail_field(detail, _DETAIL_OVERVIEW_KEY) or "",
        status=listing.status,
        seasons=listing.seasons,
        episodes=[
            episode_state(episode, today=today, in_library=in_library, jobs=jobs)
            for episode in listing.episodes
        ],
        next_episode=listing.next_episode,
        **watchlist_flags(watchlist.keys(MediaType.SHOW), tmdb_id),
        in_library=tmdb_id in await library_tmdb_ids(jellyfin, MediaType.SHOW),
    )
