"""Shared pytest fixtures.

CI isolation: tests use an in-memory SQLite DB and mock both downstream clients
at the client boundary. No ``.env``, no network. The ``app_client`` fixture
overrides the ``get_context`` dependency so the FastAPI app runs against the test
context (in-memory store + AsyncMock clients) with no real lifespan startup.
"""

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from medialab_orchestrator.clients import JellyfinClient, TorrentDownloaderClient
from medialab_orchestrator.core import auth, deps
from medialab_orchestrator.core.deps import AppContext, get_context
from medialab_orchestrator.services.worker import PipelineWorker
from medialab_orchestrator.store import JobStore

TEST_API_KEY = "test-api-key"


@pytest.fixture
def store() -> JobStore:
    """A fresh in-memory job store, isolated per test."""
    return JobStore(db_path=":memory:")


@pytest.fixture
def torrent_client() -> AsyncMock:
    return AsyncMock(spec=TorrentDownloaderClient)


@pytest.fixture
def jellyfin_client() -> AsyncMock:
    return AsyncMock(spec=JellyfinClient)


@pytest.fixture
def context(store: JobStore, torrent_client: AsyncMock, jellyfin_client: AsyncMock) -> AppContext:
    worker = PipelineWorker(
        store=store, torrent_client=torrent_client, jellyfin_client=jellyfin_client
    )
    return AppContext(store=store, torrent=torrent_client, jellyfin=jellyfin_client, worker=worker)


@pytest.fixture
def app_client(context: AppContext, mocker) -> Iterator[TestClient]:
    """A TestClient wired to the test context, authenticated by default.

    Patches the auth config so ``X-API-Key`` matches ``TEST_API_KEY``, overrides
    ``get_context`` to return the test context, and disables the real lifespan so
    no production context is built. The ``store`` it shares is the same instance
    the test injected, so assertions can read job rows directly.
    """
    mocker.patch.object(auth.config, "api_key", TEST_API_KEY)
    mocker.patch.object(deps, "build_context", return_value=context)

    from medialab_orchestrator.main import app

    app.dependency_overrides[get_context] = lambda: context
    with TestClient(app, headers={"X-API-Key": TEST_API_KEY}) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def unauthed_client(context: AppContext, mocker) -> Iterator[TestClient]:
    mocker.patch.object(auth.config, "api_key", TEST_API_KEY)
    mocker.patch.object(deps, "build_context", return_value=context)

    from medialab_orchestrator.main import app

    app.dependency_overrides[get_context] = lambda: context
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
