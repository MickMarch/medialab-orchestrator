"""Discover router: trending and popular-by-genre titles proxied from
torrent-downloader, annotated with wishlist and library state. Creates no job."""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi import status as fastapi_status
from medialab_contracts import DiscoverResponse, GenresResponse, MediaType

from medialab_orchestrator.core.deps import AppContext, get_context
from medialab_orchestrator.core.limiter import RATE_LIMIT_SEARCH, limiter
from medialab_orchestrator.schemas.errors import ErrorResponse
from medialab_orchestrator.services.discover import annotate_discover, library_tmdb_ids

router = APIRouter(prefix="/discover", tags=["Discover"])

_FIRST_PAGE = 1

_DISCOVER_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    422: {"model": ErrorResponse, "description": "Invalid media type or query parameter."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
    502: {"model": ErrorResponse, "description": "Downstream worker unavailable."},
    503: {"model": ErrorResponse, "description": "TMDB unavailable (TMDB_UNAVAILABLE)."},
}


@router.get(
    "/{media_type}",
    response_model=DiscoverResponse,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Trending titles, or the most popular in a genre, with wishlist and library flags.",
    responses=_DISCOVER_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_SEARCH)
async def discover(
    request: Request,
    media_type: MediaType,
    genre: int | None = None,
    page: int = Query(default=_FIRST_PAGE, ge=_FIRST_PAGE),
    ctx: AppContext = Depends(get_context),
) -> DiscoverResponse:
    response = await ctx.torrent.discover(media_type, genre=genre, page=page)
    return annotate_discover(
        response,
        media_type=media_type,
        wishlisted=ctx.wishlist.keys(media_type),
        in_library=await library_tmdb_ids(ctx.jellyfin, media_type),
    )


@router.get(
    "/{media_type}/genres",
    response_model=GenresResponse,
    status_code=fastapi_status.HTTP_200_OK,
    summary="TMDB genre list for the media type (proxied to torrent-downloader).",
    responses=_DISCOVER_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_SEARCH)
async def discover_genres(
    request: Request, media_type: MediaType, ctx: AppContext = Depends(get_context)
) -> GenresResponse:
    return await ctx.torrent.discover_genres(media_type)
