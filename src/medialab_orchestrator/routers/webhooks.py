"""Webhook router: the post-download entry point.

``scripts/notify_complete.py`` (qBittorrent's completion hook child process)
posts here. The endpoint records the release name, kicks the worker, and returns
202 immediately so qBittorrent is never blocked. It is keyed like the rest of
the gateway (see impl decision: localhost is not a trust boundary in the compose
network).
"""

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi import status as fastapi_status
from medialab_contracts import MediaType

from medialab_orchestrator.core.deps import AppContext, get_context
from medialab_orchestrator.core.limiter import RATE_LIMIT_DEFAULT, limiter
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.schemas.errors import ErrorResponse
from medialab_orchestrator.schemas.jobs import WebhookPayload
from medialab_orchestrator.store import JobNotFoundError

router = APIRouter(tags=["Webhooks"])

# An orphan completion (no job for the hash, e.g. download predated the gateway)
# is still tracked. We cannot know its media_type/tmdb_id, so it is inserted and
# will fail at RESOLVE_META with a clear error for operator follow-up.
_ORPHAN_TMDB_ID = 0


@router.post(
    "/webhooks/torrent-complete",
    status_code=fastapi_status.HTTP_202_ACCEPTED,
    summary="qBittorrent completion event. Advances the matching job's pipeline.",
    responses={
        403: {"model": ErrorResponse, "description": "Missing or invalid API key."},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
    },
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def torrent_complete(
    request: Request,
    payload: WebhookPayload,
    background: BackgroundTasks,
    ctx: AppContext = Depends(get_context),
) -> dict[str, Any]:
    try:
        ctx.store.get_job_by_hash(payload.hash)
        ctx.store.update_job(payload.hash, release_name=payload.name)
    except JobNotFoundError:
        app_logger.warning("Completion for unknown hash %s; tracking as orphan job", payload.hash)
        ctx.store.create_job(
            torrent_hash=payload.hash,
            release_name=payload.name,
            media_type=MediaType.MOVIE,
            tmdb_id=_ORPHAN_TMDB_ID,
        )

    # Run the pipeline off the request so qBittorrent's hook returns immediately.
    background.add_task(ctx.worker.process, payload.hash)
    return {"status": "accepted", "hash": payload.hash.lower()}
