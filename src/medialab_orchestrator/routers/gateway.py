"""Gateway router: the stateful surface. Every endpoint here binds a job."""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi import status as fastapi_status

from medialab_orchestrator.core.config import config
from medialab_orchestrator.core.deps import AppContext, get_context
from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.core.limiter import RATE_LIMIT_DEFAULT, limiter
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.schemas.errors import ErrorResponse
from medialab_orchestrator.schemas.jobs import (
    DeletionPlanView,
    DiskUsageView,
    DownloadRequest,
    DownloadResponse,
    JobsResponse,
    JobView,
)
from medialab_orchestrator.services.deletion import DeletionService, plan_deletion
from medialab_orchestrator.services.metadata import resolve_title_year
from medialab_orchestrator.services.storage import disk_usage
from medialab_orchestrator.store import JobNotFoundError, JobStatus

router = APIRouter(tags=["Gateway"])

_RESPONSE_HASH_KEY = "torrent_hash"

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
    # The job is born keyed by a surrogate id; the real info-hash is not known
    # up front for a .torrent-URL source, so it is stamped from the downloader's
    # response below (or backfilled by the completion webhook).
    job = ctx.store.create_job(
        release_name="",  # filled from the completion webhook's %N
        media_type=payload.media_type,
        tmdb_id=payload.tmdb_id,
    )
    # Resolve the canonical title now (the tmdb_id is known) so /jobs shows
    # "Title (Year)" from submit. Best-effort: a metadata hiccup must not block
    # the actual download, and RESOLVE_META backfills it.
    try:
        title, year = await resolve_title_year(ctx.torrent, payload.media_type, payload.tmdb_id)
        if title:
            job = ctx.store.update_job(job.id, resolved_title=title, resolved_year=year)
    except AppException as exc:
        app_logger.warning("Title resolve at submit failed for %s: %s", job.id, exc.detail)

    result = await ctx.torrent.download(
        source_url=payload.source_url,
        media_type=payload.media_type,
        tmdb_id=payload.tmdb_id,
    )
    # The downloader resolves the hash (parsed from a magnet, or read back from
    # qBittorrent for a .torrent URL) and returns it. Stamp it so the completion
    # webhook can match this job; if it is missing the webhook backfills it.
    torrent_hash = result.get(_RESPONSE_HASH_KEY) if isinstance(result, dict) else None
    if torrent_hash:
        job = ctx.store.stamp_hash(job.id, torrent_hash)

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
    summary="The pipeline lifecycle view, optionally filtered by status.",
    responses=_COMMON_ERRORS,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def list_jobs(
    request: Request,
    ctx: AppContext = Depends(get_context),
    status: JobStatus | None = None,
) -> JobsResponse:
    jobs = ctx.store.list_jobs(status=status)
    return JobsResponse(jobs=[JobView.from_job(j) for j in jobs])


@router.get(
    "/jobs/{job_id}",
    response_model=JobView,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Single job detail including last_error and attempts.",
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
    return JobView.from_job(job)


def _job_or_404(ctx: AppContext, job_id: str):
    try:
        return ctx.store.get_job_by_id(job_id)
    except JobNotFoundError as exc:
        raise AppException(
            status_code=fastapi_status.HTTP_404_NOT_FOUND,
            code=ErrorCode.JOB_NOT_FOUND,
            detail=f"No job {job_id}.",
        ) from exc


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
    job = _job_or_404(ctx, job_id)
    service = DeletionService(
        store=ctx.store, torrent_client=ctx.torrent, jellyfin_client=ctx.jellyfin
    )
    return JobView.from_job(await service.execute(job))


@router.post(
    "/jobs/{job_id}/retry",
    response_model=JobView,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Re-enter the worker from the last good state.",
    responses={**_COMMON_ERRORS, 404: {"model": ErrorResponse, "description": "No such job."}},
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
