"""Gateway router: the stateful surface. Every endpoint here binds a job."""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi import status as fastapi_status

from medialab_orchestrator.core.config import config
from medialab_orchestrator.core.deps import AppContext, get_context
from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.core.limiter import RATE_LIMIT_DEFAULT, limiter
from medialab_orchestrator.schemas.errors import ErrorResponse
from medialab_orchestrator.schemas.jobs import (
    BulkDeleteView,
    BulkDeletionPlanView,
    BulkDismissView,
    BulkJobsRequest,
    DeletionPlanView,
    DiskUsageView,
    DownloadRequest,
    DownloadResponse,
    JobDeleteResultView,
    JobDeletionPlanView,
    JobDismissResultView,
    JobsResponse,
    JobView,
)
from medialab_orchestrator.services.deletion import (
    UNKNOWN_JOB_REFUSAL,
    DeletionService,
    plan_deletion,
    refused_plan,
)
from medialab_orchestrator.services.dismiss import dismiss_job
from medialab_orchestrator.services.download import create_submitted_job, submit_job
from medialab_orchestrator.services.progress import with_progress
from medialab_orchestrator.services.redo import attach_redone_by, redo_job
from medialab_orchestrator.services.storage import disk_usage
from medialab_orchestrator.store import JobNotFoundError, JobStatus, PipelineJob

router = APIRouter(tags=["Gateway"])

_CLOSED = frozenset({JobStatus.DELETED, JobStatus.DISMISSED})
"""Statuses a human already closed; retry has nothing to re-enter."""

_COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
    502: {"model": ErrorResponse, "description": "Downstream worker unavailable."},
}


@router.post(
    "/download",
    response_model=DownloadResponse,
    status_code=fastapi_status.HTTP_202_ACCEPTED,
    summary="Submit a download. Creates a pipeline job and forwards to torrent-downloader.",
    responses={**_COMMON_ERRORS, 422: {"model": ErrorResponse, "description": "Invalid body."}},
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def submit_download(
    request: Request, payload: DownloadRequest, ctx: AppContext = Depends(get_context)
) -> DownloadResponse:
    job = create_submitted_job(ctx.store, payload)
    job = await submit_job(job, payload, store=ctx.store, torrent=ctx.torrent)
    return DownloadResponse(job=JobView.from_job(job))


@router.get(
    "/transfers",
    status_code=fastapi_status.HTTP_200_OK,
    summary="Live transfer state merged with pipeline job rows.",
    responses=_COMMON_ERRORS,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def list_transfers(request: Request, ctx: AppContext = Depends(get_context)) -> Any:
    """Read-through: one downstream read of live transfers, merged with jobs.

    No polling - this is the live read; the completion webhook is what advances
    the pipeline.
    """
    live = await ctx.torrent.transfers()
    jobs = [JobView.from_job(job) for job in ctx.store.list_jobs()]
    return {"status": "success", "transfers": live, "jobs": jobs}


@router.get(
    "/jobs",
    response_model=JobsResponse,
    status_code=fastapi_status.HTTP_200_OK,
    summary="The pipeline lifecycle view, optionally filtered by status, with live progress.",
    responses=_COMMON_ERRORS,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def list_jobs(
    request: Request,
    ctx: AppContext = Depends(get_context),
    status: JobStatus | None = None,
) -> JobsResponse:
    jobs = ctx.store.list_jobs(status=status)
    views = await with_progress(jobs, store=ctx.store, torrent=ctx.torrent)
    return JobsResponse(jobs=attach_redone_by(views, store=ctx.store))


@router.get(
    "/jobs/{job_id}",
    response_model=JobView,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Single job detail including last_error, attempts and live progress.",
    responses={**_COMMON_ERRORS, 404: {"model": ErrorResponse, "description": "No such job."}},
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_job(request: Request, job_id: str, ctx: AppContext = Depends(get_context)) -> JobView:
    try:
        job = ctx.store.get_job_by_id(job_id)
    except JobNotFoundError as exc:
        raise AppException(
            status_code=fastapi_status.HTTP_404_NOT_FOUND,
            code=ErrorCode.JOB_NOT_FOUND,
            detail=f"No job {job_id}.",
        ) from exc
    views = await with_progress([job], store=ctx.store, torrent=ctx.torrent)
    [view] = attach_redone_by(views, store=ctx.store)
    return view


def _job_or_404(ctx: AppContext, job_id: str):
    try:
        return ctx.store.get_job_by_id(job_id)
    except JobNotFoundError as exc:
        raise AppException(
            status_code=fastapi_status.HTTP_404_NOT_FOUND,
            code=ErrorCode.JOB_NOT_FOUND,
            detail=f"No job {job_id}.",
        ) from exc


def _deletion_service(ctx: AppContext) -> DeletionService:
    return DeletionService(
        store=ctx.store, torrent_client=ctx.torrent, jellyfin_client=ctx.jellyfin
    )


async def _delete_job(ctx: AppContext, job: PipelineJob) -> PipelineJob:
    """The one delete path: execute the plan, then keep a follow from
    re-queueing the episode."""
    deleted = await _deletion_service(ctx).execute(job)
    ctx.watchlist.ignore_submission_for_job(deleted.id)
    return deleted


@router.post(
    "/jobs/deletion-plan",
    response_model=BulkDeletionPlanView,
    status_code=fastapi_status.HTTP_200_OK,
    summary="What deleting each of these jobs would remove, in request order. No side effects.",
    responses={**_COMMON_ERRORS, 422: {"model": ErrorResponse, "description": "Invalid body."}},
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def bulk_deletion_plan(
    request: Request, payload: BulkJobsRequest, ctx: AppContext = Depends(get_context)
) -> BulkDeletionPlanView:
    plans: list[JobDeletionPlanView] = []
    for job_id in payload.unique_ids():
        try:
            job = ctx.store.get_job_by_id(job_id)
        except JobNotFoundError:
            plan = refused_plan(UNKNOWN_JOB_REFUSAL)
            plans.append(
                JobDeletionPlanView(job=None, plan=DeletionPlanView(job_id=job_id, **plan.__dict__))
            )
            continue
        plan = plan_deletion(job)
        plans.append(
            JobDeletionPlanView(
                job=JobView.from_job(job), plan=DeletionPlanView(job_id=job_id, **plan.__dict__)
            )
        )
    return BulkDeletionPlanView(plans=plans)


@router.post(
    "/jobs/delete",
    response_model=BulkDeleteView,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Delete each of these jobs as DELETE /jobs/{id} would; one result per id, never fatal.",
    responses={**_COMMON_ERRORS, 422: {"model": ErrorResponse, "description": "Invalid body."}},
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def bulk_delete(
    request: Request, payload: BulkJobsRequest, ctx: AppContext = Depends(get_context)
) -> BulkDeleteView:
    results: list[JobDeleteResultView] = []
    for job_id in payload.unique_ids():
        try:
            job = ctx.store.get_job_by_id(job_id)
        except JobNotFoundError:
            results.append(JobDeleteResultView(job_id=job_id, job=None, error=UNKNOWN_JOB_REFUSAL))
            continue
        try:
            deleted = await _delete_job(ctx, job)
        except AppException as exc:
            results.append(
                JobDeleteResultView(job_id=job_id, job=JobView.from_job(job), error=exc.detail)
            )
        except Exception as exc:  # noqa: BLE001 - one job's failure must not stop the batch
            results.append(
                JobDeleteResultView(
                    job_id=job_id,
                    job=JobView.from_job(ctx.store.get_job_by_id(job_id)),
                    error=str(exc),
                )
            )
        else:
            results.append(JobDeleteResultView(job_id=job_id, job=JobView.from_job(deleted)))
    return BulkDeleteView(results=results)


@router.post(
    "/jobs/dismiss",
    response_model=BulkDismissView,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Dismiss each of these jobs as POST /jobs/{id}/dismiss would; one result per id.",
    responses={**_COMMON_ERRORS, 422: {"model": ErrorResponse, "description": "Invalid body."}},
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def bulk_dismiss(
    request: Request, payload: BulkJobsRequest, ctx: AppContext = Depends(get_context)
) -> BulkDismissView:
    results: list[JobDismissResultView] = []
    for job_id in payload.unique_ids():
        try:
            job = ctx.store.get_job_by_id(job_id)
        except JobNotFoundError:
            results.append(JobDismissResultView(job_id=job_id, job=None, error=UNKNOWN_JOB_REFUSAL))
            continue
        try:
            dismissed = dismiss_job(job, store=ctx.store, watchlist=ctx.watchlist)
        except AppException as exc:
            results.append(
                JobDismissResultView(job_id=job_id, job=JobView.from_job(job), error=exc.detail)
            )
        else:
            results.append(JobDismissResultView(job_id=job_id, job=JobView.from_job(dismissed)))
    return BulkDismissView(results=results)


@router.post(
    "/jobs/{job_id}/dismiss",
    response_model=JobView,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Close a flagged job without touching its files; a human judged it not worth pursuing.",
    responses={
        **_COMMON_ERRORS,
        404: {"model": ErrorResponse, "description": "No such job."},
        409: {"model": ErrorResponse, "description": "Job is not FAILED or NEEDS_ATTENTION."},
    },
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def dismiss(request: Request, job_id: str, ctx: AppContext = Depends(get_context)) -> JobView:
    job = dismiss_job(_job_or_404(ctx, job_id), store=ctx.store, watchlist=ctx.watchlist)
    return JobView.from_job(job)


@router.get(
    "/jobs/{job_id}/deletion-plan",
    response_model=DeletionPlanView,
    status_code=fastapi_status.HTTP_200_OK,
    summary="What deleting this job would remove. No side effects.",
    responses={**_COMMON_ERRORS, 404: {"model": ErrorResponse, "description": "No such job."}},
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def deletion_plan(
    request: Request, job_id: str, ctx: AppContext = Depends(get_context)
) -> DeletionPlanView:
    plan = plan_deletion(_job_or_404(ctx, job_id))
    return DeletionPlanView(job_id=job_id, **plan.__dict__)


@router.delete(
    "/jobs/{job_id}",
    response_model=JobView,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Undo a download: torrent, files, placed library files, Jellyfin. Job marked DELETED.",
    responses={
        **_COMMON_ERRORS,
        404: {"model": ErrorResponse, "description": "No such job."},
        409: {
            "model": ErrorResponse,
            "description": "Refused; the reason says what to do by hand.",
        },
    },
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def delete_job(
    request: Request, job_id: str, ctx: AppContext = Depends(get_context)
) -> JobView:
    return JobView.from_job(await _delete_job(ctx, _job_or_404(ctx, job_id)))


@router.post(
    "/jobs/{job_id}/redo",
    response_model=DownloadResponse,
    status_code=fastapi_status.HTTP_202_ACCEPTED,
    summary="Replace a DONE job: create the replacement, delete the original, submit the new.",
    responses={
        **_COMMON_ERRORS,
        404: {"model": ErrorResponse, "description": "No such job."},
        409: {
            "model": ErrorResponse,
            "description": "Job is not DONE, or its deletion plan is refused.",
        },
        422: {
            "model": ErrorResponse,
            "description": "Invalid body, or media_type/tmdb_id differ from the job's.",
        },
        502: {
            "model": ErrorResponse,
            "description": "Deleting the original failed; the replacement row is kept "
            "for the next attempt.",
        },
    },
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def redo_download(
    request: Request, job_id: str, payload: DownloadRequest, ctx: AppContext = Depends(get_context)
) -> DownloadResponse:
    old = _job_or_404(ctx, job_id)
    job = await redo_job(
        old,
        payload,
        store=ctx.store,
        watchlist=ctx.watchlist,
        torrent=ctx.torrent,
        jellyfin=ctx.jellyfin,
    )
    return DownloadResponse(job=JobView.from_job(job))


@router.post(
    "/jobs/{job_id}/retry",
    response_model=JobView,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Re-enter the worker from the last good state.",
    responses={
        **_COMMON_ERRORS,
        404: {"model": ErrorResponse, "description": "No such job."},
        409: {"model": ErrorResponse, "description": "Job is closed, or has no hash yet."},
    },
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def retry_job(
    request: Request, job_id: str, ctx: AppContext = Depends(get_context)
) -> JobView:
    try:
        existing = ctx.store.get_job_by_id(job_id)
    except JobNotFoundError as exc:
        raise AppException(
            status_code=fastapi_status.HTTP_404_NOT_FOUND,
            code=ErrorCode.JOB_NOT_FOUND,
            detail=f"No job {job_id}.",
        ) from exc
    if existing.status in _CLOSED:
        raise AppException(
            status_code=fastapi_status.HTTP_409_CONFLICT,
            code=ErrorCode.JOB_NOT_RETRYABLE,
            detail=f"Job {job_id} is {existing.status.value}; a closed job cannot be retried.",
        )
    if existing.torrent_hash is None:
        # The pipeline needs the info-hash (transfer_info, stop-seeding). A job
        # whose hash never got stamped cannot be advanced; the completion webhook
        # is what stamps + drives it.
        raise AppException(
            status_code=fastapi_status.HTTP_409_CONFLICT,
            code=ErrorCode.INVALID_INPUT,
            detail=f"Job {job_id} has no torrent hash yet; cannot retry.",
        )
    # A human retry restarts the automatic budgets the health poll spends.
    ctx.store.update_job(existing.id, attempts=0, remediations=0)
    job = await ctx.worker.process(existing.torrent_hash)
    return JobView.from_job(job)


@router.post(
    "/transfers/stop-seeding",
    status_code=fastapi_status.HTTP_202_ACCEPTED,
    summary="Pause every seeding (completed) torrent. Proxied to torrent-downloader.",
    responses=_COMMON_ERRORS,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def stop_seeding(request: Request, ctx: AppContext = Depends(get_context)) -> Any:
    # A user action on the torrent client, not a pipeline transition: no job is
    # created or touched. In-progress downloads are never affected downstream.
    return await ctx.torrent.stop_seeding()


@router.get(
    "/storage",
    response_model=DiskUsageView,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Disk usage of the media mount.",
    responses={
        **_COMMON_ERRORS,
        500: {"model": ErrorResponse, "description": "Media mount missing or unreadable."},
    },
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_storage(request: Request) -> DiskUsageView:
    # Measured here, not proxied: torrent-downloader has no media mount and
    # qBittorrent's host path means nothing inside a container.
    try:
        return disk_usage(Path(config.media_mount_path))
    except OSError as err:
        raise AppException(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR,
            detail=f"Disk usage check failed for {config.media_mount_path}.",
        ) from err
