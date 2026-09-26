"""Client for the medialab-jellyfin worker service."""

from __future__ import annotations

from typing import Any

from medialab_contracts import API_PREFIX, MediaType

from medialab_orchestrator.clients.base import DownstreamClient
from medialab_orchestrator.core.config import config

_SCAN_UPDATE_TYPE_CREATED = "Created"


class JellyfinClient(DownstreamClient):
    """Wraps the medialab-jellyfin REST surface the orchestrator depends on."""

    def __init__(self) -> None:
        super().__init__(
            name="medialab-jellyfin",
            base_url=config.medialab_jellyfin_url or "",
            api_key=config.medialab_jellyfin_api_key,
        )

    async def register_path(self, *, media_type: MediaType, path: str) -> Any:
        # Idempotent: registering an already-known path is safe to repeat.
        return await self.post(
            f"{API_PREFIX}/library/paths",
            json={
                "media_type": media_type.value,
                "path": path,
                "refresh_library": False,
            },
        )

    async def scan(self, *, path: str, update_type: str = _SCAN_UPDATE_TYPE_CREATED) -> Any:
        # Idempotent: a repeat scan of the same path is safe.
        return await self.post(
            f"{API_PREFIX}/library/scan",
            json={"path": path, "update_type": update_type},
        )
