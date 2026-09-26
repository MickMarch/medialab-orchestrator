"""Place a finished download into Jellyfin's documented layout.

Movies:  ``Movies/Title (Year)/Title (Year).ext``, other videos in ``extras/``.
Shows:   ``Shows/Title (Year)/Season NN/Title SNNEMM.ext``, specials in
         ``Season 00``, multi-episode files as ``SNNEMM-EMM``.

Title and year come from TMDB (resolved upstream), never from release names.
PTN parses each file name for season and episode only. Subtitle files follow
the video whose stem they extend, keeping their language suffix. Everything
else is left in the download folder, which is deleted only once it holds no
video at all.

``plan_rename`` is pure (it takes a file listing and returns moves) so the
placement rules are unit-testable with no disk; ``apply_plan`` does the I/O.
"""

from __future__ import annotations

import contextlib
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import PTN
from fastapi import status as fastapi_status
from medialab_contracts import MediaType

from medialab_orchestrator.core.errors import AppException, ErrorCode

VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm", ".mov"})
SUBTITLE_EXTENSIONS = frozenset({".srt", ".ass", ".sub", ".idx", ".vtt"})
EXTRAS_DIR = "extras"
"""Jellyfin lists videos in this movie subfolder as extras, not as versions."""

_SEASON_DIR_TEMPLATE = "Season {season:02d}"
_EPISODE_TEMPLATE = "S{season:02d}E{episode:02d}"
_EPISODE_RANGE_TEMPLATE = "S{season:02d}E{first:02d}-E{last:02d}"
_ILLEGAL_PATH_CHARS = re.compile(r'[\\/:*?"<>|]')
_WHITESPACE = re.compile(r"\s+")


class EpisodeUnparseableError(Exception):
    """Raised when a show's video file name yields no season and episode."""


class RenameIncompleteError(Exception):
    """Raised when video files remain in the source after the moves: a locked
    file kept its original in place, so the job must not report success."""


@dataclass(frozen=True)
class MediaFile:
    path: Path
    size: int


@dataclass(frozen=True)
class RenamePlan:
    source: Path
    scan_dir: Path
    moves: tuple[tuple[Path, Path], ...]


def source_root_name(content_path: str) -> str:
    """The on-disk root name (file or folder) from qBittorrent's content path,
    whichever separator the host used."""
    return content_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def usable_root_name(value: str | None) -> str | None:
    """A job's recorded on-disk root name, or None when it is not a bare name.

    Jobs from before the content_path fix stored a host root path here; joining
    that onto the media root produces nonsense, so anything with a separator or
    a drive is ignored and the caller falls back to the release name.
    """
    if not value or "/" in value or "\\" in value or ":" in value:
        return None
    return value


def sanitize_title(title: str) -> str:
    """Strip characters Windows paths reject, collapse whitespace, drop trailing dots."""
    cleaned = _ILLEGAL_PATH_CHARS.sub("", title)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned.rstrip(".").strip()


def title_dir(title: str, year: int) -> str:
    clean = sanitize_title(title)
    return f"{clean} ({year})" if year > 0 else clean


def parse_episode(file_name: str) -> tuple[int, list[int]]:
    """Season and sorted episode numbers from a file name via PTN.

    Raises ``EpisodeUnparseableError`` when either is missing, so the caller can
    fail the whole job instead of half-placing a season.
    """
    parsed = PTN.parse(file_name)
    season = parsed.get("season")
    episode = parsed.get("episode")
    if isinstance(season, list) or not isinstance(season, int):
        raise EpisodeUnparseableError(f"No single season number in file name: {file_name!r}")
    episodes = episode if isinstance(episode, list) else [episode]
    if not episodes or not all(isinstance(e, int) for e in episodes):
        raise EpisodeUnparseableError(f"No episode number in file name: {file_name!r}")
    return season, sorted(episodes)


def episode_stem(title: str, season: int, episodes: Sequence[int]) -> str:
    clean = sanitize_title(title)
    if len(episodes) == 1:
        return f"{clean} {_EPISODE_TEMPLATE.format(season=season, episode=episodes[0])}"
    tag = _EPISODE_RANGE_TEMPLATE.format(season=season, first=episodes[0], last=episodes[-1])
    return f"{clean} {tag}"


def list_files(source: Path) -> list[MediaFile]:
    """Every file under ``source`` (or ``source`` itself when it is a file), with sizes."""
    if source.is_file():
        return [MediaFile(path=source, size=source.stat().st_size)]
    if not source.is_dir():
        return []
    return sorted(
        (MediaFile(path=p, size=p.stat().st_size) for p in source.rglob("*") if p.is_file()),
        key=lambda f: str(f.path),
    )


def _is_video(file: MediaFile) -> bool:
    return file.path.suffix.lower() in VIDEO_EXTENSIONS


def _companions(video: MediaFile, files: Sequence[MediaFile]) -> list[tuple[MediaFile, str]]:
    """Subtitle files whose stem extends the video's stem, with the extra suffix
    (``.en``, ``.forced``) they carry after it."""
    stem = video.path.stem
    found = []
    for f in files:
        if f.path.suffix.lower() not in SUBTITLE_EXTENSIONS or f.path.parent != video.path.parent:
            continue
        if f.path.stem == stem or f.path.stem.startswith(stem + "."):
            found.append((f, f.path.stem[len(stem) :]))
    return found


def _plan_show(
    *, media_root: Path, title: str, year: int, files: Sequence[MediaFile]
) -> tuple[Path, list[tuple[Path, Path]]]:
    series_dir = media_root / title_dir(title, year)
    moves: list[tuple[Path, Path]] = []
    for video in filter(_is_video, files):
        try:
            season, episodes = parse_episode(video.path.name)
        except EpisodeUnparseableError as exc:
            raise AppException(
                status_code=fastapi_status.HTTP_422_UNPROCESSABLE_CONTENT,
                code=ErrorCode.EPISODE_UNPARSEABLE,
                detail=str(exc),
            ) from exc
        season_dir = series_dir / _SEASON_DIR_TEMPLATE.format(season=season)
        stem = episode_stem(title, season, episodes)
        moves.append((video.path, season_dir / f"{stem}{video.path.suffix.lower()}"))
        for companion, extra in _companions(video, files):
            moves.append((companion.path, season_dir / f"{stem}{extra}{companion.path.suffix}"))
    return series_dir, moves


def _plan_movie(
    *, media_root: Path, title: str, year: int, files: Sequence[MediaFile]
) -> tuple[Path, list[tuple[Path, Path]]]:
    movie_dir = media_root / title_dir(title, year)
    videos = sorted(filter(_is_video, files), key=lambda f: f.size, reverse=True)
    if not videos:
        return movie_dir, []
    main, *extras = videos
    stem = title_dir(title, year)
    moves: list[tuple[Path, Path]] = [(main.path, movie_dir / f"{stem}{main.path.suffix.lower()}")]
    for companion, extra in _companions(main, files):
        moves.append((companion.path, movie_dir / f"{stem}{extra}{companion.path.suffix}"))
    for video in extras:
        moves.append((video.path, movie_dir / EXTRAS_DIR / video.path.name))
    return movie_dir, moves


def plan_rename(
    *,
    media_type: MediaType,
    media_root: Path,
    release_name: str,
    title: str,
    year: int,
    files: Sequence[MediaFile],
) -> RenamePlan:
    """Compute every file move for a download without touching the filesystem."""
    source = media_root / release_name
    if media_type is MediaType.MOVIE:
        scan_dir, moves = _plan_movie(media_root=media_root, title=title, year=year, files=files)
    else:
        scan_dir, moves = _plan_show(media_root=media_root, title=title, year=year, files=files)
    return RenamePlan(source=source, scan_dir=scan_dir, moves=tuple(moves))


def apply_plan(plan: RenamePlan) -> list[Path]:
    """Execute the moves and return every destination that now exists.

    Idempotent: existing destinations and missing sources are skipped, so a
    retry after a partial run finishes the remainder. The source folder is
    removed only once it holds no video file. The returned list is what a later
    delete removes, exactly.
    """
    for src, dest in plan.moves:
        if not src.exists():
            continue
        if dest.exists():
            # An interrupted move: the copy landed but the locked source stayed.
            # Same size means the copy is complete, so only the source goes;
            # otherwise the destination is a partial copy and is redone.
            if dest.stat().st_size == src.stat().st_size:
                _unlink_quietly(src)
                continue
            dest.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dest))
        except PermissionError:
            # Windows: the copy may have landed while the source is locked (a
            # scanner or Jellyfin reading it). Leave both; the check below fails
            # the job so a retry finishes it once the lock clears.
            continue
    leftover = (
        [f.path.name for f in list_files(plan.source) if _is_video(f)]
        if plan.source.is_dir()
        else []
    )
    if leftover:
        raise RenameIncompleteError(
            "Video still present in the download folder after the move (locked?): "
            + ", ".join(leftover)
        )
    if plan.source.is_dir():
        shutil.rmtree(plan.source)
    return [dest for _, dest in plan.moves if dest.exists()]


def _unlink_quietly(path: Path) -> None:
    # A still-locked source is reported by the leftover check in apply_plan.
    with contextlib.suppress(PermissionError):
        path.unlink()
