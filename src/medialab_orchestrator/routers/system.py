"""System router: aggregated health check. Public, no auth."""

import time

from fastapi import APIRouter, Depends, Request
from fastapi import status as fastapi_status
from medialab_contracts import CredentialState
from pydantic import BaseModel

from medialab_orchestrator.core.deps import AppContext, get_context
from medialab_orchestrator.core.limiter import limiter
from medialab_orchestrator.store import JobStatus

router = APIRouter(tags=["System"])

_START_TIME = time.time()


class DownstreamHealth(BaseModel):
    torrent_downloader: bool
    medialab_jellyfin: bool


class HealthResponse(BaseModel):
    status: str
    uptime_seconds: float
    downstream: DownstreamHealth
    needs_attention: int = 0
    # torrent-downloader's VPN assertion, relayed so clients can warn before a
    # download is confirmed. False whenever the downloader is unreachable.
    vpn_interface_bound: bool = False
    credentials: dict[str, CredentialState] = {}
    """Per-credential health across the workers and the bot; only ``invalid`` needs a human."""


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=fastapi_status.HTTP_200_OK,
    summary="Gateway health, reachability of both downstream workers, VPN binding.",
)
@limiter.exempt
async def health_check(request: Request, ctx: AppContext = Depends(get_context)) -> HealthResponse:
    """The bot's single cross-service health signal: gateway uptime + whether
    each downstream worker is reachable. Never raises - a down worker reports
    False, the gateway itself stays online."""
    torrent_ok = await ctx.torrent.is_reachable()
    jellyfin_ok = await ctx.jellyfin.is_reachable()
    vpn_bound = await ctx.torrent.vpn_bound() if torrent_ok else False
    credentials = await ctx.credentials.aggregate() if ctx.credentials is not None else {}
    return HealthResponse(
        status="online",
        uptime_seconds=round(time.time() - _START_TIME, 2),
        downstream=DownstreamHealth(torrent_downloader=torrent_ok, medialab_jellyfin=jellyfin_ok),
        needs_attention=len(ctx.store.list_jobs(status=JobStatus.NEEDS_ATTENTION)),
        vpn_interface_bound=vpn_bound,
        credentials=credentials,
    )
