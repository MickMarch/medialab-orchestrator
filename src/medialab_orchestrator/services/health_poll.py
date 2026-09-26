"""Periodic health poll: the safety net under the completion webhook.

The webhook is the fast path; it cannot fire for a download that errors, and
it can be missed when any hop is down at the moment of completion. Every tick
reads the live transfer list once, joins it with the non-terminal jobs, and
applies one rule per job (see ``docs/specs/stuck-download-remediation.md``):

- errored download            -> resume, up to ``auto_resume_max`` times
- completed but unnoticed     -> run the pipeline exactly as the webhook would
- torrent gone from qBittorrent -> NEEDS_ATTENTION
- FAILED pipeline job         -> retry, up to ``auto_retry_max`` attempts
- budget exhausted            -> NEEDS_ATTENTION, and the poll stops touching it

Every rule is idempotent and a human retry resets the budgets.
"""

from __future__ import annotations

import asyncio
from typing import Any

from medialab_orchestrator.clients import TorrentDownloaderClient
from medialab_orchestrator.core.errors import AppException
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.services.worker import PipelineWorker
from medialab_orchestrator.store import JobStatus, JobStore, PipelineJob

ERROR_STATES = frozenset({"error", "missingFiles"})
"""qBittorrent states a resume can recover from."""

COMPLETE_STATES = frozenset(
    {"uploading", "stalledUP", "queuedUP", "pausedUP", "stoppedUP", "forcedUP", "checkingUP"}
)
"""qBittorrent states that only exist once the download reached 100%."""

_COMPLETE_PROGRESS = 1.0
_AWAITING_COMPLETION = frozenset({JobStatus.DOWNLOAD_SUBMITTED, JobStatus.DOWNLOADING})
_TERMINAL = frozenset({JobStatus.DONE, JobStatus.NEEDS_ATTENTION})


def is_complete(transfer: dict[str, Any]) -> bool:
    return (
        float(transfer.get("progress", 0.0)) >= _COMPLETE_PROGRESS
        or transfer.get("state") in COMPLETE_STATES
    )


class HealthPoller:
    def __init__(
        self,
        *,
        store: JobStore,
        torrent_client: TorrentDownloaderClient,
        worker: PipelineWorker,
        auto_resume_max: int,
        auto_retry_max: int,
    ) -> None:
        self._store = store
        self._torrent = torrent_client
        self._worker = worker
        self._auto_resume_max = auto_resume_max
        self._auto_retry_max = auto_retry_max

    async def run(self, interval_seconds: float) -> None:
        """Tick forever. Cancelled by the app lifespan on shutdown."""
        while True:
            await asyncio.sleep(interval_seconds)
            await self.tick()

    async def tick(self) -> None:
        """One pass over every non-terminal job. Never raises."""
        try:
            payload = await self._torrent.transfers()
        except AppException as exc:
            app_logger.warning("Health poll skipped: %s", exc.detail)
            return
        transfers = {t["hash"].lower(): t for t in (payload or {}).get("data", []) if t.get("hash")}
        for job in self._store.list_jobs():
            if job.status in _TERMINAL:
                continue
            try:
                await self._check(job, transfers)
            except Exception as exc:  # one bad job must not stop the sweep
                app_logger.warning("Health poll: job %s raised: %s", job.id, exc)

    async def _check(self, job: PipelineJob, transfers: dict[str, dict[str, Any]]) -> None:
        if job.status is JobStatus.FAILED:
            await self._retry_failed(job)
            return
        if job.status not in _AWAITING_COMPLETION or job.torrent_hash is None:
            return
        transfer = transfers.get(job.torrent_hash.lower())
        if transfer is None:
            self._flag(job, "torrent no longer in qBittorrent")
        elif transfer.get("state") in ERROR_STATES:
            await self._resume(job, str(transfer.get("state")))
        elif is_complete(transfer):
            await self._run_missed_pipeline(job, transfer)

    async def _resume(self, job: PipelineJob, state: str) -> None:
        if job.remediations >= self._auto_resume_max:
            self._flag(job, f"qBittorrent state {state} after {job.remediations} resumes")
            return
        assert job.torrent_hash is not None
        await self._torrent.resume_transfer(job.torrent_hash)
        self._store.update_job(job.id, remediations=job.remediations + 1)
        app_logger.info(
            "Health poll: resumed %s (%s), remediation %d/%d",
            job.torrent_hash,
            state,
            job.remediations + 1,
            self._auto_resume_max,
        )

    async def _run_missed_pipeline(self, job: PipelineJob, transfer: dict[str, Any]) -> None:
        assert job.torrent_hash is not None
        if not job.release_name and transfer.get("name"):
            self._store.update_job(job.id, release_name=str(transfer["name"]))
        app_logger.info(
            "Health poll: completion for %s was missed; running pipeline", job.torrent_hash
        )
        await self._worker.process(job.torrent_hash)

    async def _retry_failed(self, job: PipelineJob) -> None:
        if job.attempts > self._auto_retry_max:
            self._flag(job, job.last_error or "failed")
            return
        if job.torrent_hash is None:
            return
        app_logger.info("Health poll: retrying FAILED job %s (attempt %d)", job.id, job.attempts)
        await self._worker.process(job.torrent_hash)

    def _flag(self, job: PipelineJob, reason: str) -> None:
        app_logger.warning("Health poll: job %s needs attention: %s", job.id, reason)
        self._store.update_job(job.id, status=JobStatus.NEEDS_ATTENTION, last_error=reason)
