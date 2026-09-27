"""Watchlist router: one shared list of saved titles and followed shows. PUT and
DELETE are idempotent; a finished download removes a saved title but never a
follow (see the pipeline worker). A follow needs the saved row first: the UI
saves, then follows, two calls."""

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi import status as fastapi_status
from medialab_contracts import (
    FollowRequest,
    MediaType,
    WatchlistAddRequest,
    WatchlistItem,
    WatchlistKind,
    WatchlistResponse,
)

from medialab_orchestrator.core.deps import AppContext, get_context
from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.core.limiter import RATE_LIMIT_DEFAULT, limiter
from medialab_orchestrator.schemas.errors import ErrorResponse
from medialab_orchestrator.services.discover import annotate_watchlist
from medialab_orchestrator.store import WatchlistItemNotFoundError

router = APIRouter(prefix="/watchlist", tags=["Watchlist"])

_WATCHLIST_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    422: {"model": ErrorResponse, "description": "Invalid media type, id or body."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
}

_FOLLOW_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_WATCHLIST_ERROR_RESPONSES,
    404: {"model": ErrorResponse, "description": "The show is not on the watchlist."},
    422: {"model": ErrorResponse, "description": "Only a show can be followed; invalid body."},
}


def _require_show(media_type: MediaType) -> None:
    if media_type is not MediaType.SHOW:
        raise AppException(
            status_code=fastapi_status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ErrorCode.INVALID_INPUT,
            detail="Only a show can be followed.",
        )


def _not_found(exc: WatchlistItemNotFoundError) -> AppException:
    return AppException(
        status_code=fastapi_status.HTTP_404_NOT_FOUND,
        code=ErrorCode.WATCHLIST_ITEM_NOT_FOUND,
        detail=str(exc),
    )


@router.get(
    "",
    response_model=WatchlistResponse,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Every watchlist item, newest first, with library flags.",
    responses=_WATCHLIST_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def list_watchlist(
    request: Request,
    media_type: MediaType | None = None,
    kind: WatchlistKind | None = None,
    ctx: AppContext = Depends(get_context),
) -> WatchlistResponse:
    items = ctx.watchlist.list(media_type, kind)
    return WatchlistResponse(items=await annotate_watchlist(items, ctx.jellyfin))


@router.put(
    "/{media_type}/{tmdb_id}",
    response_model=WatchlistItem,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Save a title. Idempotent; a repeat keeps the original added_at and any follow.",
    responses=_WATCHLIST_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def add_to_watchlist(
    request: Request,
    media_type: MediaType,
    tmdb_id: int,
    payload: WatchlistAddRequest,
    ctx: AppContext = Depends(get_context),
) -> WatchlistItem:
    return ctx.watchlist.add(media_type, tmdb_id, payload)


@router.delete(
    "/{media_type}/{tmdb_id}",
    status_code=fastapi_status.HTTP_204_NO_CONTENT,
    summary="Remove a title, saved or followed. Idempotent; 204 even when absent.",
    responses=_WATCHLIST_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def remove_from_watchlist(
    request: Request,
    media_type: MediaType,
    tmdb_id: int,
    ctx: AppContext = Depends(get_context),
) -> Response:
    ctx.watchlist.remove(media_type, tmdb_id)
    return Response(status_code=fastapi_status.HTTP_204_NO_CONTENT)


@router.put(
    "/{media_type}/{tmdb_id}/follow",
    response_model=WatchlistItem,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Follow a saved show from a start point. Idempotent; the show must be saved first.",
    responses=_FOLLOW_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def follow_show(
    request: Request,
    media_type: MediaType,
    tmdb_id: int,
    payload: FollowRequest,
    ctx: AppContext = Depends(get_context),
) -> WatchlistItem:
    _require_show(media_type)
    try:
        return ctx.watchlist.follow(tmdb_id, payload)
    except WatchlistItemNotFoundError as exc:
        raise _not_found(exc) from exc


@router.delete(
    "/{media_type}/{tmdb_id}/follow",
    status_code=fastapi_status.HTTP_204_NO_CONTENT,
    summary="Unfollow: the show stays saved. Idempotent; 204 even when absent.",
    responses=_FOLLOW_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def unfollow_show(
    request: Request,
    media_type: MediaType,
    tmdb_id: int,
    ctx: AppContext = Depends(get_context),
) -> Response:
    _require_show(media_type)
    ctx.watchlist.unfollow(tmdb_id)
    return Response(status_code=fastapi_status.HTTP_204_NO_CONTENT)


@router.post(
    "/{media_type}/{tmdb_id}/follow/pause",
    response_model=WatchlistItem,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Pause a follow: the poll skips it until resumed.",
    responses=_FOLLOW_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def pause_follow(
    request: Request,
    media_type: MediaType,
    tmdb_id: int,
    ctx: AppContext = Depends(get_context),
) -> WatchlistItem:
    return _set_paused(ctx, media_type, tmdb_id, paused=True)


@router.post(
    "/{media_type}/{tmdb_id}/follow/resume",
    response_model=WatchlistItem,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Resume a paused follow.",
    responses=_FOLLOW_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def resume_follow(
    request: Request,
    media_type: MediaType,
    tmdb_id: int,
    ctx: AppContext = Depends(get_context),
) -> WatchlistItem:
    return _set_paused(ctx, media_type, tmdb_id, paused=False)


def _set_paused(
    ctx: AppContext, media_type: MediaType, tmdb_id: int, *, paused: bool
) -> WatchlistItem:
    _require_show(media_type)
    try:
        return ctx.watchlist.set_paused(tmdb_id, paused)
    except WatchlistItemNotFoundError as exc:
        raise _not_found(exc) from exc
