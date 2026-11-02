"""Client for the medialab-jellyfin worker service."""

from __future__ import annotations

from typing import Any

from medialab_contracts import (
    API_PREFIX,
    FavoriteScope,
    JellyfinRequestEvent,
    LibraryEpisodesResponse,
    LibraryTmdbIdsResponse,
    MediaType,
    PlaceholdersResponse,
    PlaceholderWrite,
)
from pydantic import TypeAdapter

from medialab_orchestrator.clients.base import DownstreamClient
from medialab_orchestrator.core.config import config

_SCAN_UPDATE_TYPE_CREATED = "Created"
_FAVORITES = TypeAdapter(list[JellyfinRequestEvent])


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

    async def library_tmdb_ids(self, media_type: MediaType) -> LibraryTmdbIdsResponse:
        body = await self.get(
            f"{API_PREFIX}/library/tmdb-ids", params={"media_type": media_type.value}
        )
        return LibraryTmdbIdsResponse.model_validate(body)

    async def library_episodes(self, tmdb_id: int) -> LibraryEpisodesResponse:
        body = await self.get(f"{API_PREFIX}/library/episodes", params={"tmdb_id": tmdb_id})
        return LibraryEpisodesResponse.model_validate(body)

    async def put_placeholder(
        self, media_type: MediaType, tmdb_id: int, body: PlaceholderWrite, *, refresh: bool = True
    ) -> None:
        """Create or update one placeholder; unchanged is a no-op downstream.
        ``refresh=False`` batches writes before one ``refresh_discover``."""
        await self.request(
            "PUT",
            f"{API_PREFIX}/discover/placeholders/{media_type.value}/{tmdb_id}",
            params={"refresh": refresh},
            json=body.model_dump(mode="json"),
        )

    async def delete_placeholder(
        self, media_type: MediaType, tmdb_id: int, *, refresh: bool = True
    ) -> None:
        """Remove one placeholder; absent is not an error downstream."""
        await self.request(
            "DELETE",
            f"{API_PREFIX}/discover/placeholders/{media_type.value}/{tmdb_id}",
            params={"refresh": refresh},
        )

    async def placeholders(self, media_type: MediaType) -> PlaceholdersResponse:
        body = await self.get(
            f"{API_PREFIX}/discover/placeholders", params={"media_type": media_type.value}
        )
        return PlaceholdersResponse.model_validate(body)

    async def refresh_discover(self, media_type: MediaType) -> None:
        await self.request(
            "POST", f"{API_PREFIX}/discover/refresh", params={"media_type": media_type.value}
        )

    async def favorites(self, user_id: str, scope: FavoriteScope) -> list[JellyfinRequestEvent]:
        """One user's favorites on Discover placeholders (requests) or on real
        library items (recommendation seeds)."""
        body = await self.get(
            f"{API_PREFIX}/discover/favorites",
            params={"user_id": user_id, "scope": scope.value},
        )
        return _FAVORITES.validate_python(body)
