"""Discover reconcile: the desired placeholder set and the add/remove plan."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from medialab_contracts import (
    DiscoverItem,
    DiscoverResponse,
    FavoriteScope,
    JellyfinRequestEvent,
    LibraryTmdbIdsResponse,
    MediaType,
    PlaceholderInfo,
    PlaceholdersResponse,
    PlaceholderState,
    WatchlistAddRequest,
)

from medialab_orchestrator.core import config as config_module
from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.services.discover_reconcile import (
    Candidate,
    DiscoverReconciler,
    desired_placeholders,
    plan_reconcile,
)
from medialab_orchestrator.store import WatchlistStore

HEAT = Candidate(MediaType.MOVIE, 949, "Heat", 1995)
DUNE = Candidate(MediaType.MOVIE, 438631, "Dune", 2021)
SEVERANCE = Candidate(MediaType.SHOW, 95396, "Severance", 2022)
GODFATHER_ID = 238
USER = "user-1"
OTHER_USER = "user-2"


def _info(tmdb_id: int, state: PlaceholderState = PlaceholderState.AVAILABLE) -> PlaceholderInfo:
    return PlaceholderInfo(tmdb_id=tmdb_id, title="x", state=state)


def _discover(*items: Candidate) -> DiscoverResponse:
    return DiscoverResponse(
        items=[
            DiscoverItem(
                tmdb_id=c.tmdb_id,
                media_type=c.media_type,
                title=c.title,
                year=str(c.year) if c.year else None,
            )
            for c in items
        ],
        page=1,
        total_pages=1,
        cached_at=datetime.now(UTC),
    )


def _favorite(user: str, media_type: MediaType, tmdb_id: int) -> JellyfinRequestEvent:
    return JellyfinRequestEvent(
        jellyfin_user_id=user,
        item_id=f"item-{tmdb_id}",
        media_type=media_type,
        tmdb_id=tmdb_id,
        is_favorite=True,
    )


class TestDesiredPlaceholders:
    def test_union_deduplicated_minus_library(self) -> None:
        desired = desired_placeholders(
            [[HEAT, SEVERANCE], [HEAT, DUNE]],
            library={MediaType.MOVIE: {DUNE.tmdb_id}, MediaType.SHOW: set()},
        )
        assert set(desired) == {
            (MediaType.MOVIE, HEAT.tmdb_id),
            (MediaType.SHOW, SEVERANCE.tmdb_id),
        }

    def test_same_id_in_both_types_is_two_titles(self) -> None:
        show_heat = Candidate(MediaType.SHOW, HEAT.tmdb_id, "Other", None)
        desired = desired_placeholders([[HEAT, show_heat]], library={})
        assert len(desired) == 2  # noqa: PLR2004


class TestPlanReconcile:
    def test_adds_missing_and_removes_stale_available(self) -> None:
        desired = desired_placeholders([[HEAT]], library={})
        current = {MediaType.MOVIE: [_info(DUNE.tmdb_id)], MediaType.SHOW: []}
        plan = plan_reconcile(desired, current, complete=True)
        assert plan.add == [HEAT]
        assert plan.remove == [(MediaType.MOVIE, DUNE.tmdb_id)]

    def test_existing_placeholder_is_not_rewritten(self) -> None:
        desired = desired_placeholders([[HEAT]], library={})
        current = {MediaType.MOVIE: [_info(HEAT.tmdb_id, PlaceholderState.REQUESTED)]}
        plan = plan_reconcile(desired, current, complete=True)
        assert (plan.add, plan.remove) == ([], [])

    def test_never_removes_a_placeholder_in_flight(self) -> None:
        current = {
            MediaType.MOVIE: [
                _info(HEAT.tmdb_id, PlaceholderState.REQUESTED),
                _info(DUNE.tmdb_id, PlaceholderState.DOWNLOADING),
            ],
            MediaType.SHOW: [_info(SEVERANCE.tmdb_id, PlaceholderState.NOT_FOUND)],
        }
        plan = plan_reconcile({}, current, complete=True)
        assert plan.remove == []

    def test_incomplete_sources_only_add(self) -> None:
        desired = desired_placeholders([[HEAT]], library={})
        current = {MediaType.MOVIE: [_info(DUNE.tmdb_id)]}
        plan = plan_reconcile(desired, current, complete=False)
        assert (plan.add, plan.remove) == ([HEAT], [])


@pytest.fixture
def reconciler(
    watchlist: WatchlistStore, torrent_client: AsyncMock, jellyfin_client: AsyncMock, mocker
) -> DiscoverReconciler:
    mocker.patch.object(config_module.config, "jellyfin_request_users", USER)
    mocker.patch.object(config_module.config, "discover_recommendations_per_title", 5)
    jellyfin_client.library_tmdb_ids.side_effect = lambda media_type: LibraryTmdbIdsResponse(
        media_type=media_type, tmdb_ids=[]
    )
    jellyfin_client.placeholders.side_effect = lambda media_type: PlaceholdersResponse(
        media_type=media_type, placeholders=[]
    )
    jellyfin_client.favorites.return_value = []
    torrent_client.discover.return_value = _discover()
    torrent_client.recommendations.return_value = _discover()
    return DiscoverReconciler(
        watchlist=watchlist, torrent_client=torrent_client, jellyfin_client=jellyfin_client
    )


class TestReconciler:
    async def test_writes_popular_released_and_refreshes_once_per_type(
        self, reconciler, torrent_client, jellyfin_client
    ) -> None:
        torrent_client.discover.side_effect = lambda media_type, **_: (
            _discover(HEAT, DUNE) if media_type is MediaType.MOVIE else _discover(SEVERANCE)
        )
        plan = await reconciler.reconcile()

        assert {c.tmdb_id for c in plan.add} == {HEAT.tmdb_id, DUNE.tmdb_id, SEVERANCE.tmdb_id}
        for call in torrent_client.discover.await_args_list:
            assert call.kwargs == {"released": True}
        put = jellyfin_client.put_placeholder.await_args_list
        assert all(call.kwargs == {"refresh": False} for call in put)
        heat_body = next(call.args[2] for call in put if call.args[1] == HEAT.tmdb_id)
        assert (heat_body.title, heat_body.year, heat_body.state) == (
            "Heat",
            1995,
            PlaceholderState.AVAILABLE,
        )
        refreshed = [call.args[0] for call in jellyfin_client.refresh_discover.await_args_list]
        assert sorted(refreshed) == [MediaType.MOVIE, MediaType.SHOW]

    async def test_watchlist_entries_get_placeholders(
        self, reconciler, watchlist, jellyfin_client
    ) -> None:
        watchlist.add(MediaType.MOVIE, HEAT.tmdb_id, WatchlistAddRequest(title="Heat", year="1995"))
        plan = await reconciler.reconcile()
        assert plan.add == [HEAT]

    async def test_recommendations_seeded_by_allowed_users_library_favorites(
        self, reconciler, torrent_client, jellyfin_client, mocker
    ) -> None:
        mocker.patch.object(config_module.config, "jellyfin_request_users", f"{USER},{OTHER_USER}")
        jellyfin_client.favorites.side_effect = lambda user, scope: [
            _favorite(user, MediaType.MOVIE, GODFATHER_ID)
        ]
        torrent_client.recommendations.return_value = _discover(HEAT)

        plan = await reconciler.reconcile()

        assert plan.add == [HEAT]
        scopes = {call.args[1] for call in jellyfin_client.favorites.await_args_list}
        assert scopes == {FavoriteScope.LIBRARY}
        torrent_client.recommendations.assert_awaited_once_with(
            MediaType.MOVIE, GODFATHER_ID, released=True, limit=5
        )

    async def test_no_allowed_users_means_no_recommendations(
        self, reconciler, jellyfin_client, torrent_client, mocker
    ) -> None:
        mocker.patch.object(config_module.config, "jellyfin_request_users", "")
        await reconciler.reconcile()
        jellyfin_client.favorites.assert_not_awaited()
        torrent_client.recommendations.assert_not_awaited()

    async def test_zero_per_title_skips_recommendations(
        self, reconciler, jellyfin_client, torrent_client, mocker
    ) -> None:
        mocker.patch.object(config_module.config, "discover_recommendations_per_title", 0)
        jellyfin_client.favorites.return_value = [_favorite(USER, MediaType.MOVIE, GODFATHER_ID)]
        await reconciler.reconcile()
        torrent_client.recommendations.assert_not_awaited()

    async def test_library_titles_are_left_out(self, reconciler, torrent_client, jellyfin_client):
        torrent_client.discover.return_value = _discover(HEAT)
        jellyfin_client.library_tmdb_ids.side_effect = lambda media_type: LibraryTmdbIdsResponse(
            media_type=media_type,
            tmdb_ids=[HEAT.tmdb_id] if media_type is MediaType.MOVIE else [],
        )
        plan = await reconciler.reconcile()
        assert plan.add == []
        jellyfin_client.refresh_discover.assert_not_awaited()

    async def test_stale_available_removed_in_flight_kept(
        self, reconciler, jellyfin_client
    ) -> None:
        jellyfin_client.placeholders.side_effect = lambda media_type: PlaceholdersResponse(
            media_type=media_type,
            placeholders=[_info(HEAT.tmdb_id), _info(DUNE.tmdb_id, PlaceholderState.REQUESTED)]
            if media_type is MediaType.MOVIE
            else [],
        )
        plan = await reconciler.reconcile()
        assert plan.remove == [(MediaType.MOVIE, HEAT.tmdb_id)]
        jellyfin_client.delete_placeholder.assert_awaited_once_with(
            MediaType.MOVIE, HEAT.tmdb_id, refresh=False
        )
        jellyfin_client.refresh_discover.assert_awaited_once_with(MediaType.MOVIE)

    async def test_a_failed_source_adds_but_never_removes(
        self, reconciler, torrent_client, jellyfin_client
    ) -> None:
        torrent_client.discover.side_effect = AppException(
            status_code=503, code=ErrorCode.TMDB_UNAVAILABLE, detail="down"
        )
        jellyfin_client.placeholders.side_effect = lambda media_type: PlaceholdersResponse(
            media_type=media_type,
            placeholders=[_info(DUNE.tmdb_id)] if media_type is MediaType.MOVIE else [],
        )
        plan = await reconciler.reconcile()
        assert plan.remove == []
        jellyfin_client.delete_placeholder.assert_not_awaited()

    async def test_one_failed_write_does_not_stop_the_rest(
        self, reconciler, torrent_client, jellyfin_client
    ) -> None:
        torrent_client.discover.side_effect = lambda media_type, **_: (
            _discover(HEAT, DUNE) if media_type is MediaType.MOVIE else _discover()
        )
        jellyfin_client.put_placeholder.side_effect = [
            AppException(status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="x"),
            None,
        ]
        await reconciler.reconcile()
        assert jellyfin_client.put_placeholder.await_count == 2  # noqa: PLR2004
        jellyfin_client.refresh_discover.assert_awaited_once_with(MediaType.MOVIE)

    async def test_library_read_failure_aborts_before_any_write(
        self, reconciler, jellyfin_client
    ) -> None:
        jellyfin_client.library_tmdb_ids.side_effect = AppException(
            status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="x"
        )
        with pytest.raises(AppException):
            await reconciler.reconcile()
        jellyfin_client.put_placeholder.assert_not_awaited()

    async def test_tick_never_raises(self, reconciler, jellyfin_client) -> None:
        jellyfin_client.library_tmdb_ids.side_effect = RuntimeError("boom")
        await reconciler.tick()


class TestRunLoop:
    async def test_reconciles_at_start_then_sleeps_the_interval(self, reconciler, mocker):
        mocker.patch.object(config_module.config, "discover_refresh_seconds", 100)
        tick = mocker.patch.object(reconciler, "tick", AsyncMock())
        sleep = mocker.patch(
            "medialab_orchestrator.services.discover_reconcile.asyncio.sleep",
            AsyncMock(side_effect=[None, StopAsyncIteration]),
        )
        with pytest.raises(StopAsyncIteration):
            await reconciler.run()
        assert tick.await_count == 2  # noqa: PLR2004
        assert [call.args[0] for call in sleep.await_args_list] == [100.0, 100.0]

    async def test_zero_pauses_without_reconciling(self, reconciler, mocker):
        mocker.patch.object(config_module.config, "discover_refresh_seconds", 0)
        tick = mocker.patch.object(reconciler, "tick", AsyncMock())
        mocker.patch(
            "medialab_orchestrator.services.discover_reconcile.asyncio.sleep",
            AsyncMock(side_effect=StopAsyncIteration),
        )
        with pytest.raises(StopAsyncIteration):
            await reconciler.run()
        tick.assert_not_awaited()
