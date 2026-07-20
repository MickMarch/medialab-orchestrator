"""Client for the torrent-downloader worker service."""

from __future__ import annotations

from typing import Any

from medialab_contracts import MediaType, TorrentSearchScope

from medialab_orchestrator.clients.base import DownstreamClient
from medialab_orchestrator.core.config import config

_PREFIX = "/api/v1"


class TorrentDownloaderClient(DownstreamClient):
    """Wraps the torrent-downloader REST surface the orchestrator depends on."""

    def __init__(self) -> None:
        super().__init__(
            name="torrent-downloader",
            base_url=config.torrent_downloader_url or "",
            api_key=config.torrent_downloader_api_key,
        )

    async def search_tmdb(self, query: str) -> Any:
        return await self.get(f"{_PREFIX}/search/tmdb", params={"query": query})

    async def tmdb_detail(self, media_type: MediaType, tmdb_id: int) -> Any:
        return await self.get(f"{_PREFIX}/search/tmdb/{media_type.value}/{tmdb_id}")

    async def search_torrents(self, query: str, scope: TorrentSearchScope) -> Any:
        params: dict[str, Any] = {"query": query, "media_type": scope.media_type.value}
        if scope.season is not None:
            params["season"] = scope.season
        if scope.episode is not None:
            params["episode"] = scope.episode
        return await self.get(f"{_PREFIX}/search/torrents", params=params)

    async def download(self, *, source_url: str, media_type: MediaType, tmdb_id: int) -> Any:
        return await self.post(
            f"{_PREFIX}/download",
            json={
                "source_url": source_url,
                "media_type": media_type.value,
                "tmdb_id": tmdb_id,
            },
        )

    async def transfers(self) -> Any:
        return await self.get(f"{_PREFIX}/transfers")

    async def transfer_info(self, torrent_hash: str) -> Any:
        return await self.get(f"{_PREFIX}/transfers/{torrent_hash}/info")

    async def stop_seeding(self) -> Any:
        # torrent-downloader stops ALL seeding torrents; it takes no body. Safe
        # to repeat (stopping an already-stopped torrent is a no-op), so the
        # STOP_SEEDING step stays idempotent.
        return await self.post(f"{_PREFIX}/transfers/stop-seeding")

    async def storage(self) -> Any:
        return await self.get(f"{_PREFIX}/storage")
