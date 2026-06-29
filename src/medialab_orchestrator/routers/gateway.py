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

_MAGNET_HASH_BTIH = "btih:"
_HASH_LENGTH = 40

_COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
    502: {"model": ErrorResponse, "description": "Downstream worker unavailable."},
}


def _extract_hash(magnet_uri: str) -> str:
    """Pull the 40-char btih info-hash from a magnet URI, lowercased.

    The bot submits a magnet; the job is keyed by hash so the completion webhook
    (which only has the hash) can find it. Mirrors torrent-downloader's own
    extraction.
    """
    marker = magnet_uri.lower().find(_MAGNET_HASH_BTIH)
    if marker == -1:
        raise AppException(
            status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
            code=ErrorCode.INVALID_INPUT,
            detail="magnet_uri has no btih info-hash.",
        )
    start = marker + len(_MAGNET_HASH_BTIH)
    return magnet_uri[start : start + _HASH_LENGTH].lower()


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
    torrent_hash = _extract_hash(payload.magnet_uri)
    job = ctx.store.create_job(
        torrent_hash=torrent_hash,
        release_name="",  # filled from the completion webhook's %N
        media_type=payload.media_type,
        tmdb_id=payload.tmdb_id,
    )
    # Resolve the canonical title now (the tmdb_id is known) so /jobs shows
    # "Title (Year)" from submit, not just the hash. Best-effort: a metadata
    # hiccup must not block the actual download, and RESOLVE_META backfills it.
    try:
        title, year = await resolve_title_year(ctx.torrent, payload.media_type, payload.tmdb_id)
        if title:
            job = ctx.store.update_job(torrent_hash, resolved_title=title, resolved_year=year)
    except AppException as exc:
        app_logger.warning("Title resolve at submit failed for %s: %s", torrent_hash, exc.detail)

    await ctx.torrent.download(
        magnet_uri=payload.magnet_uri,
        media_type=payload.media_type,
        tmdb_id=payload.tmdb_id,
    )
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
    jobs = {job.torrent_hash: JobView.from_job(job) for job in ctx.store.list_jobs()}
    return {"status": "success", "transfers": live, "jobs": list(jobs.values())}


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
    "/jobs/{torrent_hash}",
    response_model=JobView,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Single job detail including last_error and attempts.",
    responses={**_COMMON_ERRORS, 404: {"model": ErrorResponse, "description": "No such job."}},
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_job(
    request: Request, torrent_hash: str, ctx: AppContext = Depends(get_context)
) -> JobView:
    try:
        job = ctx.store.get_job_by_hash(torrent_hash)
    except JobNotFoundError as exc:
        raise AppException(
            status_code=fastapi_status.HTTP_404_NOT_FOUND,
            code=ErrorCode.JOB_NOT_FOUND,
            detail=f"No job for hash {torrent_hash}.",
        ) from exc
    return JobView.from_job(job)


@router.post(
    "/jobs/{torrent_hash}/retry",
    response_model=JobView,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Re-enter the worker from the last good state.",
    responses={**_COMMON_ERRORS, 404: {"model": ErrorResponse, "description": "No such job."}},
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def retry_job(
    request: Request, torrent_hash: str, ctx: AppContext = Depends(get_context)
) -> JobView:
    try:
        ctx.store.get_job_by_hash(torrent_hash)
    except JobNotFoundError as exc:
        raise AppException(
            status_code=fastapi_status.HTTP_404_NOT_FOUND,
            code=ErrorCode.JOB_NOT_FOUND,
            detail=f"No job for hash {torrent_hash}.",
        ) from exc
    job = await ctx.worker.process(torrent_hash)
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
