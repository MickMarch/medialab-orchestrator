"""Search router: stateless proxies to torrent-downloader.

These create no job (a job is born at download submit). They exist only so the
bot has a single dependency - their value is gateway consistency, not state.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi import status as fastapi_status
from medialab_contracts import MediaType, TorrentSearchScope
from pydantic import ValidationError

from medialab_orchestrator.core.deps import AppContext, get_context
from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.core.limiter import RATE_LIMIT_SEARCH, limiter
from medialab_orchestrator.schemas.errors import ErrorResponse

router = APIRouter(tags=["Search"])

_SEARCH_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    422: {"model": ErrorResponse, "description": "Missing or invalid query parameter."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
    502: {"model": ErrorResponse, "description": "Downstream worker unavailable."},
}


@router.get(
    "/search/tmdb",
    status_code=fastapi_status.HTTP_200_OK,
    summary="TMDB multi-search (proxied to torrent-downloader).",
    responses=_SEARCH_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_SEARCH)
async def search_tmdb(request: Request, query: str, ctx: AppContext = Depends(get_context)) -> Any:
    return await ctx.torrent.search_tmdb(query)


@router.get(
    "/search/tmdb/{media_type}/{tmdb_id}",
    status_code=fastapi_status.HTTP_200_OK,
    summary="TMDB detail by id (proxied to torrent-downloader).",
    responses=_SEARCH_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_SEARCH)
async def search_tmdb_detail(
    request: Request,
    media_type: MediaType,
    tmdb_id: int,
    ctx: AppContext = Depends(get_context),
) -> Any:
    return await ctx.torrent.tmdb_detail(media_type, tmdb_id)


@router.get(
    "/search/torrents",
    status_code=fastapi_status.HTTP_200_OK,
    summary="Torrent search (proxied to torrent-downloader).",
    responses=_SEARCH_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_SEARCH)
async def search_torrents(
    request: Request,
    query: str,
    media_type: MediaType,
    season: int | None = None,
    episode: int | None = None,
    ctx: AppContext = Depends(get_context),
) -> Any:
    try:
        scope = TorrentSearchScope(media_type=media_type, season=season, episode=episode)
    except ValidationError as error:
        raise AppException(
            status_code=fastapi_status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ErrorCode.INVALID_INPUT,
            detail="Invalid season/episode combination for the requested media type.",
        ) from error
    return await ctx.torrent.search_torrents(query, scope)
