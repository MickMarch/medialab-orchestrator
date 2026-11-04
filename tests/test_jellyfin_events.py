"""POST /jellyfin/events: the Webhook plugin's wake-up for the request poll."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from medialab_orchestrator.core import auth
from medialab_orchestrator.core.deps import AppContext
from medialab_orchestrator.routers import jellyfin_events
from medialab_orchestrator.routers.jellyfin_events import WEBHOOK_SECRET_HEADER
from medialab_orchestrator.services import requests as requests_module
from medialab_orchestrator.services.requests import RequestPoller

EVENTS_URL = "/api/v1/jellyfin/events"
SECRET = "webhook-secret"
UPLOAD = {"NotificationType": "UserDataSaved", "ItemId": "abc", "Favorite": "True"}


@pytest.fixture
def poller(context: AppContext) -> MagicMock:
    context.request_poller = MagicMock(spec=RequestPoller)
    return context.request_poller


@pytest.fixture
def with_secret(mocker) -> None:
    mocker.patch.object(jellyfin_events.config, "jellyfin_webhook_secret", SECRET)


class TestRoute:
    def test_valid_secret_wakes_the_poll(
        self, unauthed_client: TestClient, poller: MagicMock, with_secret: None
    ) -> None:
        response = unauthed_client.post(
            EVENTS_URL, json=UPLOAD, headers={WEBHOOK_SECRET_HEADER: SECRET}
        )
        assert response.status_code == 202
        poller.wake.assert_called_once_with()

    def test_needs_no_api_key_and_ignores_the_body(
        self, unauthed_client: TestClient, poller: MagicMock, with_secret: None
    ) -> None:
        response = unauthed_client.post(
            EVENTS_URL, content=b"not json", headers={WEBHOOK_SECRET_HEADER: SECRET}
        )
        assert response.status_code == 202
        poller.wake.assert_called_once_with()

    def test_wrong_secret_is_rejected(
        self, unauthed_client: TestClient, poller: MagicMock, with_secret: None
    ) -> None:
        response = unauthed_client.post(
            EVENTS_URL, json=UPLOAD, headers={WEBHOOK_SECRET_HEADER: "nope"}
        )
        assert response.status_code == 403
        assert response.json()["code"] == "UNAUTHORIZED"
        poller.wake.assert_not_called()

    def test_missing_secret_is_rejected(
        self, unauthed_client: TestClient, poller: MagicMock, with_secret: None
    ) -> None:
        assert unauthed_client.post(EVENTS_URL, json=UPLOAD).status_code == 403
        poller.wake.assert_not_called()

    def test_unconfigured_secret_rejects_everything(
        self, unauthed_client: TestClient, poller: MagicMock, mocker
    ) -> None:
        mocker.patch.object(jellyfin_events.config, "jellyfin_webhook_secret", None)
        response = unauthed_client.post(
            EVENTS_URL, json=UPLOAD, headers={WEBHOOK_SECRET_HEADER: ""}
        )
        assert response.status_code == 403
        poller.wake.assert_not_called()


class TestApiKeyCheck:
    async def test_compares_in_constant_time(self, mocker) -> None:
        mocker.patch.object(auth.config, "api_key", "k")
        digest = mocker.spy(auth.hmac, "compare_digest")
        await auth.verify_api_key("k")
        digest.assert_called_once()


class TestWake:
    @pytest.fixture
    def bare_poller(self) -> RequestPoller:
        return RequestPoller(
            store=MagicMock(),
            watchlist=MagicMock(),
            requests=MagicMock(),
            torrent_client=AsyncMock(),
            jellyfin_client=AsyncMock(),
            follow_poller=AsyncMock(),
        )

    async def test_a_wake_ends_the_wait_early(self, bare_poller: RequestPoller, mocker) -> None:
        mocker.patch.object(requests_module.config, "discover_poll_seconds", 3600)
        mocker.patch.object(requests_module, "WAKE_SETTLE_SECONDS", 0)
        ticked = asyncio.Event()

        async def tick() -> None:
            ticked.set()

        mocker.patch.object(bare_poller, "tick", tick)
        task = asyncio.create_task(bare_poller.run())
        await asyncio.sleep(0)
        bare_poller.wake()
        await asyncio.wait_for(ticked.wait(), timeout=1)
        task.cancel()

    async def test_a_burst_of_wakes_is_one_tick(self, bare_poller: RequestPoller, mocker) -> None:
        mocker.patch.object(requests_module.config, "discover_poll_seconds", 3600)
        mocker.patch.object(requests_module, "WAKE_SETTLE_SECONDS", 0.05)
        tick = mocker.patch.object(bare_poller, "tick", AsyncMock())
        task = asyncio.create_task(bare_poller.run())
        await asyncio.sleep(0)
        for _ in range(5):
            bare_poller.wake()
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.2)
        task.cancel()
        assert tick.await_count == 1

    async def test_a_wake_while_paused_does_not_tick(
        self, bare_poller: RequestPoller, mocker
    ) -> None:
        mocker.patch.object(requests_module.config, "discover_poll_seconds", 0)
        mocker.patch.object(requests_module, "PAUSED_POLL_RECHECK_SECONDS", 0.01)
        tick = mocker.patch.object(bare_poller, "tick", AsyncMock())
        task = asyncio.create_task(bare_poller.run())
        bare_poller.wake()
        await asyncio.sleep(0.1)
        task.cancel()
        tick.assert_not_awaited()
