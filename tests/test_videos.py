"""The trailers proxy: one downloader call, TMDB outages relayed."""

from unittest.mock import AsyncMock

from medialab_contracts import MediaType, Video, VideosResponse, VideoType

from medialab_orchestrator.core.errors import AppException, ErrorCode

MOVIE_ID = 438631
SHOW_ID = 100088
SEASON = 2


def _videos() -> VideosResponse:
    return VideosResponse(
        videos=[Video(key="abc", name="Official Trailer", type=VideoType.TRAILER, official=True)]
    )


class TestVideosProxy:
    def test_movie_videos_pass_through(self, app_client, torrent_client: AsyncMock):
        torrent_client.videos.return_value = _videos()
        resp = app_client.get(f"/api/v1/search/tmdb/movie/{MOVIE_ID}/videos")
        assert resp.status_code == 200
        assert resp.json()["videos"][0]["key"] == "abc"
        torrent_client.videos.assert_awaited_once_with(MediaType.MOVIE, MOVIE_ID, season=None)

    def test_season_is_forwarded(self, app_client, torrent_client: AsyncMock):
        torrent_client.videos.return_value = _videos()
        app_client.get(f"/api/v1/search/tmdb/show/{SHOW_ID}/videos", params={"season": SEASON})
        torrent_client.videos.assert_awaited_once_with(MediaType.SHOW, SHOW_ID, season=SEASON)

    def test_tmdb_unavailable_relays_as_503(self, app_client, torrent_client: AsyncMock):
        torrent_client.videos.side_effect = AppException(
            status_code=503, code=ErrorCode.TMDB_UNAVAILABLE, detail="TMDB is unavailable."
        )
        resp = app_client.get(f"/api/v1/search/tmdb/movie/{MOVIE_ID}/videos")
        assert resp.status_code == 503
        assert resp.json()["code"] == ErrorCode.TMDB_UNAVAILABLE.value

    def test_requires_api_key(self, unauthed_client):
        assert (
            unauthed_client.get(f"/api/v1/search/tmdb/movie/{MOVIE_ID}/videos").status_code == 403
        )
