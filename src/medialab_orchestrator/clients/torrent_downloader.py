"""Client for the torrent-downloader worker service."""

from __future__ import annotations

from typing import Any

from medialab_contracts import (
    API_PREFIX,
    DiscoverResponse,
    GenresResponse,
    MediaType,
    SeriesEpisodesResponse,
    TorrentSearchScope,
)

from medialab_orchestrator.clients.base import DownstreamClient
from medialab_orchestrator.core.config import config
from medialab_orchestrator.core.errors import ErrorCode

# TMDB outages and out-of-range pages keep their meaning for the UI instead of
# collapsing into DOWNSTREAM_UNAVAILABLE.
_DISCOVER_RELAYED = frozenset({ErrorCode.TMDB_UNAVAILABLE, ErrorCode.INVALID_INPUT})


class TorrentDownloaderClient(DownstreamClient):
    """Wraps the torrent-downloader REST surface the orchestrator depends on."""

    def __init__(self) -> None:
        super().__init__(
            name="torrent-downloader",
            base_url=config.torrent_downloader_url or "",
            api_key=config.torrent_downloader_api_key,
        )

    async def search_tmdb(self, query: str) -> Any:
        return await self.get(f"{API_PREFIX}/search/tmdb", params={"query": query})

    async def tmdb_detail(self, media_type: MediaType, tmdb_id: int) -> Any:
        return await self.get(f"{API_PREFIX}/search/tmdb/{media_type.value}/{tmdb_id}")

    async def series_episodes(self, tmdb_id: int) -> SeriesEpisodesResponse:
        body = await self.get(
            f"{API_PREFIX}/search/tmdb/{MediaType.SHOW.value}/{tmdb_id}/episodes",
            relay=_DISCOVER_RELAYED,
        )
        return SeriesEpisodesResponse.model_validate(body)

    async def discover(
        self, media_type: MediaType, *, genre: int | None = None, page: int | None = None
    ) -> DiscoverResponse:
        params: dict[str, Any] = {}
        if genre is not None:
            params["genre"] = genre
        if page is not None:
            params["page"] = page
        body = await self.get(
            f"{API_PREFIX}/discover/{media_type.value}", params=params, relay=_DISCOVER_RELAYED
        )
        return DiscoverResponse.model_validate(body)

    async def discover_genres(self, media_type: MediaType) -> GenresResponse:
        body = await self.get(
            f"{API_PREFIX}/discover/{media_type.value}/genres", relay=_DISCOVER_RELAYED
        )
        return GenresResponse.model_validate(body)

    async def search_torrents(
        self, query: str, scope: TorrentSearchScope, *, alt_query: str | None = None
    ) -> Any:
        params: dict[str, Any] = {"query": query, "media_type": scope.media_type.value}
        if alt_query:
            params["alt_query"] = alt_query
        if scope.season is not None:
            params["season"] = scope.season
        if scope.episode is not None:
            params["episode"] = scope.episode
        return await self.get(f"{API_PREFIX}/search/torrents", params=params)

    async def download(self, *, source_url: str, media_type: MediaType, tmdb_id: int) -> Any:
        return await self.post(
            f"{API_PREFIX}/download",
            json={
                "source_url": source_url,
                "media_type": media_type.value,
                "tmdb_id": tmdb_id,
            },
        )

    async def settings(self) -> Any:
        return await self.get(f"{API_PREFIX}/settings")

    async def set_setting(self, key: str, value: Any) -> Any:
        return await self.put(f"{API_PREFIX}/settings/{key}", json={"value": value})

    async def reset_setting(self, key: str) -> Any:
        return await self.delete(f"{API_PREFIX}/settings/{key}", accept=(404, 422))

    async def clear_search_cache(self) -> Any:
        return await self.delete(f"{API_PREFIX}/cache")

    async def transfers(self) -> Any:
        return await self.get(f"{API_PREFIX}/transfers")

    async def transfer_info(self, torrent_hash: str) -> Any:
        # Informational: the downloader's diskcache entry for this hash. It is
        # gone after a cache wipe, so 404 returns None rather than failing.
        return await self.request(
            "GET", f"{API_PREFIX}/transfers/{torrent_hash}/info", accept=(404,)
        )

    async def stop_seeding(self) -> Any:
        # torrent-downloader stops ALL seeding torrents; it takes no body. Safe
        # to repeat (stopping an already-stopped torrent is a no-op), so the
        # STOP_SEEDING step stays idempotent.
        return await self.post(f"{API_PREFIX}/transfers/stop-seeding")

    async def resume_transfer(self, torrent_hash: str) -> Any:
        return await self.post(f"{API_PREFIX}/transfers/{torrent_hash}/resume")

    async def remove_transfer(self, torrent_hash: str, *, delete_files: bool = False) -> Any:
        # Drops qBittorrent's handle once the pipeline owns the files (or, for a
        # delete, the data too). A 404 means it is already gone, which is the
        # wanted state either way.
        path = f"{API_PREFIX}/transfers/{torrent_hash}"
        if delete_files:
            path += "?delete_files=true"
        return await self.delete(path, accept=(404,))
