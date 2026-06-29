"""Canonical title/year resolution from TMDB (via torrent-downloader).

Shared by the download-submit path (so a job shows its title immediately) and
the worker's RESOLVE_META step. torrent-downloader's TMDB-detail endpoints wrap
the raw TMDB body under ``data``; this unwraps it and pulls the canonical title +
year, with movies and shows using TMDB's own field names.
"""

from __future__ import annotations

from typing import Any

from medialab_contracts import MediaType

from medialab_orchestrator.clients import TorrentDownloaderClient

_YEAR_LENGTH = 4


def extract_title_year(media_type: MediaType, detail_body: Any) -> tuple[str, int]:
    """Pull ``(title, year)`` from a torrent-downloader TMDB-detail response.

    ``detail_body`` is the full response (``{status, message, data}``); the raw
    TMDB dict is under ``data``. Returns ``("", 0)`` if the body is missing or
    has no usable fields, so a failed lookup degrades rather than raising.
    """
    data = detail_body.get("data") if isinstance(detail_body, dict) else None
    if not isinstance(data, dict):
        return "", 0
    if media_type is MediaType.MOVIE:
        title = data.get("title") or data.get("name") or ""
        date = data.get("release_date") or data.get("first_air_date") or ""
    else:
        title = data.get("name") or data.get("title") or ""
        date = data.get("first_air_date") or data.get("release_date") or ""
    year = int(date[:_YEAR_LENGTH]) if date[:_YEAR_LENGTH].isdigit() else 0
    return title, year


async def resolve_title_year(
    client: TorrentDownloaderClient, media_type: MediaType, tmdb_id: int
) -> tuple[str, int]:
    """Fetch TMDB detail and extract ``(title, year)``."""
    detail = await client.tmdb_detail(media_type, tmdb_id)
    return extract_title_year(media_type, detail)
