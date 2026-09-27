"""Live download progress on job reads, and the shared DOWNLOADING rule.

A job listing that includes an active download makes one transfers read,
joins it to the jobs by lowercase hash and attaches ``JobProgress``. The same
join applies ``advance_to_downloading``, the one forward-only rule the health
poll also uses, so the two paths cannot drift. The read is best effort: a
downstream failure returns the jobs without progress.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from medialab_contracts import ETA_UNKNOWN_SECONDS, JobProgress

from medialab_orchestrator.clients import TorrentDownloaderClient
from medialab_orchestrator.core.errors import AppException
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.schemas.jobs import JobView
from medialab_orchestrator.store import JobStatus, JobStore, PipelineJob

ACTIVE_DOWNLOAD_STATES = frozenset({"downloading", "stalledDL", "metaDL", "forcedDL"})
"""qBittorrent states where the torrent is started and fetching (stalled
means started but no peers right now). Queued, paused and checking are not."""

AWAITING_DOWNLOAD = frozenset({JobStatus.DOWNLOAD_SUBMITTED, JobStatus.DOWNLOADING})
"""Job statuses still waiting on qBittorrent."""

_TRANSFERS_DATA_KEY = "data"
_HASH_KEY = "hash"
_STATE_KEY = "state"
_PROGRESS_KEY = "progress"
_SPEED_KEY = "download_speed"
_ETA_KEY = "eta_seconds"
_NO_PROGRESS = 0.0
_NO_SPEED = 0
_UNKNOWN_STATE = ""


def index_transfers(payload: Any) -> dict[str, dict[str, Any]]:
    """The downloader's ``/transfers`` payload keyed by lowercase hash."""
    return {
        str(transfer[_HASH_KEY]).lower(): transfer
        for transfer in (payload or {}).get(_TRANSFERS_DATA_KEY, [])
        if transfer.get(_HASH_KEY)
    }


def advance_to_downloading(
    store: JobStore, job: PipelineJob, transfer: dict[str, Any]
) -> PipelineJob:
    """Move a ``DOWNLOAD_SUBMITTED`` job to ``DOWNLOADING`` when qBittorrent is
    actively fetching it. Forward-only and idempotent; returns the current job."""
    if (
        job.status is JobStatus.DOWNLOAD_SUBMITTED
        and transfer.get(_STATE_KEY) in ACTIVE_DOWNLOAD_STATES
    ):
        return store.update_job(job.id, status=JobStatus.DOWNLOADING)
    return job


def job_progress(transfer: dict[str, Any]) -> JobProgress:
    """Map a transfer to ``JobProgress``; qBittorrent's unknown ETA becomes None."""
    eta = transfer.get(_ETA_KEY)
    return JobProgress(
        progress=float(transfer.get(_PROGRESS_KEY, _NO_PROGRESS)),
        download_speed=int(transfer.get(_SPEED_KEY, _NO_SPEED)),
        eta_seconds=None if eta is None or int(eta) == ETA_UNKNOWN_SECONDS else int(eta),
        state=str(transfer.get(_STATE_KEY, _UNKNOWN_STATE)),
    )


def _active_hash(job: PipelineJob) -> str | None:
    """The lowercase hash of a job still waiting on qBittorrent, else None."""
    if job.status in AWAITING_DOWNLOAD and job.torrent_hash:
        return job.torrent_hash.lower()
    return None


async def with_progress(
    jobs: Iterable[PipelineJob], *, store: JobStore, torrent: TorrentDownloaderClient
) -> list[JobView]:
    """Job views with live progress attached to active downloads.

    No active job means no downstream call. A failed transfers read is logged
    and the jobs are returned as stored.
    """
    jobs = list(jobs)
    if not any(_active_hash(job) for job in jobs):
        return [JobView.from_job(job) for job in jobs]
    try:
        transfers = index_transfers(await torrent.transfers())
    except AppException as exc:
        app_logger.warning("Job progress skipped: %s", exc.detail)
        return [JobView.from_job(job) for job in jobs]

    views: list[JobView] = []
    for job in jobs:
        active_hash = _active_hash(job)
        transfer = transfers.get(active_hash) if active_hash else None
        if transfer is None:
            views.append(JobView.from_job(job))
            continue
        current = advance_to_downloading(store, job, transfer)
        view = JobView.from_job(current)
        view.progress = job_progress(transfer)
        views.append(view)
    return views
