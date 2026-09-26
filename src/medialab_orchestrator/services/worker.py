"""The pipeline worker: advances a job through its post-download lifecycle.

Driven by the completion webhook. Each step is idempotent and the job is
persisted after every transition, so a crash mid-pipeline resumes from the last
committed state on retry. Failure is forward-retry: the step records
``last_error`` and sets status FAILED; ``retry`` re-enters from the last good
state (the worker reads current status and runs the remaining steps).

Title/year come from TMDB (via torrent-downloader); PTN parses season and
episode per file. Moves go through the shared media mount, never a host shell.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from fastapi import status as fastapi_status
from medialab_contracts import MEDIA_TYPE_SUBDIRS

from medialab_orchestrator.clients import JellyfinClient, TorrentDownloaderClient
from medialab_orchestrator.core.config import config
from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.services.metadata import resolve_title_year
from medialab_orchestrator.services.rename import (
    RenameIncompleteError,
    apply_plan,
    list_files,
    plan_rename,
    source_root_name,
)
from medialab_orchestrator.store import JobStatus, JobStore, PipelineJob

_REENTRY_STATUSES = frozenset(
    {
        JobStatus.DOWNLOAD_SUBMITTED,
        JobStatus.DOWNLOADING,
        JobStatus.FAILED,
        JobStatus.NEEDS_ATTENTION,
    }
)
_UNRETRYABLE = frozenset({JobStatus.DELETED})


class PipelineWorker:
    """Runs the post-download pipeline for a single job at a time.

    The webhook hands a hash to ``process``; the worker advances that job
    through the remaining steps. Steps are async (downstream HTTP) except the
    file move, which runs in a thread executor so the event loop is not blocked.
    """

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

    async def process(self, torrent_hash: str) -> PipelineJob:
        """Advance the job from its current state to DONE, or FAILED on error."""
        job = self._store.get_job_by_hash(torrent_hash)
        if job.status in _UNRETRYABLE:
            raise AppException(
                status_code=fastapi_status.HTTP_409_CONFLICT,
                code=ErrorCode.INVALID_INPUT,
                detail=f"Job {job.id} was deleted; nothing to run.",
            )
        # A freshly-arrived webhook job may still be DOWNLOAD_SUBMITTED /
        # DOWNLOADING; the first pipeline step is STOP_SEEDING.
        if job.status in _REENTRY_STATUSES:
            job = self._reenter(job)

        try:
            while job.status is not JobStatus.DONE:
                job = await self._run_step(job)
        except Exception as exc:
            # Forward-retry saga: any step failure (a mapped AppException or an
            # unexpected error) marks the job FAILED with the step recorded, so
            # the background task never crashes silently and retry can resume.
            detail = exc.detail if isinstance(exc, AppException) else str(exc)
            app_logger.warning(
                "Job %s failed at %s: %s", job.torrent_hash, job.status.value, detail
            )
            return self._store.update_job(
                job.id,
                status=JobStatus.FAILED,
                last_error=f"{job.status.value}: {detail}",
                attempts=job.attempts + 1,
            )
        return job

    def _reenter(self, job: PipelineJob) -> PipelineJob:
        """Set a not-yet-started or failed job to the first pipeline step.

        A FAILED job keeps the step it failed at recorded only in last_error;
        retry restarts the pipeline from STOP_SEEDING (every step is idempotent,
        so re-running the early steps is safe).
        """
        return self._store.update_job(job.id, status=JobStatus.STOP_SEEDING)

    async def _run_step(self, job: PipelineJob) -> PipelineJob:
        step = _STEPS[job.status]
        return await step(self, job)

    async def _step_stop_seeding(self, job: PipelineJob) -> PipelineJob:
        # Remove the torrent from qBittorrent (files kept): once the pipeline owns
        # the files nothing should keep a handle on the download folder, which
        # RENAME is about to empty. Already-removed (404) counts as done.
        if job.torrent_hash is None:
            raise AppException(
                status_code=fastapi_status.HTTP_409_CONFLICT,
                code=ErrorCode.INVALID_INPUT,
                detail="Job reached STOP_SEEDING without a torrent hash.",
            )
        # Last chance to learn the on-disk root name before the torrent is gone.
        fields: dict[str, object] = {
            "status": JobStatus.RESOLVE_META,
            "seeding_removed_at": _utc_now(),
        }
        if job.source_path is None:
            content_path = await self._content_path_of(job.torrent_hash)
            if content_path:
                fields["source_path"] = source_root_name(content_path)
        await self._torrent.remove_transfer(job.torrent_hash)
        return self._store.update_job(job.id, **fields)

    async def _content_path_of(self, torrent_hash: str) -> str | None:
        payload = await self._torrent.transfers()
        for transfer in (payload or {}).get("data", []):
            if str(transfer.get("hash", "")).lower() == torrent_hash.lower():
                return str(transfer.get("content_path") or "") or None
        return None

    async def _step_resolve_meta(self, job: PipelineJob) -> PipelineJob:
        # The pipeline only runs once the completion webhook (which carries the
        # hash) has matched or stamped the job, so the hash is present here.
        if job.torrent_hash is None:
            raise AppException(
                status_code=fastapi_status.HTTP_409_CONFLICT,
                code=ErrorCode.INVALID_INPUT,
                detail="Job reached RESOLVE_META without a torrent hash.",
            )
        # The job already carries media_type and tmdb_id; nothing else is needed
        # from the downloader here.
        title, year = await resolve_title_year(self._torrent, job.media_type, job.tmdb_id)
        return self._store.update_job(
            job.id,
            status=JobStatus.RENAME,
            resolved_title=title,
            resolved_year=year,
        )

    async def _step_rename(self, job: PipelineJob) -> PipelineJob:
        media_root = Path(config.media_mount_path) / MEDIA_TYPE_SUBDIRS[job.media_type]
        # The on-disk root recorded from qBittorrent's content path; the display
        # name is only a fallback for jobs that predate it.
        root_name = job.source_path or job.release_name
        source = media_root / root_name
        files = await asyncio.to_thread(list_files, source)
        plan = plan_rename(
            media_type=job.media_type,
            media_root=media_root,
            release_name=root_name,
            title=job.resolved_title or "",
            year=job.resolved_year or 0,
            files=files,
        )
        # A retry after a completed move finds the source gone and the
        # destination present: that is done, not an error. Gone with no
        # destination either is a real problem, never a silent success.
        dest_exists = await asyncio.to_thread(plan.scan_dir.exists)
        if not files and not source.exists() and not dest_exists:
            raise AppException(
                status_code=fastapi_status.HTTP_404_NOT_FOUND,
                code=ErrorCode.SOURCE_NOT_FOUND,
                detail=f"Download folder not found: {source}",
            )
        try:
            placed = await asyncio.to_thread(apply_plan, plan)
        except RenameIncompleteError as exc:
            raise AppException(
                status_code=fastapi_status.HTTP_409_CONFLICT,
                code=ErrorCode.RENAME_INCOMPLETE,
                detail=str(exc),
            ) from exc
        return self._store.update_job(
            job.id,
            status=JobStatus.SCAN,
            dest_path=str(plan.scan_dir),
            placed_paths=[str(p) for p in placed],
        )

    async def _step_scan(self, job: PipelineJob) -> PipelineJob:
        await self._jellyfin.scan(path=job.dest_path or "")
        return self._store.update_job(job.id, status=JobStatus.DONE, last_error=None)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


_STEPS = {
    JobStatus.STOP_SEEDING: PipelineWorker._step_stop_seeding,
    JobStatus.RESOLVE_META: PipelineWorker._step_resolve_meta,
    JobStatus.RENAME: PipelineWorker._step_rename,
    JobStatus.SCAN: PipelineWorker._step_scan,
}
