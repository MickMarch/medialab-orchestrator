"""Discover and watchlist gateway routes: annotation, best-effort library badge, relays."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from medialab_contracts import (
    DEFAULT_FOLLOW_RESOLUTION,
    DiscoverItem,
    DiscoverResponse,
    FollowRequest,
    FollowStart,
    FollowStartMode,
    Genre,
    GenresResponse,
    LibraryTmdbIdsResponse,
    MediaType,
    WatchlistAddRequest,
    WatchlistKind,
)

from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.store import WatchlistStore

WISHED_ID = 1
LIBRARY_ID = 2
PLAIN_ID = 3


def _discover(media_type: MediaType = MediaType.MOVIE) -> DiscoverResponse:
    return DiscoverResponse(
        items=[
            DiscoverItem(tmdb_id=tmdb_id, media_type=media_type, title=f"t{tmdb_id}")
            for tmdb_id in (WISHED_ID, LIBRARY_ID, PLAIN_ID)
        ],
        page=1,
        total_pages=5,
        cached_at=datetime.now(UTC),
    )


def _library(media_type: MediaType, *tmdb_ids: int) -> LibraryTmdbIdsResponse:
    return LibraryTmdbIdsResponse(media_type=media_type, tmdb_ids=list(tmdb_ids))


def _flags(body: dict, key: str) -> dict[int, object]:
    return {item["tmdb_id"]: item[key] for item in body["items"]}


def _tmdb_down() -> AppException:
    return AppException(
        status_code=503, code=ErrorCode.TMDB_UNAVAILABLE, detail="TMDB is unavailable."
    )


def _follow(**start) -> FollowRequest:
    return FollowRequest(start=FollowStart(mode=FollowStartMode.NEW_ONLY, **start))


def _jellyfin_down() -> AppException:
    return AppException(status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="down")


class TestDiscover:
    def test_proxies_with_genre_and_page(
        self, app_client, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ):
        torrent_client.discover.return_value = _discover()
        jellyfin_client.library_tmdb_ids.return_value = _library(MediaType.MOVIE)
        resp = app_client.get("/api/v1/discover/movie?genre=28&page=2")
        assert resp.status_code == 200
        torrent_client.discover.assert_awaited_once_with(MediaType.MOVIE, genre=28, page=2)
        assert resp.json()["total_pages"] == 5

    def test_on_watchlist_only_for_matching_media_type_and_id(
        self,
        app_client,
        watchlist: WatchlistStore,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
    ):
        watchlist.add(MediaType.MOVIE, WISHED_ID, WatchlistAddRequest(title="w"))
        watchlist.add(MediaType.SHOW, PLAIN_ID, WatchlistAddRequest(title="other type"))
        torrent_client.discover.return_value = _discover(MediaType.MOVIE)
        jellyfin_client.library_tmdb_ids.return_value = _library(MediaType.MOVIE)
        body = app_client.get("/api/v1/discover/movie").json()
        assert _flags(body, "on_watchlist") == {WISHED_ID: True, LIBRARY_ID: False, PLAIN_ID: False}
        assert _flags(body, "watchlist_kind") == {
            WISHED_ID: "saved",
            LIBRARY_ID: None,
            PLAIN_ID: None,
        }

    def test_in_library_from_jellyfin_client(
        self, app_client, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ):
        torrent_client.discover.return_value = _discover(MediaType.SHOW)
        jellyfin_client.library_tmdb_ids.return_value = _library(MediaType.SHOW, LIBRARY_ID)
        body = app_client.get("/api/v1/discover/show").json()
        jellyfin_client.library_tmdb_ids.assert_awaited_once_with(MediaType.SHOW)
        assert _flags(body, "in_library") == {WISHED_ID: False, LIBRARY_ID: True, PLAIN_ID: False}

    def test_in_library_false_when_jellyfin_fails(
        self, app_client, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ):
        torrent_client.discover.return_value = _discover()
        jellyfin_client.library_tmdb_ids.side_effect = _jellyfin_down()
        resp = app_client.get("/api/v1/discover/movie")
        assert resp.status_code == 200
        assert not any(_flags(resp.json(), "in_library").values())

    def test_tmdb_unavailable_propagates_as_503(self, app_client, torrent_client: AsyncMock):
        torrent_client.discover.side_effect = _tmdb_down()
        resp = app_client.get("/api/v1/discover/movie")
        assert resp.status_code == 503
        assert resp.json()["code"] == ErrorCode.TMDB_UNAVAILABLE.value

    def test_invalid_media_type_is_422(self, app_client):
        assert app_client.get("/api/v1/discover/podcast").status_code == 422

    def test_requires_api_key(self, unauthed_client):
        assert unauthed_client.get("/api/v1/discover/movie").status_code == 403


class TestGenres:
    def test_proxies(self, app_client, torrent_client: AsyncMock):
        torrent_client.discover_genres.return_value = GenresResponse(
            genres=[Genre(id=28, name="Action")]
        )
        resp = app_client.get("/api/v1/discover/show/genres")
        assert resp.status_code == 200
        assert resp.json() == {"genres": [{"id": 28, "name": "Action"}]}
        torrent_client.discover_genres.assert_awaited_once_with(MediaType.SHOW)

    def test_tmdb_unavailable_propagates_as_503(self, app_client, torrent_client: AsyncMock):
        torrent_client.discover_genres.side_effect = _tmdb_down()
        resp = app_client.get("/api/v1/discover/movie/genres")
        assert resp.status_code == 503
        assert resp.json()["code"] == ErrorCode.TMDB_UNAVAILABLE.value


class TestDiscoverKind:
    def test_following_kind_is_carried(
        self,
        app_client,
        watchlist: WatchlistStore,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
    ):
        watchlist.add(MediaType.SHOW, WISHED_ID, WatchlistAddRequest(title="w"))
        watchlist.follow(WISHED_ID, _follow())
        torrent_client.discover.return_value = _discover(MediaType.SHOW)
        jellyfin_client.library_tmdb_ids.return_value = _library(MediaType.SHOW)
        body = app_client.get("/api/v1/discover/show").json()
        assert _flags(body, "watchlist_kind")[WISHED_ID] == WatchlistKind.FOLLOWING.value
        assert _flags(body, "on_watchlist")[WISHED_ID] is True


class TestWatchlistRoutes:
    def test_put_returns_200_with_the_item_and_is_idempotent(
        self, app_client, watchlist: WatchlistStore
    ):
        body = {"title": "Dune", "year": "2021", "poster_path": "/p.jpg", "overview": "o"}
        first = app_client.put("/api/v1/watchlist/movie/438631", json=body)
        second = app_client.put("/api/v1/watchlist/movie/438631", json=body)
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["tmdb_id"] == 438631
        assert first.json()["media_type"] == "movie"
        assert second.json()["added_at"] == first.json()["added_at"]
        assert len(watchlist.list()) == 1

    def test_put_without_title_is_422(self, app_client):
        assert app_client.put("/api/v1/watchlist/movie/1", json={}).status_code == 422

    def test_delete_returns_204(self, app_client, watchlist: WatchlistStore):
        watchlist.add(MediaType.MOVIE, 1, WatchlistAddRequest(title="t"))
        resp = app_client.delete("/api/v1/watchlist/movie/1")
        assert resp.status_code == 204
        assert watchlist.list() == []

    def test_delete_absent_returns_204(self, app_client):
        assert app_client.delete("/api/v1/watchlist/show/999").status_code == 204

    def test_list_newest_first_with_in_library(
        self, app_client, watchlist: WatchlistStore, jellyfin_client: AsyncMock
    ):
        watchlist.add(MediaType.MOVIE, 1, WatchlistAddRequest(title="a"))
        watchlist.add(MediaType.SHOW, 2, WatchlistAddRequest(title="b"))
        watchlist.add(MediaType.MOVIE, 3, WatchlistAddRequest(title="c"))
        libraries = {
            MediaType.MOVIE: _library(MediaType.MOVIE, 3),
            MediaType.SHOW: _library(MediaType.SHOW, 1),
        }
        jellyfin_client.library_tmdb_ids.side_effect = lambda media_type: libraries[media_type]
        body = app_client.get("/api/v1/watchlist").json()
        assert [item["tmdb_id"] for item in body["items"]] == [3, 2, 1]
        assert _flags(body, "in_library") == {3: True, 2: False, 1: False}
        assert jellyfin_client.library_tmdb_ids.await_count == 2

    def test_list_filtered_by_media_type(
        self, app_client, watchlist: WatchlistStore, jellyfin_client: AsyncMock
    ):
        watchlist.add(MediaType.MOVIE, 1, WatchlistAddRequest(title="a"))
        watchlist.add(MediaType.SHOW, 2, WatchlistAddRequest(title="b"))
        jellyfin_client.library_tmdb_ids.return_value = _library(MediaType.SHOW)
        body = app_client.get("/api/v1/watchlist?media_type=show").json()
        assert [item["tmdb_id"] for item in body["items"]] == [2]
        jellyfin_client.library_tmdb_ids.assert_awaited_once_with(MediaType.SHOW)

    def test_list_survives_jellyfin_failure(
        self, app_client, watchlist: WatchlistStore, jellyfin_client: AsyncMock
    ):
        watchlist.add(MediaType.MOVIE, 1, WatchlistAddRequest(title="a"))
        jellyfin_client.library_tmdb_ids.side_effect = _jellyfin_down()
        resp = app_client.get("/api/v1/watchlist")
        assert resp.status_code == 200
        assert resp.json()["items"][0]["in_library"] is False

    def test_empty_list_skips_jellyfin(self, app_client, jellyfin_client: AsyncMock):
        assert app_client.get("/api/v1/watchlist").json() == {"items": []}
        jellyfin_client.library_tmdb_ids.assert_not_awaited()

    def test_list_filtered_by_kind(
        self, app_client, watchlist: WatchlistStore, jellyfin_client: AsyncMock
    ):
        watchlist.add(MediaType.SHOW, 1, WatchlistAddRequest(title="a"))
        watchlist.add(MediaType.SHOW, 2, WatchlistAddRequest(title="b"))
        watchlist.follow(2, _follow())
        jellyfin_client.library_tmdb_ids.return_value = _library(MediaType.SHOW)
        following = app_client.get("/api/v1/watchlist?kind=following").json()
        saved = app_client.get("/api/v1/watchlist?kind=saved").json()
        assert [item["tmdb_id"] for item in following["items"]] == [2]
        assert following["items"][0]["kind"] == "following"
        assert following["items"][0]["follow"]["start"]["mode"] == "new_only"
        assert [item["tmdb_id"] for item in saved["items"]] == [1]
        assert saved["items"][0]["follow"] is None

    def test_list_rejects_unknown_kind(self, app_client):
        assert app_client.get("/api/v1/watchlist?kind=nope").status_code == 422

    def test_delete_removes_a_follow_entirely(self, app_client, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, 1, WatchlistAddRequest(title="a"))
        watchlist.follow(1, _follow())
        assert app_client.delete("/api/v1/watchlist/show/1").status_code == 204
        assert watchlist.list() == []


SHOW_ID = 1396
FOLLOW_URL = f"/api/v1/watchlist/show/{SHOW_ID}/follow"
FOLLOW_BODY = {"start": {"mode": "from", "season": 2, "episode": 5}, "resolution": "720p"}


class TestFollowRoutes:
    def test_follow_saved_show_returns_the_following_item(
        self, app_client, watchlist: WatchlistStore
    ):
        watchlist.add(MediaType.SHOW, SHOW_ID, WatchlistAddRequest(title="bb"))
        resp = app_client.put(FOLLOW_URL, json=FOLLOW_BODY)
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "following"
        assert body["follow"]["start"] == {"mode": "from", "season": 2, "episode": 5}
        assert body["follow"]["resolution"] == "720p"
        assert body["follow"]["paused"] is False
        assert body["follow"]["followed_at"]
        assert watchlist.keys(MediaType.SHOW) == {SHOW_ID: WatchlistKind.FOLLOWING}

    def test_follow_is_idempotent(self, app_client, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, SHOW_ID, WatchlistAddRequest(title="bb"))
        first = app_client.put(FOLLOW_URL, json=FOLLOW_BODY)
        second = app_client.put(FOLLOW_URL, json={"start": {"mode": "new_only"}})
        assert (first.status_code, second.status_code) == (200, 200)
        assert second.json()["follow"]["start"]["mode"] == "new_only"
        assert second.json()["follow"]["resolution"] == DEFAULT_FOLLOW_RESOLUTION
        assert len(watchlist.list()) == 1

    def test_follow_missing_row_is_404(self, app_client):
        resp = app_client.put(FOLLOW_URL, json=FOLLOW_BODY)
        assert resp.status_code == 404
        assert resp.json()["code"] == ErrorCode.WATCHLIST_ITEM_NOT_FOUND.value

    def test_follow_movie_is_422(self, app_client, watchlist: WatchlistStore):
        watchlist.add(MediaType.MOVIE, SHOW_ID, WatchlistAddRequest(title="m"))
        resp = app_client.put(f"/api/v1/watchlist/movie/{SHOW_ID}/follow", json=FOLLOW_BODY)
        assert resp.status_code == 422
        assert resp.json()["code"] == ErrorCode.INVALID_INPUT.value
        assert watchlist.keys(MediaType.MOVIE) == {SHOW_ID: WatchlistKind.SAVED}

    def test_follow_from_without_episode_is_422(self, app_client, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, SHOW_ID, WatchlistAddRequest(title="bb"))
        resp = app_client.put(FOLLOW_URL, json={"start": {"mode": "from", "season": 2}})
        assert resp.status_code == 422

    def test_unfollow_keeps_the_saved_row(self, app_client, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, SHOW_ID, WatchlistAddRequest(title="bb"))
        watchlist.follow(SHOW_ID, _follow())
        assert app_client.delete(FOLLOW_URL).status_code == 204
        assert watchlist.keys(MediaType.SHOW) == {SHOW_ID: WatchlistKind.SAVED}

    def test_unfollow_absent_is_204(self, app_client):
        assert app_client.delete(FOLLOW_URL).status_code == 204

    def test_pause_and_resume(self, app_client, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, SHOW_ID, WatchlistAddRequest(title="bb"))
        watchlist.follow(SHOW_ID, _follow())
        paused = app_client.post(f"{FOLLOW_URL}/pause")
        assert paused.status_code == 200
        assert paused.json()["follow"]["paused"] is True
        resumed = app_client.post(f"{FOLLOW_URL}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["follow"]["paused"] is False

    def test_pause_without_a_follow_is_404(self, app_client, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, SHOW_ID, WatchlistAddRequest(title="bb"))
        resp = app_client.post(f"{FOLLOW_URL}/pause")
        assert resp.status_code == 404
        assert resp.json()["code"] == ErrorCode.WATCHLIST_ITEM_NOT_FOUND.value

    def test_pause_on_a_movie_is_422(self, app_client):
        url = f"/api/v1/watchlist/movie/{SHOW_ID}/follow/pause"
        assert app_client.post(url).status_code == 422

    def test_follow_routes_require_api_key(self, unauthed_client):
        assert unauthed_client.put(FOLLOW_URL, json=FOLLOW_BODY).status_code == 403
