"""Undo a download: plan what a delete would touch, then execute it.

A job may be mid-download (torrent and partial files), mid-pipeline (download
folder still in place), or placed (files in the library). The plan is computed
without side effects so the user sees the exact paths before confirming; the
execution runs the same plan, each step idempotent, and marks the job DELETED.
Nothing outside the media root is ever removed.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import status as fastapi_status
from medialab_contracts import MEDIA_TYPE_SUBDIRS, MediaType

from medialab_orchestrator.clients import JellyfinClient, TorrentDownloaderClient
from medialab_orchestrator.core.config import config
from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.services.rename import usable_root_name
from medialab_orchestrator.store import JobStatus, JobStore, PipelineJob

SCAN_UPDATE_DELETED = "Deleted"
_TORRENT_MAY_REMAIN = frozenset({JobStatus.DOWNLOAD_SUBMITTED, JobStatus.DOWNLOADING})
_PLACED = frozenset({JobStatus.SCAN, JobStatus.DONE})


@dataclass(frozen=True)
class DeletionPlan:
    torrent: bool
    download_folder: str | None
    placed_paths: list[str] = field(default_factory=list)
    scan_path: str | None = None
    refused: str | None = None


def _media_root(media_type: MediaType) -> Path:
    return Path(config.media_mount_path) / MEDIA_TYPE_SUBDIRS[media_type]


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def plan_deletion(job: PipelineJob) -> DeletionPlan:
    """What deleting this job would remove. Pure: touches no disk, no service."""
    if job.status is JobStatus.DELETED:
        return DeletionPlan(torrent=False, download_folder=None, refused="already deleted")
    root = _media_root(job.media_type)
    torrent = job.torrent_hash is not None and (
        job.status in _TORRENT_MAY_REMAIN or job.seeding_removed_at is None
    )
    root_name = usable_root_name(job.source_path) or usable_root_name(job.release_name)
    download_folder = str(root / root_name) if root_name else None

    placed: list[str] = list(job.placed_paths)
    scan_path: str | None = None
    refused: str | None = None
    if job.status in _PLACED and not placed:
        if job.media_type is MediaType.MOVIE and job.dest_path:
            placed = [job.dest_path]
        else:
            refused = (
                "this job predates placed-file tracking; remove its episodes by hand under "
                f"{job.dest_path or root}"
            )
    if placed:
        # Movies: the folder itself goes, so tell Jellyfin about the library
        # root. Shows: the series folder stays (other seasons), so scan it.
        scan_path = job.dest_path if job.media_type is MediaType.SHOW else str(root)
    return DeletionPlan(
        torrent=torrent,
        download_folder=download_folder,
        placed_paths=placed,
        scan_path=scan_path,
        refused=refused,
    )


def _remove_path(path: Path, root: Path) -> None:
    if not _within(path, root) or path.resolve() == root.resolve():
        raise AppException(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=ErrorCode.PERMISSION_DENIED,
            detail=f"Refusing to delete outside the media root: {path}",
        )
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _prune_empty_parents(path: Path, root: Path) -> None:
    parent = path.parent
    while _within(parent, root) and parent.resolve() != root.resolve():
        if parent.exists() and any(parent.iterdir()):
            return
        if parent.exists():
            parent.rmdir()
        parent = parent.parent


class DeletionService:
    def __init__(
        self,
        *,
        store: JobStore,
        torrent_client: TorrentDownloaderClient,
        jellyfin_client: JellyfinClient,
    ) -> None:
        self._store = store
        self._torrent = torrent_client
        self._jellyfin = jellyfin_client

    async def execute(self, job: PipelineJob) -> PipelineJob:
        plan = plan_deletion(job)
        if plan.refused:
            raise AppException(
                status_code=fastapi_status.HTTP_409_CONFLICT,
                code=ErrorCode.INVALID_INPUT,
                detail=plan.refused,
            )
        root = _media_root(job.media_type)
        if plan.torrent and job.torrent_hash:
            await self._torrent.remove_transfer(job.torrent_hash, delete_files=True)
        if plan.download_folder:
            await asyncio.to_thread(_remove_path, Path(plan.download_folder), root)
        for placed in plan.placed_paths:
            path = Path(placed)
            await asyncio.to_thread(_remove_path, path, root)
            await asyncio.to_thread(_prune_empty_parents, path, root)
        if plan.scan_path:
            await self._jellyfin.scan(path=plan.scan_path, update_type=SCAN_UPDATE_DELETED)
        app_logger.info("Deleted job %s: %s", job.id, plan)
        return self._store.update_job(
            job.id,
            status=JobStatus.DELETED,
            last_error=None,
            deleted_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
