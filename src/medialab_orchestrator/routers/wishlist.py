"""Wishlist router: one shared list of titles saved for later. PUT and DELETE are
idempotent; a finished download removes its title (see the pipeline worker)."""

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi import status as fastapi_status
from medialab_contracts import MediaType, WishlistAddRequest, WishlistItem, WishlistResponse

from medialab_orchestrator.core.deps import AppContext, get_context
from medialab_orchestrator.core.limiter import RATE_LIMIT_DEFAULT, limiter
from medialab_orchestrator.schemas.errors import ErrorResponse
from medialab_orchestrator.services.discover import annotate_wishlist

router = APIRouter(prefix="/wishlist", tags=["Wishlist"])

_WISHLIST_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    422: {"model": ErrorResponse, "description": "Invalid media type, id or body."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
}


@router.get(
    "",
    response_model=WishlistResponse,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Every wishlist item, newest first, with library flags.",
    responses=_WISHLIST_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def list_wishlist(
    request: Request,
    media_type: MediaType | None = None,
    ctx: AppContext = Depends(get_context),
) -> WishlistResponse:
    items = ctx.wishlist.list(media_type)
    return WishlistResponse(items=await annotate_wishlist(items, ctx.jellyfin))


@router.put(
    "/{media_type}/{tmdb_id}",
    response_model=WishlistItem,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Add a title to the wishlist. Idempotent; a repeat keeps the original added_at.",
    responses=_WISHLIST_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def add_to_wishlist(
    request: Request,
    media_type: MediaType,
    tmdb_id: int,
    payload: WishlistAddRequest,
    ctx: AppContext = Depends(get_context),
) -> WishlistItem:
    return ctx.wishlist.add(media_type, tmdb_id, payload)


@router.delete(
    "/{media_type}/{tmdb_id}",
    status_code=fastapi_status.HTTP_204_NO_CONTENT,
    summary="Remove a title from the wishlist. Idempotent; 204 even when absent.",
    responses=_WISHLIST_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def remove_from_wishlist(
    request: Request,
    media_type: MediaType,
    tmdb_id: int,
    ctx: AppContext = Depends(get_context),
) -> Response:
    ctx.wishlist.remove(media_type, tmdb_id)
    return Response(status_code=fastapi_status.HTTP_204_NO_CONTENT)
