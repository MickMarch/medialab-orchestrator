"""Jellyfin Webhook plugin events: a valid one wakes the request poll.

Mounted without the gateway API key; the plugin sends ``JELLYFIN_WEBHOOK_SECRET``
in a header instead, compared in constant time. The body is never read: the
poll re-reads every allowed user's favorites, so the plugin's template does not
matter and a webhook and a poll for the same favorite file one request.
"""

import hmac

from fastapi import APIRouter, Depends, Request, Security
from fastapi import status as fastapi_status
from fastapi.security import APIKeyHeader

from medialab_orchestrator.core.config import config
from medialab_orchestrator.core.deps import AppContext, get_context
from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.core.limiter import RATE_LIMIT_DEFAULT, limiter
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.schemas.errors import ErrorResponse

WEBHOOK_SECRET_HEADER = "X-Medialab-Webhook-Secret"
ACCEPTED_BODY = {"status": "accepted"}

router = APIRouter(tags=["Webhooks"])
_secret_header = APIKeyHeader(name=WEBHOOK_SECRET_HEADER, auto_error=False)


def _secret_matches(given: str | None) -> bool:
    expected = config.jellyfin_webhook_secret
    if not expected or given is None:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


@router.post(
    "/jellyfin/events",
    status_code=fastapi_status.HTTP_202_ACCEPTED,
    summary="Jellyfin Webhook plugin event. Wakes the request poll.",
    responses={
        403: {"model": ErrorResponse, "description": "Missing, wrong or unconfigured secret."},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
    },
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def jellyfin_event(
    request: Request,
    secret: str | None = Security(_secret_header),
    ctx: AppContext = Depends(get_context),
) -> dict[str, str]:
    if not _secret_matches(secret):
        app_logger.warning("Jellyfin event rejected: missing or wrong webhook secret")
        raise AppException(
            status_code=fastapi_status.HTTP_403_FORBIDDEN,
            code=ErrorCode.UNAUTHORIZED,
            detail="Invalid webhook secret.",
        )
    if ctx.request_poller is not None:
        ctx.request_poller.wake()
    return ACCEPTED_BODY
