"""TMDB title search: passthrough annotated with watchlist and library flags."""

from unittest.mock import AsyncMock

from medialab_contracts import (
    FollowRequest,
    FollowStart,
    FollowStartMode,
    LibraryTmdbIdsResponse,
    MediaType,
    WatchlistAddRequest,
)

from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.store import WatchlistStore

MOVIE_ID = 10
SHOW_ID = 20
PLAIN_ID = 30
TMDB_MOVIE = "movie"
TMDB_TV = "tv"


def _result(tmdb_id: int, media_type: str) -> dict:
    return {
        "tmdb_id": tmdb_id,
        "title": f"t{tmdb_id}",
        "year": "2021",
        "media_type": media_type,
        "overview": "o",
        "vote_average": 7.5,
        "poster_path": "/p.jpg",
    }


def _search(*results: dict) -> dict:
    return {"status": "success", "message": "ok", "data": list(results)}


def _library(media_type: MediaType, *tmdb_ids: int) -> LibraryTmdbIdsResponse:
    return LibraryTmdbIdsResponse(media_type=media_type, tmdb_ids=list(tmdb_ids))


def _flags(body: dict, key: str) -> dict[tuple[str, int], object]:
    return {(item["media_type"], item["tmdb_id"]): item[key] for item in body["data"]}


class TestSearchTmdb:
    def test_on_watchlist_matches_media_type_and_id_with_tv_as_show(
        self,
        app_client,
        watchlist: WatchlistStore,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
    ):
        watchlist.add(MediaType.MOVIE, MOVIE_ID, WatchlistAddRequest(title="m"))
        watchlist.add(MediaType.SHOW, SHOW_ID, WatchlistAddRequest(title="s"))
        watchlist.follow(SHOW_ID, FollowRequest(start=FollowStart(mode=FollowStartMode.NEW_ONLY)))
        torrent_client.search_tmdb.return_value = _search(
            _result(MOVIE_ID, TMDB_MOVIE),
            _result(MOVIE_ID, TMDB_TV),
            _result(SHOW_ID, TMDB_TV),
            _result(SHOW_ID, TMDB_MOVIE),
            _result(PLAIN_ID, TMDB_MOVIE),
        )
        jellyfin_client.library_tmdb_ids.side_effect = lambda media_type: _library(media_type)
        body = app_client.get("/api/v1/search/tmdb?query=foo").json()
        assert _flags(body, "on_watchlist") == {
            (TMDB_MOVIE, MOVIE_ID): True,
            (TMDB_TV, MOVIE_ID): False,
            (TMDB_TV, SHOW_ID): True,
            (TMDB_MOVIE, SHOW_ID): False,
            (TMDB_MOVIE, PLAIN_ID): False,
        }
        assert _flags(body, "watchlist_kind") == {
            (TMDB_MOVIE, MOVIE_ID): "saved",
            (TMDB_TV, MOVIE_ID): None,
            (TMDB_TV, SHOW_ID): "following",
            (TMDB_MOVIE, SHOW_ID): None,
            (TMDB_MOVIE, PLAIN_ID): None,
        }

    def test_in_library_with_one_lookup_per_media_type(
        self, app_client, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ):
        torrent_client.search_tmdb.return_value = _search(
            _result(MOVIE_ID, TMDB_MOVIE),
            _result(PLAIN_ID, TMDB_MOVIE),
            _result(SHOW_ID, TMDB_TV),
        )
        libraries = {
            MediaType.MOVIE: _library(MediaType.MOVIE, MOVIE_ID),
            MediaType.SHOW: _library(MediaType.SHOW, SHOW_ID),
        }
        jellyfin_client.library_tmdb_ids.side_effect = lambda media_type: libraries[media_type]
        body = app_client.get("/api/v1/search/tmdb?query=foo").json()
        assert _flags(body, "in_library") == {
            (TMDB_MOVIE, MOVIE_ID): True,
            (TMDB_MOVIE, PLAIN_ID): False,
            (TMDB_TV, SHOW_ID): True,
        }
        assert jellyfin_client.library_tmdb_ids.await_count == len(libraries)

    def test_passes_through_fields_and_wire_media_type(
        self, app_client, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ):
        result = _result(SHOW_ID, TMDB_TV)
        torrent_client.search_tmdb.return_value = _search(result)
        jellyfin_client.library_tmdb_ids.return_value = _library(MediaType.SHOW)
        body = app_client.get("/api/v1/search/tmdb?query=foo").json()
        assert body["status"] == "success"
        assert body["message"] == "ok"
        assert body["data"] == [
            {**result, "on_watchlist": False, "watchlist_kind": None, "in_library": False}
        ]
        torrent_client.search_tmdb.assert_awaited_once_with("foo")

    def test_in_library_false_when_jellyfin_raises(
        self, app_client, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ):
        torrent_client.search_tmdb.return_value = _search(_result(MOVIE_ID, TMDB_MOVIE))
        jellyfin_client.library_tmdb_ids.side_effect = AppException(
            status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="down"
        )
        resp = app_client.get("/api/v1/search/tmdb?query=foo")
        assert resp.status_code == 200
        assert resp.json()["data"][0]["in_library"] is False

    def test_no_results_makes_no_jellyfin_call(
        self, app_client, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ):
        torrent_client.search_tmdb.return_value = _search()
        body = app_client.get("/api/v1/search/tmdb?query=nothing").json()
        assert body["data"] == []
        jellyfin_client.library_tmdb_ids.assert_not_awaited()

    def test_unknown_media_type_is_flagged_false_without_lookup(
        self, app_client, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ):
        torrent_client.search_tmdb.return_value = _search(_result(PLAIN_ID, "person"))
        item = app_client.get("/api/v1/search/tmdb?query=foo").json()["data"][0]
        assert item["on_watchlist"] is False
        assert item["watchlist_kind"] is None
        assert item["in_library"] is False
        jellyfin_client.library_tmdb_ids.assert_not_awaited()
