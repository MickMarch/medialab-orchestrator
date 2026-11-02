"""Jellyfin in-app requests: the request table and the request poll."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from medialab_contracts import (
    FavoriteScope,
    FollowRequest,
    FollowStart,
    FollowStartMode,
    JellyfinRequestEvent,
    LibraryTmdbIdsResponse,
    MediaType,
    PlaceholderInfo,
    PlaceholdersResponse,
    PlaceholderState,
    WatchlistAddRequest,
    WatchlistKind,
)

from medialab_orchestrator.core import config as config_module
from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.services.follow import FollowPoller
from medialab_orchestrator.services.requests import RequestPoller, movie_query
from medialab_orchestrator.store import JobStatus, JobStore, WatchlistStore
from medialab_orchestrator.store.requests import RequestStatus, RequestStore

HEAT_ID = 949
SEVERANCE_ID = 95396
USER = "user-1"
OTHER_USER = "user-2"
RETRY_HOURS = 12
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
PICK = {"fileName": "Heat.1995.1080p.BluRay.x264", "fileUrl": "magnet:?xt=urn:btih:abc"}
DETAILS = {
    (MediaType.MOVIE, HEAT_ID): {"title": "Heat", "release_date": "1995-12-15"},
    (MediaType.SHOW, SEVERANCE_ID): {"name": "Severance", "first_air_date": "2022-02-18"},
}


@pytest.fixture
def requests_store() -> RequestStore:
    return RequestStore(db_path=":memory:")


class Downstream:
    """Mutable Jellyfin state the mocked client answers from."""

    def __init__(self) -> None:
        self.library: dict[MediaType, list[int]] = {MediaType.MOVIE: [], MediaType.SHOW: []}
        self.placeholders: dict[MediaType, list[PlaceholderInfo]] = {
            MediaType.MOVIE: [PlaceholderInfo(tmdb_id=HEAT_ID, title="Heat", state="available")],
            MediaType.SHOW: [
                PlaceholderInfo(tmdb_id=SEVERANCE_ID, title="Severance", state="available")
            ],
        }
        self.favorites: dict[str, list[tuple[MediaType, int]]] = {}

    def set_state(self, media_type: MediaType, tmdb_id: int, state: PlaceholderState) -> None:
        self.placeholders[media_type] = [
            info.model_copy(update={"state": state}) if info.tmdb_id == tmdb_id else info
            for info in self.placeholders[media_type]
        ]


@pytest.fixture
def downstream(jellyfin_client: AsyncMock, torrent_client: AsyncMock, mocker) -> Downstream:
    state = Downstream()
    mocker.patch.object(config_module.config, "jellyfin_request_users", f"{USER},{OTHER_USER}")
    mocker.patch.object(config_module.config, "jellyfin_request_retry_hours", RETRY_HOURS)
    jellyfin_client.library_tmdb_ids.side_effect = lambda media_type: LibraryTmdbIdsResponse(
        media_type=media_type, tmdb_ids=state.library[media_type]
    )
    jellyfin_client.placeholders.side_effect = lambda media_type: PlaceholdersResponse(
        media_type=media_type, placeholders=state.placeholders[media_type]
    )

    def favorites(user_id: str, scope: FavoriteScope) -> list[JellyfinRequestEvent]:
        assert scope is FavoriteScope.DISCOVER
        return [
            JellyfinRequestEvent(
                jellyfin_user_id=user_id,
                item_id=f"item-{tmdb_id}",
                media_type=media_type,
                tmdb_id=tmdb_id,
                is_favorite=True,
            )
            for media_type, tmdb_id in state.favorites.get(user_id, [])
        ]

    jellyfin_client.favorites.side_effect = favorites
    torrent_client.tmdb_detail.side_effect = lambda media_type, tmdb_id: {
        "data": DETAILS[(media_type, tmdb_id)]
    }
    torrent_client.pick_torrent.return_value = PICK
    torrent_client.download.return_value = {"torrent_hash": "abc"}
    return state


@pytest.fixture
def follow_poller() -> AsyncMock:
    return AsyncMock(spec=FollowPoller)


@pytest.fixture
def poller(
    store: JobStore,
    watchlist: WatchlistStore,
    requests_store: RequestStore,
    torrent_client: AsyncMock,
    jellyfin_client: AsyncMock,
    follow_poller: AsyncMock,
    downstream: Downstream,
) -> RequestPoller:
    return RequestPoller(
        store=store,
        watchlist=watchlist,
        requests=requests_store,
        torrent_client=torrent_client,
        jellyfin_client=jellyfin_client,
        follow_poller=follow_poller,
    )


def _written_states(jellyfin_client: AsyncMock) -> list[tuple[int, PlaceholderState]]:
    return [
        (call.args[1], call.args[2].state)
        for call in jellyfin_client.put_placeholder.await_args_list
    ]


class TestRequestStore:
    def test_file_then_reopen_after_cancel(self, requests_store: RequestStore) -> None:
        requests_store.file(MediaType.MOVIE, HEAT_ID, title="Heat", year=1995, users={USER})
        requests_store.update(MediaType.MOVIE, HEAT_ID, status=RequestStatus.CANCELLED)
        assert requests_store.list_open() == []
        reopened = requests_store.file(
            MediaType.MOVIE, HEAT_ID, title="Heat", year=1995, users={OTHER_USER}
        )
        assert (reopened.status, reopened.users) == (RequestStatus.REQUESTED, {OTHER_USER})

    def test_update_round_trips(self, requests_store: RequestStore) -> None:
        requests_store.file(MediaType.SHOW, SEVERANCE_ID, title="Severance", year=None, users=[])
        updated = requests_store.update(
            MediaType.SHOW, SEVERANCE_ID, users={USER, OTHER_USER}, last_tried_at=NOW
        )
        assert updated.users == {USER, OTHER_USER}
        assert updated.last_tried_at == NOW


class TestMovieRequest:
    async def test_favorite_files_picks_and_shows_downloading(
        self, poller, downstream, requests_store, store, watchlist, torrent_client, jellyfin_client
    ) -> None:
        downstream.favorites[USER] = [(MediaType.MOVIE, HEAT_ID)]
        await poller.poll(now=NOW)

        request = requests_store.get(MediaType.MOVIE, HEAT_ID)
        assert request is not None
        assert (request.title, request.year, request.status) == (
            "Heat",
            1995,
            RequestStatus.SUBMITTED,
        )
        torrent_client.pick_torrent.assert_awaited_once()
        call = torrent_client.pick_torrent.await_args
        assert call.args == (movie_query("Heat", 1995),)
        assert call.kwargs["media_type"] is MediaType.MOVIE
        assert "season" not in call.kwargs
        [job] = store.list_jobs_for_title(MediaType.MOVIE, HEAT_ID)
        assert job.status is JobStatus.DOWNLOAD_SUBMITTED
        assert watchlist.get(MediaType.MOVIE, HEAT_ID) is not None
        assert _written_states(jellyfin_client) == [(HEAT_ID, PlaceholderState.DOWNLOADING)]

    async def test_disallowed_users_file_nothing(
        self, poller, downstream, requests_store, jellyfin_client, mocker
    ) -> None:
        mocker.patch.object(config_module.config, "jellyfin_request_users", "")
        downstream.favorites["stranger"] = [(MediaType.MOVIE, HEAT_ID)]
        await poller.poll(now=NOW)
        jellyfin_client.favorites.assert_not_awaited()
        assert requests_store.list_open() == []

    async def test_two_users_one_request(self, poller, downstream, requests_store) -> None:
        downstream.favorites[USER] = [(MediaType.MOVIE, HEAT_ID)]
        downstream.favorites[OTHER_USER] = [(MediaType.MOVIE, HEAT_ID)]
        await poller.poll(now=NOW)
        [request] = requests_store.list_open()
        assert request.users == {USER, OTHER_USER}

    async def test_favorite_without_a_placeholder_is_ignored(
        self, poller, downstream, requests_store
    ) -> None:
        downstream.favorites[USER] = [(MediaType.MOVIE, 1)]
        await poller.poll(now=NOW)
        assert requests_store.list_open() == []

    async def test_title_lookup_failure_files_next_tick(
        self, poller, downstream, requests_store, torrent_client
    ) -> None:
        downstream.favorites[USER] = [(MediaType.MOVIE, HEAT_ID)]
        torrent_client.tmdb_detail.side_effect = AppException(
            status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="down"
        )
        await poller.poll(now=NOW)
        assert requests_store.list_open() == []

    async def test_not_found_retries_after_the_interval(
        self, poller, downstream, requests_store, torrent_client, jellyfin_client
    ) -> None:
        downstream.favorites[USER] = [(MediaType.MOVIE, HEAT_ID)]
        torrent_client.pick_torrent.return_value = None
        await poller.poll(now=NOW)
        request = requests_store.get(MediaType.MOVIE, HEAT_ID)
        assert request is not None and request.status is RequestStatus.NOT_FOUND
        assert _written_states(jellyfin_client) == [(HEAT_ID, PlaceholderState.NOT_FOUND)]
        downstream.set_state(MediaType.MOVIE, HEAT_ID, PlaceholderState.NOT_FOUND)

        await poller.poll(now=NOW + timedelta(hours=RETRY_HOURS - 1))
        assert torrent_client.pick_torrent.await_count == 1

        torrent_client.pick_torrent.return_value = PICK
        await poller.poll(now=NOW + timedelta(hours=RETRY_HOURS))
        assert torrent_client.pick_torrent.await_count == 2  # noqa: PLR2004
        request = requests_store.get(MediaType.MOVIE, HEAT_ID)
        assert request is not None and request.status is RequestStatus.SUBMITTED

    async def test_abandoned_jobs_send_the_movie_back_to_not_found(
        self, poller, downstream, requests_store, store, torrent_client
    ) -> None:
        downstream.favorites[USER] = [(MediaType.MOVIE, HEAT_ID)]
        await poller.poll(now=NOW)
        [job] = store.list_jobs_for_title(MediaType.MOVIE, HEAT_ID)
        store.update_job(job.id, status=JobStatus.DISMISSED)
        await poller.poll(now=NOW)
        request = requests_store.get(MediaType.MOVIE, HEAT_ID)
        assert request is not None and request.status is RequestStatus.NOT_FOUND
        assert torrent_client.pick_torrent.await_count == 1

    async def test_closes_when_the_title_reaches_the_library(
        self, poller, downstream, requests_store, jellyfin_client
    ) -> None:
        downstream.favorites[USER] = [(MediaType.MOVIE, HEAT_ID)]
        await poller.poll(now=NOW)
        downstream.library[MediaType.MOVIE] = [HEAT_ID]
        await poller.poll(now=NOW)
        request = requests_store.get(MediaType.MOVIE, HEAT_ID)
        assert request is not None and request.status is RequestStatus.CLOSED
        jellyfin_client.delete_placeholder.assert_awaited_once_with(MediaType.MOVIE, HEAT_ID)

    async def test_unchanged_state_is_not_rewritten(
        self, poller, downstream, jellyfin_client
    ) -> None:
        downstream.favorites[USER] = [(MediaType.MOVIE, HEAT_ID)]
        await poller.poll(now=NOW)
        downstream.set_state(MediaType.MOVIE, HEAT_ID, PlaceholderState.DOWNLOADING)
        await poller.poll(now=NOW)
        assert jellyfin_client.put_placeholder.await_count == 1


class TestCancel:
    @pytest.fixture
    def not_found(self, poller, downstream, torrent_client) -> Downstream:
        downstream.favorites[USER] = [(MediaType.MOVIE, HEAT_ID)]
        torrent_client.pick_torrent.return_value = None
        return downstream

    async def test_unfavorite_before_submit_cancels_and_frees_the_placeholder(
        self, poller, not_found, requests_store, watchlist, jellyfin_client
    ) -> None:
        await poller.poll(now=NOW)
        not_found.set_state(MediaType.MOVIE, HEAT_ID, PlaceholderState.NOT_FOUND)
        not_found.favorites[USER] = []
        await poller.poll(now=NOW)
        request = requests_store.get(MediaType.MOVIE, HEAT_ID)
        assert request is not None and request.status is RequestStatus.CANCELLED
        assert watchlist.get(MediaType.MOVIE, HEAT_ID) is None
        assert _written_states(jellyfin_client)[-1] == (HEAT_ID, PlaceholderState.AVAILABLE)

    async def test_cancels_only_when_the_last_requester_leaves(
        self, poller, not_found, requests_store
    ) -> None:
        not_found.favorites[OTHER_USER] = [(MediaType.MOVIE, HEAT_ID)]
        await poller.poll(now=NOW)
        not_found.favorites[USER] = []
        await poller.poll(now=NOW)
        request = requests_store.get(MediaType.MOVIE, HEAT_ID)
        assert request is not None
        assert (request.status, request.users) == (RequestStatus.NOT_FOUND, {OTHER_USER})

    async def test_unfavorite_after_submit_is_ignored(
        self, poller, downstream, requests_store
    ) -> None:
        downstream.favorites[USER] = [(MediaType.MOVIE, HEAT_ID)]
        await poller.poll(now=NOW)
        downstream.favorites[USER] = []
        await poller.poll(now=NOW)
        request = requests_store.get(MediaType.MOVIE, HEAT_ID)
        assert request is not None and request.status is RequestStatus.SUBMITTED

    async def test_unreadable_favorites_cancel_nobody(
        self, poller, not_found, requests_store, jellyfin_client
    ) -> None:
        await poller.poll(now=NOW)
        jellyfin_client.favorites.side_effect = AppException(
            status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="x"
        )
        await poller.poll(now=NOW)
        request = requests_store.get(MediaType.MOVIE, HEAT_ID)
        assert request is not None and request.status is RequestStatus.NOT_FOUND


class TestShowRequest:
    async def test_follows_from_the_beginning_and_checks_at_once(
        self, poller, downstream, requests_store, watchlist, follow_poller, torrent_client
    ) -> None:
        downstream.favorites[USER] = [(MediaType.SHOW, SEVERANCE_ID)]
        await poller.poll(now=NOW)
        item = watchlist.get(MediaType.SHOW, SEVERANCE_ID)
        assert item is not None and item.kind is WatchlistKind.FOLLOWING
        assert item.follow is not None
        assert item.follow.start.mode is FollowStartMode.BEGINNING
        follow_poller.check_show.assert_awaited_once()
        torrent_client.pick_torrent.assert_not_awaited()
        request = requests_store.get(MediaType.SHOW, SEVERANCE_ID)
        assert request is not None and request.status is RequestStatus.SUBMITTED

    async def test_an_existing_follow_is_kept(
        self, poller, downstream, watchlist, follow_poller
    ) -> None:
        watchlist.add(MediaType.SHOW, SEVERANCE_ID, WatchlistAddRequest(title="Severance"))
        watchlist.follow(
            SEVERANCE_ID, FollowRequest(start=FollowStart(mode=FollowStartMode.NEW_ONLY))
        )
        downstream.favorites[USER] = [(MediaType.SHOW, SEVERANCE_ID)]
        await poller.poll(now=NOW)
        item = watchlist.get(MediaType.SHOW, SEVERANCE_ID)
        assert item is not None and item.follow is not None
        assert item.follow.start.mode is FollowStartMode.NEW_ONLY

    async def test_first_check_failure_still_submits(
        self, poller, downstream, requests_store, follow_poller
    ) -> None:
        follow_poller.check_show.side_effect = AppException(
            status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="x"
        )
        downstream.favorites[USER] = [(MediaType.SHOW, SEVERANCE_ID)]
        await poller.poll(now=NOW)
        request = requests_store.get(MediaType.SHOW, SEVERANCE_ID)
        assert request is not None and request.status is RequestStatus.SUBMITTED

    async def test_state_follows_the_shows_jobs(
        self, poller, downstream, store, jellyfin_client
    ) -> None:
        downstream.favorites[USER] = [(MediaType.SHOW, SEVERANCE_ID)]
        await poller.poll(now=NOW)
        assert _written_states(jellyfin_client) == [(SEVERANCE_ID, PlaceholderState.REQUESTED)]
        downstream.set_state(MediaType.SHOW, SEVERANCE_ID, PlaceholderState.REQUESTED)
        store.create_job(
            release_name="Severance.S01", media_type=MediaType.SHOW, tmdb_id=SEVERANCE_ID, season=1
        )
        await poller.poll(now=NOW)
        assert _written_states(jellyfin_client)[-1] == (
            SEVERANCE_ID,
            PlaceholderState.DOWNLOADING,
        )


class TestTick:
    async def test_library_failure_aborts_before_favorites(
        self, poller, downstream, jellyfin_client
    ) -> None:
        jellyfin_client.library_tmdb_ids.side_effect = AppException(
            status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="x"
        )
        with pytest.raises(AppException):
            await poller.poll(now=NOW)
        jellyfin_client.favorites.assert_not_awaited()

    async def test_tick_never_raises(self, poller, jellyfin_client) -> None:
        jellyfin_client.library_tmdb_ids.side_effect = RuntimeError("boom")
        await poller.tick(now=NOW)

    async def test_one_failing_request_does_not_stop_the_rest(
        self, poller, downstream, requests_store, torrent_client
    ) -> None:
        downstream.favorites[USER] = [(MediaType.MOVIE, HEAT_ID), (MediaType.SHOW, SEVERANCE_ID)]
        torrent_client.pick_torrent.side_effect = AppException(
            status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="x"
        )
        await poller.poll(now=NOW)
        show = requests_store.get(MediaType.SHOW, SEVERANCE_ID)
        assert show is not None and show.status is RequestStatus.SUBMITTED
