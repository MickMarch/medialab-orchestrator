"""Show browse route: episodes joined with library presence and queued jobs."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from medialab_contracts import (
    Episode,
    EpisodeKey,
    FollowRequest,
    FollowStart,
    FollowStartMode,
    LibraryEpisodesResponse,
    LibraryTmdbIdsResponse,
    MediaType,
    Season,
    SeriesEpisodesResponse,
    WatchlistAddRequest,
)

from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.store import JobStatus, JobStore, WatchlistStore

SHOW_ID = 1396
OTHER_SHOW_ID = 2
TODAY = datetime.now(UTC).date()
YESTERDAY = TODAY - timedelta(days=1)
TOMORROW = TODAY + timedelta(days=1)
DETAIL = {
    "status": "success",
    "data": {
        "name": "Breaking Bad",
        "first_air_date": "2008-01-20",
        "poster_path": "/bb.jpg",
        "overview": "A chemistry teacher.",
    },
}


def _episodes() -> SeriesEpisodesResponse:
    return SeriesEpisodesResponse(
        tmdb_id=SHOW_ID,
        status="Ended",
        seasons=[
            Season(season=1, name="Season 1", episode_count=2),
            Season(season=2, name="Season 2", episode_count=2),
        ],
        episodes=[
            Episode(season=1, episode=1, title="Pilot", air_date=YESTERDAY),
            Episode(season=1, episode=2, title="Cat", air_date=TODAY),
            Episode(season=2, episode=1, title="Seven", air_date=TOMORROW),
            Episode(season=2, episode=2, title="Grilled", air_date=None),
        ],
        next_episode=Episode(season=2, episode=1, title="Seven", air_date=TOMORROW),
    )


def _library(*keys: tuple[int, int]) -> LibraryEpisodesResponse:
    return LibraryEpisodesResponse(
        tmdb_id=SHOW_ID, episodes=[EpisodeKey(season=s, episode=e) for s, e in keys]
    )


def _by_key(body: dict, field: str) -> dict[tuple[int, int], object]:
    return {(ep["season"], ep["episode"]): ep[field] for ep in body["episodes"]}


def _tmdb_down() -> AppException:
    return AppException(
        status_code=503, code=ErrorCode.TMDB_UNAVAILABLE, detail="TMDB is unavailable."
    )


def _jellyfin_down() -> AppException:
    return AppException(status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="down")


@pytest.fixture
def downstream(torrent_client: AsyncMock, jellyfin_client: AsyncMock) -> None:
    torrent_client.series_episodes.return_value = _episodes()
    torrent_client.tmdb_detail.return_value = DETAIL
    jellyfin_client.library_episodes.return_value = _library()
    jellyfin_client.library_tmdb_ids.return_value = LibraryTmdbIdsResponse(
        media_type=MediaType.SHOW, tmdb_ids=[]
    )


@pytest.mark.usefixtures("downstream")
class TestBrowseShow:
    def test_header_from_tmdb_detail_and_episode_listing(
        self, app_client, torrent_client: AsyncMock
    ):
        resp = app_client.get(f"/api/v1/shows/{SHOW_ID}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tmdb_id"] == SHOW_ID
        assert body["title"] == "Breaking Bad"
        assert body["year"] == "2008"
        assert body["poster_path"] == "/bb.jpg"
        assert body["overview"] == "A chemistry teacher."
        assert body["status"] == "Ended"
        assert [s["season"] for s in body["seasons"]] == [1, 2]
        assert body["next_episode"]["episode"] == 1
        assert len(body["episodes"]) == 4
        torrent_client.series_episodes.assert_awaited_once_with(SHOW_ID)
        torrent_client.tmdb_detail.assert_awaited_once_with(MediaType.SHOW, SHOW_ID)

    def test_aired_when_air_date_is_today_or_earlier(self, app_client):
        body = app_client.get(f"/api/v1/shows/{SHOW_ID}").json()
        assert _by_key(body, "aired") == {
            (1, 1): True,
            (1, 2): True,
            (2, 1): False,
            (2, 2): False,
        }

    def test_in_library_from_jellyfin_keys(self, app_client, jellyfin_client: AsyncMock):
        jellyfin_client.library_episodes.return_value = _library((1, 1), (2, 2))
        body = app_client.get(f"/api/v1/shows/{SHOW_ID}").json()
        jellyfin_client.library_episodes.assert_awaited_once_with(SHOW_ID)
        assert _by_key(body, "in_library") == {
            (1, 1): True,
            (1, 2): False,
            (2, 1): False,
            (2, 2): True,
        }

    def test_jellyfin_failure_leaves_in_library_false_with_200(
        self, app_client, jellyfin_client: AsyncMock
    ):
        jellyfin_client.library_episodes.side_effect = _jellyfin_down()
        jellyfin_client.library_tmdb_ids.side_effect = _jellyfin_down()
        resp = app_client.get(f"/api/v1/shows/{SHOW_ID}")
        assert resp.status_code == 200
        assert not any(_by_key(resp.json(), "in_library").values())
        assert resp.json()["in_library"] is False

    def test_series_level_flags(
        self, app_client, watchlist: WatchlistStore, jellyfin_client: AsyncMock
    ):
        watchlist.add(MediaType.SHOW, SHOW_ID, WatchlistAddRequest(title="bb"))
        jellyfin_client.library_tmdb_ids.return_value = LibraryTmdbIdsResponse(
            media_type=MediaType.SHOW, tmdb_ids=[SHOW_ID]
        )
        body = app_client.get(f"/api/v1/shows/{SHOW_ID}").json()
        assert body["on_watchlist"] is True
        assert body["in_library"] is True
        assert body["watchlist_kind"] == "saved"

    def test_following_kind_on_the_header(
        self, app_client, watchlist: WatchlistStore, jellyfin_client: AsyncMock
    ):
        watchlist.add(MediaType.SHOW, SHOW_ID, WatchlistAddRequest(title="bb"))
        watchlist.follow(SHOW_ID, FollowRequest(start=FollowStart(mode=FollowStartMode.BEGINNING)))
        jellyfin_client.library_tmdb_ids.return_value = LibraryTmdbIdsResponse(
            media_type=MediaType.SHOW, tmdb_ids=[]
        )
        body = app_client.get(f"/api/v1/shows/{SHOW_ID}").json()
        assert body["on_watchlist"] is True
        assert body["watchlist_kind"] == "following"

    def test_tmdb_unavailable_relays_as_503(self, app_client, torrent_client: AsyncMock):
        torrent_client.series_episodes.side_effect = _tmdb_down()
        resp = app_client.get(f"/api/v1/shows/{SHOW_ID}")
        assert resp.status_code == 503
        assert resp.json()["code"] == ErrorCode.TMDB_UNAVAILABLE.value

    def test_requires_api_key(self, unauthed_client):
        assert unauthed_client.get(f"/api/v1/shows/{SHOW_ID}").status_code == 403


@pytest.mark.usefixtures("downstream")
class TestQueuedJobId:
    def test_episode_job_covers_only_that_episode(self, app_client, store: JobStore):
        job = store.create_job(
            release_name="r", media_type=MediaType.SHOW, tmdb_id=SHOW_ID, season=1, episode=2
        )
        body = app_client.get(f"/api/v1/shows/{SHOW_ID}").json()
        assert _by_key(body, "queued_job_id") == {
            (1, 1): None,
            (1, 2): job.id,
            (2, 1): None,
            (2, 2): None,
        }

    def test_season_job_covers_every_episode_of_the_season(self, app_client, store: JobStore):
        job = store.create_job(
            release_name="r", media_type=MediaType.SHOW, tmdb_id=SHOW_ID, season=2
        )
        body = app_client.get(f"/api/v1/shows/{SHOW_ID}").json()
        assert _by_key(body, "queued_job_id") == {
            (1, 1): None,
            (1, 2): None,
            (2, 1): job.id,
            (2, 2): job.id,
        }

    def test_series_job_covers_every_episode(self, app_client, store: JobStore):
        job = store.create_job(release_name="r", media_type=MediaType.SHOW, tmdb_id=SHOW_ID)
        body = app_client.get(f"/api/v1/shows/{SHOW_ID}").json()
        assert set(_by_key(body, "queued_job_id").values()) == {job.id}

    def test_terminal_jobs_do_not_count(self, app_client, store: JobStore):
        for status in (JobStatus.DONE, JobStatus.NEEDS_ATTENTION, JobStatus.DELETED):
            store.create_job(
                release_name="r", media_type=MediaType.SHOW, tmdb_id=SHOW_ID, status=status
            )
        body = app_client.get(f"/api/v1/shows/{SHOW_ID}").json()
        assert set(_by_key(body, "queued_job_id").values()) == {None}

    def test_failed_job_still_counts_as_queued(self, app_client, store: JobStore):
        job = store.create_job(
            release_name="r", media_type=MediaType.SHOW, tmdb_id=SHOW_ID, status=JobStatus.FAILED
        )
        body = app_client.get(f"/api/v1/shows/{SHOW_ID}").json()
        assert set(_by_key(body, "queued_job_id").values()) == {job.id}

    def test_other_titles_and_movies_are_ignored(self, app_client, store: JobStore):
        store.create_job(release_name="r", media_type=MediaType.SHOW, tmdb_id=OTHER_SHOW_ID)
        store.create_job(release_name="r", media_type=MediaType.MOVIE, tmdb_id=SHOW_ID)
        body = app_client.get(f"/api/v1/shows/{SHOW_ID}").json()
        assert set(_by_key(body, "queued_job_id").values()) == {None}
