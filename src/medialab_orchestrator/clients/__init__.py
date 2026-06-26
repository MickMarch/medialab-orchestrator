"""Async HTTP clients for downstream worker services."""

from medialab_orchestrator.clients.base import DownstreamClient
from medialab_orchestrator.clients.jellyfin import JellyfinClient
from medialab_orchestrator.clients.torrent_downloader import TorrentDownloaderClient

__all__ = [
    "DownstreamClient",
    "JellyfinClient",
    "TorrentDownloaderClient",
]
