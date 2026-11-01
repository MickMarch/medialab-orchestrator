"""DownstreamClient error relay: chosen downstream codes keep their status and code."""

import pytest
from medialab_contracts import MediaType

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


class TestDownloadRelay:
    """``download()`` keeps the downloader's retryable 503 so the UI can say
    "source unreachable, try again"; every other failure stays a 502."""

    @staticmethod
    def _client_with(handler):
        import httpx

        from medialab_orchestrator.clients.torrent_downloader import TorrentDownloaderClient

        client = TorrentDownloaderClient()
        client._base_url = "http://downloader.test"
        client._transport = httpx.MockTransport(handler)
        return client

    @pytest.mark.asyncio
    async def test_source_unreachable_keeps_status_and_code(self):
        import httpx

        from medialab_orchestrator.core.errors import AppException

        body = {
            "status": "error",
            "code": "SOURCE_UNREACHABLE",
            "detail": "The source page could not be reached; the request can be retried.",
        }
        client = self._client_with(lambda request: httpx.Response(503, json=body))
        with pytest.raises(AppException) as excinfo:
            await client.download(
                source_url="https://x.test/page.html", media_type=MediaType.MOVIE, tmdb_id=1
            )
        assert excinfo.value.status_code == 503
        assert excinfo.value.code is ErrorCode.SOURCE_UNREACHABLE
        assert excinfo.value.detail == body["detail"]

    @pytest.mark.asyncio
    async def test_no_magnet_422_stays_downstream_unavailable(self):
        import httpx

        from medialab_orchestrator.core.errors import AppException

        body = {"status": "error", "code": "INVALID_INPUT", "detail": "no magnet"}
        client = self._client_with(lambda request: httpx.Response(422, json=body))
        with pytest.raises(AppException) as excinfo:
            await client.download(
                source_url="https://x.test/page.html", media_type=MediaType.MOVIE, tmdb_id=1
            )
        assert excinfo.value.status_code == 502
        assert excinfo.value.code is ErrorCode.DOWNSTREAM_UNAVAILABLE


class TestSearchProgressParams:
    @pytest.mark.asyncio
    async def test_sends_the_search_parameters(self):
        import httpx
        from medialab_contracts import TorrentSearchScope

        from medialab_orchestrator.clients.torrent_downloader import TorrentDownloaderClient

        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"state": "idle"})

        client = TorrentDownloaderClient()
        client._base_url = "http://downloader.test"
        client._transport = httpx.MockTransport(handler)
        scope = TorrentSearchScope(media_type=MediaType.SHOW, season=2, episode=5)

        await client.search_progress("the wire", scope, alt_query="wire")

        request = seen[0]
        assert request.url.path.endswith("/search/torrents/progress")
        assert dict(request.url.params) == {
            "query": "the wire",
            "media_type": "show",
            "alt_query": "wire",
            "season": "2",
            "episode": "5",
        }


def _recording(client_class, seen: list, body=None, status: int = 200):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)

    client = client_class()
    client._base_url = "http://worker.test"
    client._transport = httpx.MockTransport(handler)
    return client


DISCOVER_BODY = {"items": [], "page": 1, "total_pages": 1, "cached_at": "2026-11-01T00:00:00Z"}


class TestDownloaderRequestCalls:
    @pytest.mark.asyncio
    async def test_movie_pick_sends_movie_scope_and_no_season(self):
        from medialab_orchestrator.clients.torrent_downloader import TorrentDownloaderClient

        seen: list = []
        client = _recording(TorrentDownloaderClient, seen, status=404, body={"code": "x"})
        result = await client.pick_torrent(
            "Heat 1995", media_type=MediaType.MOVIE, resolution="1080p", min_seeders=10
        )
        assert result is None
        assert dict(seen[0].url.params) == {
            "query": "Heat 1995",
            "media_type": "movie",
            "resolution": "1080p",
            "min_seeders": "10",
        }

    @pytest.mark.asyncio
    async def test_season_pick_defaults_to_show(self):
        from medialab_orchestrator.clients.torrent_downloader import TorrentDownloaderClient

        seen: list = []
        client = _recording(TorrentDownloaderClient, seen, body={"name": "x"})
        await client.pick_torrent("Severance", season=1, resolution="1080p", min_seeders=10)
        params = dict(seen[0].url.params)
        assert (params["media_type"], params["season"]) == ("show", "1")

    @pytest.mark.asyncio
    async def test_released_discover_sends_the_flag_only_when_set(self):
        from medialab_orchestrator.clients.torrent_downloader import TorrentDownloaderClient

        seen: list = []
        client = _recording(TorrentDownloaderClient, seen, body=DISCOVER_BODY)
        await client.discover(MediaType.MOVIE, released=True)
        await client.discover(MediaType.MOVIE)
        assert dict(seen[0].url.params) == {"released": "true"}
        assert dict(seen[1].url.params) == {}

    @pytest.mark.asyncio
    async def test_recommendations_path_and_params(self):
        from medialab_orchestrator.clients.torrent_downloader import TorrentDownloaderClient

        seen: list = []
        client = _recording(TorrentDownloaderClient, seen, body=DISCOVER_BODY)
        response = await client.recommendations(MediaType.MOVIE, 238, released=True, limit=5)
        assert response.items == []
        assert seen[0].url.path.endswith("/discover/movie/recommendations/238")
        assert dict(seen[0].url.params) == {"released": "true", "limit": "5"}


class TestJellyfinDiscoverCalls:
    @pytest.mark.asyncio
    async def test_put_placeholder_sends_body_and_refresh(self):
        import json

        from medialab_contracts import PlaceholderState, PlaceholderWrite

        from medialab_orchestrator.clients.jellyfin import JellyfinClient

        seen: list = []
        client = _recording(JellyfinClient, seen, status=204)
        body = PlaceholderWrite(title="Heat", year=1995, state=PlaceholderState.REQUESTED)
        await client.put_placeholder(MediaType.MOVIE, 949, body, refresh=False)
        request = seen[0]
        assert request.method == "PUT"
        assert request.url.path.endswith("/discover/placeholders/movie/949")
        assert dict(request.url.params) == {"refresh": "false"}
        assert json.loads(request.content) == {"title": "Heat", "year": 1995, "state": "requested"}

    @pytest.mark.asyncio
    async def test_put_placeholder_failure_raises(self):
        from medialab_contracts import PlaceholderWrite

        from medialab_orchestrator.clients.jellyfin import JellyfinClient
        from medialab_orchestrator.core.errors import AppException

        client = _recording(JellyfinClient, [], status=404, body={"code": "NOT_FOUND"})
        with pytest.raises(AppException):
            await client.put_placeholder(MediaType.MOVIE, 949, PlaceholderWrite(title="Heat"))

    @pytest.mark.asyncio
    async def test_delete_placeholder(self):
        from medialab_orchestrator.clients.jellyfin import JellyfinClient

        seen: list = []
        client = _recording(JellyfinClient, seen, status=204)
        await client.delete_placeholder(MediaType.SHOW, 95396)
        assert seen[0].method == "DELETE"
        assert seen[0].url.path.endswith("/discover/placeholders/show/95396")
        assert dict(seen[0].url.params) == {"refresh": "true"}

    @pytest.mark.asyncio
    async def test_placeholders_and_refresh(self):
        from medialab_contracts import PlaceholderState

        from medialab_orchestrator.clients.jellyfin import JellyfinClient

        seen: list = []
        listing = {
            "media_type": "movie",
            "placeholders": [{"tmdb_id": 949, "title": "Heat", "state": "requested"}],
        }
        client = _recording(JellyfinClient, seen, body=listing)
        response = await client.placeholders(MediaType.MOVIE)
        assert response.placeholders[0].state is PlaceholderState.REQUESTED
        await client.refresh_discover(MediaType.MOVIE)
        assert seen[1].method == "POST"
        assert seen[1].url.path.endswith("/discover/refresh")
        assert dict(seen[1].url.params) == {"media_type": "movie"}

    @pytest.mark.asyncio
    async def test_favorites_parse_events(self):
        from medialab_contracts import FavoriteScope

        from medialab_orchestrator.clients.jellyfin import JellyfinClient

        seen: list = []
        events = [
            {
                "jellyfin_user_id": "u1",
                "item_id": "i1",
                "item_type": "Movie",
                "tmdb_id": 949,
                "is_favorite": True,
            }
        ]
        client = _recording(JellyfinClient, seen, body=events)
        result = await client.favorites("u1", FavoriteScope.LIBRARY)
        assert [(e.tmdb_id, e.media_type) for e in result] == [(949, MediaType.MOVIE)]
        assert dict(seen[0].url.params) == {"user_id": "u1", "scope": "library"}
