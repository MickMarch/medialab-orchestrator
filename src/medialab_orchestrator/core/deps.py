"""Application context and FastAPI dependency providers.

A single ``AppContext`` holds the long-lived collaborators (job store, downstream
clients, worker). It is built at startup (FastAPI ``lifespan``) and stored on
``app.state``; route handlers pull it via the ``get_context`` dependency. Keeping
construction in one place lets tests build a context with mocked clients and an
in-memory store, then override the dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from medialab_orchestrator.clients import JellyfinClient, TorrentDownloaderClient
from medialab_orchestrator.core.config import config
from medialab_orchestrator.services.worker import PipelineWorker
from medialab_orchestrator.store import JobStore


@dataclass
class AppContext:
    store: JobStore
    torrent: TorrentDownloaderClient
    jellyfin: JellyfinClient
    worker: PipelineWorker


def build_context() -> AppContext:
    """Construct the production context from config."""
    store = JobStore(db_path=config.db_path)
    torrent = TorrentDownloaderClient()
    jellyfin = JellyfinClient()
    worker = PipelineWorker(store=store, torrent_client=torrent, jellyfin_client=jellyfin)
    return AppContext(store=store, torrent=torrent, jellyfin=jellyfin, worker=worker)


def get_context(request: Request) -> AppContext:
    """Dependency: the AppContext stashed on app.state at startup."""
    return request.app.state.context
