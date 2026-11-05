"""A show's episode numbering, read from torrent-downloader (the TMDB key
holder). Canonical (TMDB default) numbering stays the library layout, the
follow start and the submission key; release numbering is what searches,
packs and job scopes use. Spec: ``docs/specs/anime-numbering.md``."""

from __future__ import annotations

from medialab_contracts import EpisodeState, FollowState, NumberingMode, ShowNumbering

from medialab_orchestrator.clients import TorrentDownloaderClient
from medialab_orchestrator.core.logger import app_logger

NUMBERING_AUTO = "auto"
NUMBERING_TMDB = "tmdb"


def numbering_group(follow: FollowState | None) -> str:
    """The downloader's ``group`` value for a follow: auto when not followed."""
    if follow is None or follow.numbering is NumberingMode.AUTO:
        return NUMBERING_AUTO
    if follow.numbering is NumberingMode.TMDB:
        return NUMBERING_TMDB
    assert follow.episode_group_id is not None
    return follow.episode_group_id


async def fetch_numbering(
    torrent: TorrentDownloaderClient, tmdb_id: int, group: str
) -> ShowNumbering | None:
    """The show's numbering, or None when the downloader cannot answer: callers
    then behave as TMDB numbering. Broad on purpose, like library decoration."""
    try:
        return await torrent.show_numbering(tmdb_id, group)
    except Exception as exc:
        app_logger.warning("Numbering for show %d unavailable: %s", tmdb_id, exc)
        return None


def release_code(episode: EpisodeState) -> tuple[int, int]:
    """The episode's release ``(season, episode)``; canonical when unmapped."""
    if episode.release_season is None or episode.release_episode is None:
        return episode.season, episode.episode
    return episode.release_season, episode.release_episode


def absolute_number(numbering: ShowNumbering | None, episode: EpisodeState) -> int | None:
    """The absolute number to search with, only when the show is mapped."""
    if numbering is None or not numbering.is_mapped:
        return None
    number = numbering.for_canonical(episode.season, episode.episode)
    return number.absolute if number is not None else None
