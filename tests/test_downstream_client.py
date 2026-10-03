"""DownstreamClient error relay: chosen downstream codes keep their status and code."""

import pytest

from medialab_orchestrator.clients.base import relayed_error
from medialab_orchestrator.core.errors import ErrorCode

RELAYED = frozenset({ErrorCode.TMDB_UNAVAILABLE})
TMDB_BODY = {"status": "error", "code": "TMDB_UNAVAILABLE", "detail": "TMDB is unavailable."}


def test_relays_a_listed_code_with_the_downstream_status():
    error = relayed_error(503, TMDB_BODY, RELAYED)
    assert error is not None
    assert error.status_code == 503
    assert error.code is ErrorCode.TMDB_UNAVAILABLE
    assert error.detail == "TMDB is unavailable."


def test_ignores_an_unlisted_code():
    body = {"status": "error", "code": "INTERNAL_ERROR", "detail": "x"}
    assert relayed_error(500, body, RELAYED) is None


def test_ignores_an_unknown_code():
    body = {"status": "error", "code": "SOMETHING_NEW", "detail": "x"}
    assert relayed_error(500, body, RELAYED) is None


def test_ignores_a_non_error_body():
    assert relayed_error(503, "Service Unavailable", RELAYED) is None
    assert relayed_error(503, None, RELAYED) is None


def test_nothing_relayed_by_default():
    assert relayed_error(503, TMDB_BODY, frozenset()) is None


class TestHealthProbe:
    """``health()`` and the downloader's ``vpn_bound()`` never raise; a failed
    probe is simply no body, and no body is an unbound VPN."""

    @staticmethod
    def _client_with(handler):
        import httpx

        from medialab_orchestrator.clients.torrent_downloader import TorrentDownloaderClient

        client = TorrentDownloaderClient()
        client._base_url = "http://downloader.test"
        client._transport = httpx.MockTransport(handler)
        return client

    @pytest.mark.asyncio
    async def test_health_returns_the_body_on_200(self):
        import httpx

        body = {"status": "online", "vpn_interface_bound": True}
        client = self._client_with(lambda request: httpx.Response(200, json=body))
        assert await client.health() == body
        assert await client.is_reachable() is True
        assert await client.vpn_bound() is True

    @pytest.mark.asyncio
    async def test_non_200_is_unreachable_and_unbound(self):
        import httpx

        client = self._client_with(lambda request: httpx.Response(503, json={"status": "down"}))
        assert await client.health() is None
        assert await client.is_reachable() is False
        assert await client.vpn_bound() is False

    @pytest.mark.asyncio
    async def test_transport_error_is_unreachable_and_unbound(self):
        import httpx

        def boom(request):
            raise httpx.ConnectError("refused", request=request)

        client = self._client_with(boom)
        assert await client.health() is None
        assert await client.is_reachable() is False
        assert await client.vpn_bound() is False

    @pytest.mark.asyncio
    async def test_missing_flag_is_unbound(self):
        import httpx

        client = self._client_with(lambda request: httpx.Response(200, json={"status": "online"}))
        assert await client.vpn_bound() is False
