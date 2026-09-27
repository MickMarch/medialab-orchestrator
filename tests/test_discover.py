"""Discover and wishlist gateway routes: annotation, best-effort library badge, relays."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from medialab_contracts import (
    DiscoverItem,
    DiscoverResponse,
    Genre,
    GenresResponse,
    LibraryTmdbIdsResponse,
    MediaType,
    WishlistAddRequest,
)

from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.store import WishlistStore

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


def _flags(body: dict, key: str) -> dict[int, bool]:
    return {item["tmdb_id"]: item[key] for item in body["items"]}


def _tmdb_down() -> AppException:
    return AppException(
        status_code=503, code=ErrorCode.TMDB_UNAVAILABLE, detail="TMDB is unavailable."
    )


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

    def test_on_wishlist_only_for_matching_media_type_and_id(
        self,
        app_client,
        wishlist: WishlistStore,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
    ):
        wishlist.add(MediaType.MOVIE, WISHED_ID, WishlistAddRequest(title="w"))
        wishlist.add(MediaType.SHOW, PLAIN_ID, WishlistAddRequest(title="other type"))
        torrent_client.discover.return_value = _discover(MediaType.MOVIE)
        jellyfin_client.library_tmdb_ids.return_value = _library(MediaType.MOVIE)
        body = app_client.get("/api/v1/discover/movie").json()
        assert _flags(body, "on_wishlist") == {WISHED_ID: True, LIBRARY_ID: False, PLAIN_ID: False}

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


class TestWishlistRoutes:
    def test_put_returns_200_with_the_item_and_is_idempotent(
        self, app_client, wishlist: WishlistStore
    ):
        body = {"title": "Dune", "year": "2021", "poster_path": "/p.jpg", "overview": "o"}
        first = app_client.put("/api/v1/wishlist/movie/438631", json=body)
        second = app_client.put("/api/v1/wishlist/movie/438631", json=body)
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["tmdb_id"] == 438631
        assert first.json()["media_type"] == "movie"
        assert second.json()["added_at"] == first.json()["added_at"]
        assert len(wishlist.list()) == 1

    def test_put_without_title_is_422(self, app_client):
        assert app_client.put("/api/v1/wishlist/movie/1", json={}).status_code == 422

    def test_delete_returns_204(self, app_client, wishlist: WishlistStore):
        wishlist.add(MediaType.MOVIE, 1, WishlistAddRequest(title="t"))
        resp = app_client.delete("/api/v1/wishlist/movie/1")
        assert resp.status_code == 204
        assert wishlist.list() == []

    def test_delete_absent_returns_204(self, app_client):
        assert app_client.delete("/api/v1/wishlist/show/999").status_code == 204

    def test_list_newest_first_with_in_library(
        self, app_client, wishlist: WishlistStore, jellyfin_client: AsyncMock
    ):
        wishlist.add(MediaType.MOVIE, 1, WishlistAddRequest(title="a"))
        wishlist.add(MediaType.SHOW, 2, WishlistAddRequest(title="b"))
        wishlist.add(MediaType.MOVIE, 3, WishlistAddRequest(title="c"))
        libraries = {
            MediaType.MOVIE: _library(MediaType.MOVIE, 3),
            MediaType.SHOW: _library(MediaType.SHOW, 1),
        }
        jellyfin_client.library_tmdb_ids.side_effect = lambda media_type: libraries[media_type]
        body = app_client.get("/api/v1/wishlist").json()
        assert [item["tmdb_id"] for item in body["items"]] == [3, 2, 1]
        assert _flags(body, "in_library") == {3: True, 2: False, 1: False}
        assert jellyfin_client.library_tmdb_ids.await_count == 2

    def test_list_filtered_by_media_type(
        self, app_client, wishlist: WishlistStore, jellyfin_client: AsyncMock
    ):
        wishlist.add(MediaType.MOVIE, 1, WishlistAddRequest(title="a"))
        wishlist.add(MediaType.SHOW, 2, WishlistAddRequest(title="b"))
        jellyfin_client.library_tmdb_ids.return_value = _library(MediaType.SHOW)
        body = app_client.get("/api/v1/wishlist?media_type=show").json()
        assert [item["tmdb_id"] for item in body["items"]] == [2]
        jellyfin_client.library_tmdb_ids.assert_awaited_once_with(MediaType.SHOW)

    def test_list_survives_jellyfin_failure(
        self, app_client, wishlist: WishlistStore, jellyfin_client: AsyncMock
    ):
        wishlist.add(MediaType.MOVIE, 1, WishlistAddRequest(title="a"))
        jellyfin_client.library_tmdb_ids.side_effect = _jellyfin_down()
        resp = app_client.get("/api/v1/wishlist")
        assert resp.status_code == 200
        assert resp.json()["items"][0]["in_library"] is False

    def test_empty_list_skips_jellyfin(self, app_client, jellyfin_client: AsyncMock):
        assert app_client.get("/api/v1/wishlist").json() == {"items": []}
        jellyfin_client.library_tmdb_ids.assert_not_awaited()
