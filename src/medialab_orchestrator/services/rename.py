"""TV folder rename into Jellyfin's required convention.

Jellyfin's TV library requires ``Series Name (Year)/Season NN/`` exactly (season
zero-padded, never ``S01``). Torrent release names almost never match
(``Show.Name.S01.1080p.GROUP``), so the orchestrator restructures the downloaded
folder before triggering a Jellyfin scan.

Title and year come from TMDB (resolved upstream), never from the release name.
PTN parses the release name for the **season number only**. Movies need no
rename - Jellyfin matches movie folders loosely.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import PTN
from medialab_contracts import MediaType

from medialab_orchestrator.core.errors import AppException, ErrorCode

_SEASON_DIR_TEMPLATE = "Season {season:02d}"


class SeasonUnparseableError(Exception):
    """Raised when a TV release name yields no parseable season number."""


def parse_season(release_name: str) -> int:
    """Extract the season number from a release name via PTN.

    Raises ``SeasonUnparseableError`` if no season is present, so the worker can
    mark the job FAILED with a clear error rather than silently mis-filing.
    """
    parsed = PTN.parse(release_name)
    season = parsed.get("season")
    if isinstance(season, list):
        # Multi-season packs report a list; the single-season pipeline cannot
        # place these unambiguously - surface for operator handling.
        raise SeasonUnparseableError(
            f"Release name spans multiple seasons {season}: {release_name!r}"
        )
    if not isinstance(season, int):
        raise SeasonUnparseableError(f"No season number in release name: {release_name!r}")
    return season


def build_tv_dest(*, media_root: Path, title: str, year: int, season: int) -> Path:
    """Compute the Jellyfin-convention destination dir for a TV download."""
    series_dir = f"{title} ({year})"
    season_dir = _SEASON_DIR_TEMPLATE.format(season=season)
    return media_root / series_dir / season_dir


def build_movie_dest(*, media_root: Path, release_name: str) -> Path:
    """Movies are not renamed; destination is the download folder as-is."""
    return media_root / release_name


def plan_rename(
    *,
    media_type: MediaType,
    media_root: Path,
    release_name: str,
    title: str,
    year: int,
) -> tuple[Path, Path]:
    """Compute ``(source, dest)`` for a download without touching the filesystem.

    Pure function - the FS move is done separately by ``apply_rename`` so the
    planning is unit-testable with no disk.
    """
    source = media_root / release_name
    if media_type is MediaType.MOVIE:
        return source, build_movie_dest(media_root=media_root, release_name=release_name)
    try:
        season = parse_season(release_name)
    except SeasonUnparseableError as exc:
        raise AppException(
            status_code=422,
            code=ErrorCode.SEASON_UNPARSEABLE,
            detail=str(exc),
        ) from exc
    return source, build_tv_dest(media_root=media_root, title=title, year=year, season=season)


def apply_rename(source: Path, dest: Path) -> None:
    """Move ``source`` into ``dest`` through the shared media mount.

    Idempotent: if ``dest`` already exists, the move is skipped (a retry that
    already ran the move is a no-op). Movies where source == dest are also a
    no-op.
    """
    if dest.exists():
        return
    if source == dest:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
