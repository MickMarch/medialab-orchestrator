"""The pipeline worker: advances a job through its post-download lifecycle.

Driven by the completion webhook. Each step is idempotent and the job is
persisted after every transition, so a crash mid-pipeline resumes from the last
committed state on retry. Failure is forward-retry: the step records
``last_error`` and sets status FAILED; ``retry`` re-enters from the last good
state (the worker reads current status and runs the remaining steps).

Title/year come from TMDB (via torrent-downloader); PTN is season-only. The file
move goes through the shared media mount, never a host shell.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from medialab_contracts import MediaType

from medialab_orchestrator.clients import JellyfinClient, TorrentDownloaderClient
from medialab_orchestrator.core.config import config
from medialab_orchestrator.core.errors import AppException
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.services.metadata import resolve_title_year
from medialab_orchestrator.services.rename import apply_rename, plan_rename
from medialab_orchestrator.store import JobStatus, JobStore, PipelineJob


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
        # A freshly-arrived webhook job may still be DOWNLOAD_SUBMITTED /
        # DOWNLOADING; the first pipeline step is STOP_SEEDING.
        if job.status in (JobStatus.DOWNLOAD_SUBMITTED, JobStatus.DOWNLOADING, JobStatus.FAILED):
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
                job.torrent_hash,
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
        return self._store.update_job(job.torrent_hash, status=JobStatus.STOP_SEEDING)

    async def _run_step(self, job: PipelineJob) -> PipelineJob:
        step = _STEPS[job.status]
        return await step(self, job)

    async def _step_stop_seeding(self, job: PipelineJob) -> PipelineJob:
        await self._torrent.stop_seeding()
        return self._store.update_job(job.torrent_hash, status=JobStatus.RESOLVE_META)

    async def _step_resolve_meta(self, job: PipelineJob) -> PipelineJob:
        info = await self._torrent.transfer_info(job.torrent_hash)
        title, year = await resolve_title_year(self._torrent, job.media_type, job.tmdb_id)
        return self._store.update_job(
            job.torrent_hash,
            status=JobStatus.RENAME,
            source_path=info["host_path"],
            resolved_title=title,
            resolved_year=year,
        )

    async def _step_rename(self, job: PipelineJob) -> PipelineJob:
        media_root = Path(config.media_mount_path) / _MEDIA_SUBDIR[job.media_type]
        source, dest = plan_rename(
            media_type=job.media_type,
            media_root=media_root,
            release_name=job.release_name,
            title=job.resolved_title or "",
            year=job.resolved_year or 0,
        )
        await asyncio.to_thread(apply_rename, source, dest)
        return self._store.update_job(
            job.torrent_hash, status=JobStatus.REGISTER, dest_path=str(dest)
        )

    async def _step_register(self, job: PipelineJob) -> PipelineJob:
        await self._jellyfin.register_path(media_type=job.media_type, path=job.dest_path or "")
        return self._store.update_job(job.torrent_hash, status=JobStatus.SCAN)

    async def _step_scan(self, job: PipelineJob) -> PipelineJob:
        await self._jellyfin.scan(path=job.dest_path or "")
        return self._store.update_job(job.torrent_hash, status=JobStatus.DONE)


# In-container media-type subdir of the shared mount. Matches the dirs
# torrent-downloader/qBittorrent save into (Movies / Shows).
_MEDIA_SUBDIR: dict[MediaType, str] = {
    MediaType.MOVIE: "Movies",
    MediaType.SHOW: "Shows",
}

_STEPS = {
    JobStatus.STOP_SEEDING: PipelineWorker._step_stop_seeding,
    JobStatus.RESOLVE_META: PipelineWorker._step_resolve_meta,
    JobStatus.RENAME: PipelineWorker._step_rename,
    JobStatus.REGISTER: PipelineWorker._step_register,
    JobStatus.SCAN: PipelineWorker._step_scan,
}
