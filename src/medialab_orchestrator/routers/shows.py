"""Shows router: a show's seasons and episodes joined with library presence
and queued jobs. Creates no job."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi import status as fastapi_status
from medialab_contracts import ShowBrowseResponse

from medialab_orchestrator.core.deps import AppContext, get_context
from medialab_orchestrator.core.limiter import RATE_LIMIT_SEARCH, limiter
from medialab_orchestrator.schemas.errors import ErrorResponse
from medialab_orchestrator.services.shows import browse_show

router = APIRouter(prefix="/shows", tags=["Shows"])

_SHOWS_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
    502: {"model": ErrorResponse, "description": "Downstream worker unavailable."},
    503: {"model": ErrorResponse, "description": "TMDB unavailable (TMDB_UNAVAILABLE)."},
}


@router.get(
    "/{tmdb_id}",
    response_model=ShowBrowseResponse,
    status_code=fastapi_status.HTTP_200_OK,
    summary="A show's seasons and episodes with aired, in-library and queued-job state.",
    responses=_SHOWS_ERROR_RESPONSES,
)
@limiter.limit(RATE_LIMIT_SEARCH)
async def get_show(
    request: Request, tmdb_id: int, ctx: AppContext = Depends(get_context)
) -> ShowBrowseResponse:
    return await browse_show(
        tmdb_id,
        torrent=ctx.torrent,
        jellyfin=ctx.jellyfin,
        store=ctx.store,
        wishlist=ctx.wishlist,
    )
