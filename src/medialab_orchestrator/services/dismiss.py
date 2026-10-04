"""Dismiss a flagged job: a human decided it is not worth pursuing.

The row keeps its error and its files; only the status and a timestamp
change, so nothing becomes silent. The follow submission goes ignored, as it
does for a delete, so the poll does not re-create the job just dismissed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import status as fastapi_status

from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.services.attention import ATTENTION_STATUSES
from medialab_orchestrator.store import JobStatus, JobStore, PipelineJob, WatchlistStore


def dismiss_job(job: PipelineJob, *, store: JobStore, watchlist: WatchlistStore) -> PipelineJob:
    """Mark ``job`` DISMISSED. Idempotent: an already dismissed job is returned as is.

    Raises ``409 JOB_NOT_DISMISSABLE`` for any status other than the attention
    ones: a job still in the pipeline is retryable or deletable, a DONE job has
    nothing to dismiss, a DELETED job is already closed.
    """
    if job.status is JobStatus.DISMISSED:
        return job
    if job.status not in ATTENTION_STATUSES:
        raise AppException(
            status_code=fastapi_status.HTTP_409_CONFLICT,
            code=ErrorCode.JOB_NOT_DISMISSABLE,
            detail=f"Job {job.id} is {job.status.value}; only a flagged job can be dismissed.",
        )
    dismissed = store.update_job(
        job.id,
        status=JobStatus.DISMISSED,
        dismissed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    watchlist.ignore_submission_for_job(job.id)
    app_logger.info("Dismissed job %s: %s", job.id, job.last_error)
    return dismissed
