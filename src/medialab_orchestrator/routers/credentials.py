"""Credentials router: the bot reports its Discord login result here."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as fastapi_status
from medialab_contracts import CredentialState

from medialab_orchestrator.core.deps import AppContext, get_context
from medialab_orchestrator.core.limiter import RATE_LIMIT_DEFAULT, limiter
from medialab_orchestrator.services.credentials import REPORTABLE_CREDENTIALS

router = APIRouter(tags=["Credentials"])


@router.post(
    "/credentials/{name}",
    status_code=fastapi_status.HTTP_204_NO_CONTENT,
    summary="Record a credential state a client observed (the bot's Discord login result).",
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def report_credential(
    request: Request,
    name: str,
    body: CredentialState,
    ctx: AppContext = Depends(get_context),
) -> None:
    if name not in REPORTABLE_CREDENTIALS:
        raise HTTPException(
            fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{name} is reported by its worker, not by clients",
        )
    if ctx.credentials is None:
        raise HTTPException(fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, "monitor not running")
    ctx.credentials.record_bot_report(body)
    await ctx.credentials.tick()
