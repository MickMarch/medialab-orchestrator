"""Gateway router: the stateful surface. Every endpoint here binds a job."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi import status as fastapi_status

from medialab_orchestrator.core.deps import AppContext, get_context
from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.core.limiter import RATE_LIMIT_DEFAULT, limiter
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.schemas.errors import ErrorResponse
from medialab_orchestrator.schemas.jobs import (
    DownloadRequest,
    DownloadResponse,
    JobsResponse,
    JobView,
)
from medialab_orchestrator.services.metadata import resolve_title_year
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
    job = await ctx.worker.process(existing.torrent_hash)
    return JobView.from_job(job)


@router.get(
    "/storage",
    status_code=fastapi_status.HTTP_200_OK,
    summary="Disk usage (proxied to torrent-downloader).",
    responses=_COMMON_ERRORS,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_storage(request: Request, ctx: AppContext = Depends(get_context)) -> Any:
    return await ctx.torrent.storage()
