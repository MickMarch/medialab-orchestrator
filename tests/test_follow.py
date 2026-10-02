"""The follow poll: wanted-episode computation, the tick, the Discord notice
and the episode view, Retry and Check now routes."""

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from medialab_contracts import (
    Episode,
    EpisodeState,
    FollowRequest,
    FollowStart,
    FollowStartMode,
    FollowState,
    LibraryEpisodesResponse,
    LibraryTmdbIdsResponse,
    MediaType,
    Season,
    SeasonFollowMode,
    SeriesEpisodesResponse,
    ShowBrowseResponse,
    SubmissionState,
    WatchlistAddRequest,
    WatchlistItem,
    WatchlistKind,
)

from medialab_orchestrator.clients import torrent_downloader
from medialab_orchestrator.clients.torrent_downloader import TorrentDownloaderClient
from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.services import follow as follow_module
from medialab_orchestrator.services import notify
from medialab_orchestrator.services.follow import (
    FollowPoller,
    annotate_follow,
    episode_code,
    pack_eligible,
    season_code,
    wanted_episodes,
)
from medialab_orchestrator.store import JobStatus, JobStore, WatchlistStore

SHOW_ID = 1396
OTHER_SHOW_ID = 2
TITLE = "Breaking Bad"
# Early in the day, so an episode dated today is still inside the default delay.
NOW = datetime(2026, 9, 27, 6, 0, tzinfo=UTC)
TODAY = NOW.date()
DELAY_HOURS = 12
WEBHOOK_URL = "https://discord.example/webhook"
RESOLUTION = "720p"
MIN_SEEDERS = 50
PER_TICK = 2
PACK_SEEDERS = 20
PACK_TIMEOUT = 30
RETRY_TIMEOUT = 90
RETRY_SEEDERS = 5
DETAIL = {
    "status": "success",
    "data": {"name": TITLE, "first_air_date": "2008-01-20", "poster_path": None, "overview": ""},
}


def _episode(season: int, episode: int, days_ago: int | None, **flags) -> EpisodeState:
    air_date = TODAY - timedelta(days=days_ago) if days_ago is not None else None
    return EpisodeState(season=season, episode=episode, air_date=air_date, **flags)


def _browse(*episodes: EpisodeState, tmdb_id: int = SHOW_ID) -> ShowBrowseResponse:
    return ShowBrowseResponse(
        tmdb_id=tmdb_id,
        title=TITLE,
        seasons=[Season(season=1), Season(season=2)],
        episodes=list(episodes),
    )


def _listing(
    *episodes: EpisodeState, next_episode: Episode | None = None
) -> SeriesEpisodesResponse:
    """What the downloader returns; browse_show derives the state itself."""
    return SeriesEpisodesResponse(
        tmdb_id=SHOW_ID,
        status="Returning",
        seasons=[Season(season=1), Season(season=2)],
        episodes=[Episode(**e.model_dump(include=set(Episode.model_fields))) for e in episodes],
        next_episode=next_episode,
    )


def _follow(
    mode: FollowStartMode = FollowStartMode.BEGINNING, *, followed_days_ago: int = 30, **start
) -> FollowState:
    return FollowState(
        start=FollowStart(mode=mode, **start),
        resolution=RESOLUTION,
        followed_at=NOW - timedelta(days=followed_days_ago),
    )


def _wanted(
    browse: ShowBrowseResponse, follow: FollowState, submissions: dict | None = None
) -> list[tuple[int, int]]:
    episodes = wanted_episodes(browse, follow, submissions or {}, now=NOW, delay_hours=DELAY_HOURS)
    return [(e.season, e.episode) for e in episodes]


class TestWantedEpisodes:
    def test_beginning_wants_every_aired_episode_in_air_order(self):
        browse = _browse(_episode(2, 1, 3), _episode(1, 2, 10), _episode(1, 1, 20))
        assert _wanted(browse, _follow()) == [(1, 1), (1, 2), (2, 1)]

    def test_from_starts_at_the_season_and_episode_inclusive(self):
        browse = _browse(
            _episode(1, 5, 40), _episode(2, 4, 30), _episode(2, 5, 20), _episode(3, 1, 10)
        )
        follow = _follow(FollowStartMode.FROM, season=2, episode=5)
        assert _wanted(browse, follow) == [(2, 5), (3, 1)]

    def test_new_only_includes_the_follow_day(self):
        browse = _browse(_episode(1, 1, 8), _episode(1, 2, 7), _episode(1, 3, 6))
        follow = _follow(FollowStartMode.NEW_ONLY, followed_days_ago=7)
        assert _wanted(browse, follow) == [(1, 2), (1, 3)]

    def test_specials_are_never_wanted(self):
        browse = _browse(_episode(0, 1, 10), _episode(1, 1, 10))
        assert _wanted(browse, _follow()) == [(1, 1)]

    def test_unaired_and_undated_episodes_are_not_wanted(self):
        browse = _browse(_episode(1, 1, 10), _episode(1, 2, -1), _episode(1, 3, None))
        assert _wanted(browse, _follow()) == [(1, 1)]

    def test_the_delay_holds_a_fresh_episode_back(self):
        fresh = EpisodeState(season=1, episode=1, air_date=TODAY)
        browse = _browse(fresh)
        assert wanted_episodes(browse, _follow(), {}, now=NOW, delay_hours=DELAY_HOURS) == []
        later = NOW + timedelta(hours=DELAY_HOURS)
        assert wanted_episodes(browse, _follow(), {}, now=later, delay_hours=DELAY_HOURS) == [fresh]
        assert wanted_episodes(browse, _follow(), {}, now=NOW, delay_hours=0) == [fresh]

    def test_library_and_queued_episodes_are_not_wanted(self):
        browse = _browse(
            _episode(1, 1, 10, in_library=True),
            _episode(1, 2, 10, queued_job_id="job-1"),
            _episode(1, 3, 10),
        )
        assert _wanted(browse, _follow()) == [(1, 3)]

    @pytest.mark.parametrize("state", [SubmissionState.SUBMITTED, SubmissionState.IGNORED])
    def test_any_submission_blocks(self, state: SubmissionState):
        browse = _browse(_episode(1, 1, 10), _episode(1, 2, 10))
        assert _wanted(browse, _follow(), {(1, 1): (state, "job-1")}) == [(1, 2)]

    def test_annotate_sets_submitted_and_wanted(self):
        browse = _browse(_episode(1, 1, 10), _episode(1, 2, 10), _episode(1, 3, -1))
        submissions = {(1, 1): (SubmissionState.IGNORED, "job-1")}
        annotated = annotate_follow(
            browse, _follow(), submissions, now=NOW, delay_hours=DELAY_HOURS
        )
        by_key = {(e.season, e.episode): (e.submitted, e.wanted) for e in annotated.episodes}
        assert by_key == {
            (1, 1): (SubmissionState.IGNORED, False),
            (1, 2): (None, True),
            (1, 3): (None, False),
        }

    def test_episode_code(self):
        assert episode_code(2, 5) == "S02E05"
        assert episode_code(12, 105) == "S12E105"


def _pick(season: int, episode: int) -> dict:
    return {
        "fileName": f"Show.S{season:02d}E{episode:02d}.720p",
        "fileUrl": f"magnet:?xt=urn:btih:{season}{episode}",
        "nbSeeders": 80,
    }


@pytest.fixture
def poller(
    store: JobStore,
    watchlist: WatchlistStore,
    torrent_client: AsyncMock,
    jellyfin_client: AsyncMock,
) -> FollowPoller:
    return FollowPoller(
        store=store,
        watchlist=watchlist,
        torrent_client=torrent_client,
        jellyfin_client=jellyfin_client,
    )


@pytest.fixture
def notice(mocker) -> AsyncMock:
    return mocker.patch.object(follow_module, "post_discord", AsyncMock())


@pytest.fixture
def followed(
    watchlist: WatchlistStore, torrent_client: AsyncMock, jellyfin_client: AsyncMock, mocker
) -> WatchlistItem:
    """One followed show from the beginning with three aired episodes, none
    picked yet, the settings pinned and the webhook unset."""
    mocker.patch.object(follow_module.config, "follow_delay_hours", DELAY_HOURS)
    mocker.patch.object(follow_module.config, "follow_minimum_seeders", MIN_SEEDERS)
    mocker.patch.object(follow_module.config, "follow_max_submissions_per_tick", PER_TICK)
    mocker.patch.object(follow_module.config, "discord_notify_webhook_url", None)
    mocker.patch.object(follow_module.config, "follow_pack_minimum_seeders", PACK_SEEDERS)
    mocker.patch.object(follow_module.config, "follow_pack_timeout_seconds", PACK_TIMEOUT)
    mocker.patch.object(follow_module.config, "follow_pack_retry_timeout_seconds", RETRY_TIMEOUT)
    mocker.patch.object(follow_module.config, "follow_pack_retry_minimum_seeders", RETRY_SEEDERS)
    # Season 1 still has an unaired episode and season 2 has a next episode
    # coming, so every season takes the episode path here.
    torrent_client.series_episodes.return_value = _listing(
        _episode(1, 2, 10),
        _episode(1, 1, 20),
        _episode(1, 3, None),
        _episode(2, 1, 5),
        next_episode=Episode(season=2, episode=2, air_date=TODAY + timedelta(days=7)),
    )
    torrent_client.tmdb_detail.return_value = DETAIL
    torrent_client.download.side_effect = lambda **kw: {
        "torrent_hash": hashlib.sha1(kw["source_url"].encode()).hexdigest()
    }
    torrent_client.pick_torrent.return_value = None
    jellyfin_client.library_episodes.return_value = LibraryEpisodesResponse(
        tmdb_id=SHOW_ID, episodes=[]
    )
    jellyfin_client.library_tmdb_ids.return_value = LibraryTmdbIdsResponse(
        media_type=MediaType.SHOW, tmdb_ids=[]
    )
    watchlist.add(MediaType.SHOW, SHOW_ID, WatchlistAddRequest(title=TITLE))
    return watchlist.follow(
        SHOW_ID,
        FollowRequest(start=FollowStart(mode=FollowStartMode.BEGINNING), resolution=RESOLUTION),
    )


def _downloader_down() -> AppException:
    return AppException(status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="down")


@pytest.mark.usefixtures("followed", "notice")
class TestTick:
    async def test_submits_in_air_order_and_records_each(
        self, poller: FollowPoller, store: JobStore, watchlist: WatchlistStore, torrent_client
    ):
        torrent_client.pick_torrent.side_effect = [_pick(1, 1), _pick(1, 2)]
        await poller.tick()
        jobs = sorted(store.list_jobs(), key=lambda j: (j.season, j.episode))
        assert [(j.season, j.episode) for j in jobs] == [(1, 1), (1, 2)]
        assert all(j.media_type is MediaType.SHOW and j.tmdb_id == SHOW_ID for j in jobs)
        assert jobs[0].release_name == "Show.S01E01.720p"
        assert jobs[0].status is JobStatus.DOWNLOAD_SUBMITTED
        assert watchlist.submissions(SHOW_ID) == {
            (1, 1): (SubmissionState.SUBMITTED, jobs[0].id),
            (1, 2): (SubmissionState.SUBMITTED, jobs[1].id),
        }
        torrent_client.download.assert_any_await(
            source_url="magnet:?xt=urn:btih:11", media_type=MediaType.SHOW, tmdb_id=SHOW_ID
        )

    async def test_pick_passes_title_scope_resolution_and_seeder_floor(
        self, poller: FollowPoller, torrent_client
    ):
        await poller.tick()
        torrent_client.pick_torrent.assert_any_await(
            TITLE, season=1, episode=1, resolution=RESOLUTION, min_seeders=MIN_SEEDERS
        )

    async def test_an_airing_season_never_asks_for_a_pack(
        self, poller: FollowPoller, torrent_client, watchlist: WatchlistStore
    ):
        await poller.tick()
        assert all("episode" in call.kwargs for call in torrent_client.pick_torrent.await_args_list)
        assert watchlist.season_states(SHOW_ID) == {}

    async def test_no_candidate_moves_on_to_the_next_episode(
        self, poller: FollowPoller, store: JobStore, torrent_client
    ):
        torrent_client.pick_torrent.side_effect = [None, None, _pick(2, 1)]
        await poller.tick()
        assert [(j.season, j.episode) for j in store.list_jobs()] == [(2, 1)]
        assert torrent_client.pick_torrent.await_count == 3

    async def test_caps_submissions_per_tick(
        self, poller: FollowPoller, store: JobStore, torrent_client
    ):
        torrent_client.pick_torrent.side_effect = [_pick(1, 1), _pick(1, 2), _pick(2, 1)]
        await poller.tick()
        assert len(store.list_jobs()) == PER_TICK
        assert torrent_client.pick_torrent.await_count == PER_TICK

    async def test_marks_checked_and_last_submitted(
        self, poller: FollowPoller, watchlist: WatchlistStore, torrent_client
    ):
        torrent_client.pick_torrent.side_effect = [_pick(1, 1), None, _pick(2, 1)]
        await poller.tick()
        item = watchlist.get(MediaType.SHOW, SHOW_ID)
        assert item is not None and item.follow is not None
        assert item.follow.last_checked_at is not None
        assert item.follow.last_submitted == "S02E01"

    async def test_marks_checked_without_a_submission(
        self, poller: FollowPoller, watchlist: WatchlistStore
    ):
        await poller.tick()
        item = watchlist.get(MediaType.SHOW, SHOW_ID)
        assert item is not None and item.follow is not None
        assert item.follow.last_checked_at is not None
        assert item.follow.last_submitted is None

    async def test_skips_paused_follows(
        self, poller: FollowPoller, watchlist: WatchlistStore, torrent_client
    ):
        watchlist.set_paused(SHOW_ID, True)
        await poller.tick()
        torrent_client.series_episodes.assert_not_awaited()
        torrent_client.pick_torrent.assert_not_awaited()

    async def test_skips_saved_titles(
        self, poller: FollowPoller, watchlist: WatchlistStore, torrent_client
    ):
        watchlist.unfollow(SHOW_ID)
        await poller.tick()
        torrent_client.series_episodes.assert_not_awaited()

    async def test_downloader_error_on_pick_stops_the_show_but_marks_checked(
        self, poller: FollowPoller, store: JobStore, watchlist: WatchlistStore, torrent_client
    ):
        torrent_client.pick_torrent.side_effect = [_pick(1, 1), _downloader_down(), _pick(2, 1)]
        await poller.tick()
        assert [(j.season, j.episode) for j in store.list_jobs()] == [(1, 1)]
        item = watchlist.get(MediaType.SHOW, SHOW_ID)
        assert item is not None and item.follow is not None
        assert item.follow.last_checked_at is not None
        assert item.follow.last_submitted == "S01E01"

    async def test_one_show_raising_does_not_stop_the_sweep(
        self, poller: FollowPoller, store: JobStore, watchlist: WatchlistStore, torrent_client
    ):
        watchlist.add(MediaType.SHOW, OTHER_SHOW_ID, WatchlistAddRequest(title="other"))
        watchlist.follow(OTHER_SHOW_ID, FollowRequest(start=FollowStart(mode="beginning")))
        torrent_client.series_episodes.side_effect = [
            RuntimeError("boom"),
            _listing(_episode(1, 1, 20)),
        ]
        torrent_client.pick_torrent.return_value = _pick(1, 1)
        await poller.tick()
        assert [j.tmdb_id for j in store.list_jobs()] == [SHOW_ID]

    async def test_no_notice_without_a_webhook(
        self, poller: FollowPoller, torrent_client, notice: AsyncMock
    ):
        torrent_client.pick_torrent.side_effect = [_pick(1, 1), None, None]
        await poller.tick()
        notice.assert_not_awaited()

    async def test_notice_per_submission_when_the_webhook_is_set(
        self, poller: FollowPoller, torrent_client, notice: AsyncMock, mocker
    ):
        mocker.patch.object(follow_module.config, "discord_notify_webhook_url", WEBHOOK_URL)
        torrent_client.pick_torrent.side_effect = [_pick(1, 1), None, _pick(2, 1)]
        await poller.tick()
        assert notice.await_args_list[0].args == (
            WEBHOOK_URL,
            f"Following {TITLE}: submitted S01E01 (Show.S01E01.720p)",
        )
        assert notice.await_args_list[1].args[1].startswith(f"Following {TITLE}: submitted S02E01")


class TestRunLoop:
    async def test_run_rereads_the_interval_each_sleep(self, poller: FollowPoller, mocker) -> None:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 1:
                mocker.patch.object(follow_module.config, "follow_poll_interval_seconds", 0)
            elif len(sleeps) == 2:
                mocker.patch.object(follow_module.config, "follow_poll_interval_seconds", 90)
            else:
                raise RuntimeError("stop")

        mocker.patch.object(follow_module.config, "follow_poll_interval_seconds", 300)
        mocker.patch.object(follow_module.asyncio, "sleep", fake_sleep)
        tick = mocker.patch.object(poller, "tick", AsyncMock())
        with pytest.raises(RuntimeError):
            await poller.run()
        assert sleeps == [300.0, follow_module.PAUSED_POLL_RECHECK_SECONDS, 90.0]
        assert tick.await_count == 1


class TestNotify:
    @pytest.fixture
    def http(self, mocker) -> MagicMock:
        client = MagicMock()
        client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))
        factory = mocker.patch.object(notify.httpx, "AsyncClient")
        factory.return_value.__aenter__ = AsyncMock(return_value=client)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        return client

    async def test_posts_the_content_as_json(self, http: MagicMock):
        await notify.post_discord(WEBHOOK_URL, "hello")
        http.post.assert_awaited_once_with(WEBHOOK_URL, json={"content": "hello"})

    async def test_never_raises(self, http: MagicMock):
        http.post.side_effect = httpx.ConnectError("refused")
        await notify.post_discord(WEBHOOK_URL, "hello")

    def test_follow_notice_wording(self):
        assert (
            notify.follow_notice(TITLE, "S02E05", "Show.S02E05")
            == "Following Breaking Bad: submitted S02E05 (Show.S02E05)"
        )


def _pack(season: int) -> dict:
    return {
        "fileName": f"Show.S{season:02d}.COMPLETE.720p",
        "fileUrl": f"magnet:?xt=urn:btih:pack{season}",
        "nbSeeders": 40,
    }


def _pick_calls(torrent_client) -> list[tuple]:
    """(season, episode, min_seeders, timeout_seconds) per pick call."""
    return [
        (
            c.kwargs["season"],
            c.kwargs.get("episode"),
            c.kwargs["min_seeders"],
            c.kwargs.get("timeout_seconds"),
        )
        for c in torrent_client.pick_torrent.await_args_list
    ]


@pytest.fixture
def pack_followed(followed, watchlist: WatchlistStore, torrent_client) -> WatchlistItem:
    """Season 1 complete (two aired episodes), season 2 airing (one aired, one
    not), followed from S01E02 so the start season is still pack-eligible."""
    torrent_client.series_episodes.return_value = _listing(
        _episode(1, 1, 20), _episode(1, 2, 10), _episode(2, 1, 5), _episode(2, 2, None)
    )
    return watchlist.follow(
        SHOW_ID,
        FollowRequest(
            start=FollowStart(mode=FollowStartMode.FROM, season=1, episode=2),
            resolution=RESOLUTION,
        ),
    )


class TestPackEligibility:
    def test_complete_untouched_season_is_eligible(self):
        browse = _browse(_episode(1, 1, 20), _episode(1, 2, 10), _episode(2, 1, 5))
        assert pack_eligible(browse, 1, {}, now=NOW, delay_hours=DELAY_HOURS)

    def test_airing_or_undated_season_is_not(self):
        browse = _browse(_episode(1, 1, 20), _episode(1, 2, None))
        assert not pack_eligible(browse, 1, {}, now=NOW, delay_hours=DELAY_HOURS)
        fresh = _browse(_episode(1, 1, 20), _episode(1, 2, 0))
        assert not pack_eligible(fresh, 1, {}, now=NOW, delay_hours=DELAY_HOURS)

    def test_anything_fetched_queued_or_submitted_disqualifies(self):
        in_library = _browse(_episode(1, 1, 20, in_library=True), _episode(1, 2, 10))
        assert not pack_eligible(in_library, 1, {}, now=NOW, delay_hours=DELAY_HOURS)
        queued = _browse(_episode(1, 1, 20, queued_job_id="j"), _episode(1, 2, 10))
        assert not pack_eligible(queued, 1, {}, now=NOW, delay_hours=DELAY_HOURS)
        clean = _browse(_episode(1, 1, 20), _episode(1, 2, 10))
        submissions = {(1, 1): (SubmissionState.IGNORED, None)}
        assert not pack_eligible(clean, 1, submissions, now=NOW, delay_hours=DELAY_HOURS)

    def test_a_season_tmdb_still_expects_more_of_is_not(self):
        upcoming = Episode(season=1, episode=3, air_date=TODAY + timedelta(days=7))
        browse = _browse(_episode(1, 1, 20), _episode(1, 2, 10)).model_copy(
            update={"next_episode": upcoming}
        )
        assert not pack_eligible(browse, 1, {}, now=NOW, delay_hours=DELAY_HOURS)
        short = _browse(_episode(1, 1, 20), _episode(1, 2, 10)).model_copy(
            update={"seasons": [Season(season=1, episode_count=10)]}
        )
        assert not pack_eligible(short, 1, {}, now=NOW, delay_hours=DELAY_HOURS)
        counted = short.model_copy(update={"seasons": [Season(season=1, episode_count=2)]})
        assert pack_eligible(counted, 1, {}, now=NOW, delay_hours=DELAY_HOURS)

    def test_specials_and_empty_seasons_are_not(self):
        browse = _browse(_episode(0, 1, 20), _episode(1, 1, 20))
        assert not pack_eligible(browse, 0, {}, now=NOW, delay_hours=DELAY_HOURS)
        assert not pack_eligible(browse, 5, {}, now=NOW, delay_hours=DELAY_HOURS)

    def test_season_code(self):
        assert season_code(3) == "S03"


@pytest.mark.usefixtures("pack_followed", "notice")
class TestPacks:
    async def test_complete_season_is_fetched_as_one_pack_and_the_airing_one_per_episode(
        self, poller: FollowPoller, store: JobStore, watchlist: WatchlistStore, torrent_client
    ):
        torrent_client.pick_torrent.side_effect = [_pack(1), _pick(2, 1)]
        submitted = await poller.check_show(watchlist.get(MediaType.SHOW, SHOW_ID))
        assert submitted == ["S01", "S02E01"]
        jobs = sorted(store.list_jobs(), key=lambda j: (j.season, j.episode or 0))
        assert [(j.season, j.episode) for j in jobs] == [(1, None), (2, 1)]
        assert jobs[0].release_name == "Show.S01.COMPLETE.720p"
        assert watchlist.submissions(SHOW_ID) == {
            (1, 1): (SubmissionState.SUBMITTED, jobs[0].id),
            (1, 2): (SubmissionState.SUBMITTED, jobs[0].id),
            (2, 1): (SubmissionState.SUBMITTED, jobs[1].id),
        }
        state = watchlist.season_state(SHOW_ID, 1)
        assert state is not None
        assert (state.mode, state.attempts, state.job_id) == (SeasonFollowMode.PACK, 1, jobs[0].id)
        assert _pick_calls(torrent_client) == [
            (1, None, PACK_SEEDERS, PACK_TIMEOUT),
            (2, 1, MIN_SEEDERS, None),
        ]
        item = watchlist.get(MediaType.SHOW, SHOW_ID)
        assert item is not None and item.follow is not None
        assert item.follow.last_submitted == "S02E01"

    async def test_missing_pack_marks_the_season_notifies_and_waits(
        self,
        poller: FollowPoller,
        store: JobStore,
        watchlist: WatchlistStore,
        torrent_client,
        notice: AsyncMock,
        mocker,
    ):
        mocker.patch.object(follow_module.config, "discord_notify_webhook_url", WEBHOOK_URL)
        torrent_client.pick_torrent.side_effect = [None, _pick(2, 1)]
        await poller.tick()
        assert [(j.season, j.episode) for j in store.list_jobs()] == [(2, 1)]
        state = watchlist.season_state(SHOW_ID, 1)
        assert state is not None
        assert (state.mode, state.attempts) == (SeasonFollowMode.PACK_NOT_FOUND, 1)
        assert state.last_tried_at is not None
        assert notice.await_args_list[0].args == (
            WEBHOOK_URL,
            f"Following {TITLE}: no season pack found for S01; "
            "choose how to continue on the Watchlist",
        )
        # Next tick: season 1 is left alone, nothing else is wanted.
        torrent_client.pick_torrent.reset_mock()
        torrent_client.pick_torrent.side_effect = None
        torrent_client.pick_torrent.return_value = None
        await poller.tick()
        torrent_client.pick_torrent.assert_not_awaited()

    @pytest.mark.parametrize(
        ("mode", "expected_call"),
        [
            (SeasonFollowMode.PACK_RETRY_TIMEOUT, (1, None, PACK_SEEDERS, RETRY_TIMEOUT)),
            (SeasonFollowMode.PACK_RETRY_SEEDERS, (1, None, RETRY_SEEDERS, PACK_TIMEOUT)),
            (SeasonFollowMode.PACK, (1, None, PACK_SEEDERS, PACK_TIMEOUT)),
        ],
    )
    async def test_a_retry_choice_uses_its_profile_and_is_consumed(
        self, poller: FollowPoller, watchlist: WatchlistStore, torrent_client, mode, expected_call
    ):
        watchlist.set_season_mode(SHOW_ID, 1, SeasonFollowMode.PACK_NOT_FOUND, tried=True)
        watchlist.set_season_mode(SHOW_ID, 1, mode)
        torrent_client.pick_torrent.side_effect = [None, None]
        await poller.tick()
        assert _pick_calls(torrent_client)[0] == expected_call
        state = watchlist.season_state(SHOW_ID, 1)
        assert state is not None
        assert (state.mode, state.attempts) == (SeasonFollowMode.PACK_NOT_FOUND, 2)

    async def test_a_successful_retry_lands_in_pack_with_the_job(
        self, poller: FollowPoller, watchlist: WatchlistStore, store: JobStore, torrent_client
    ):
        watchlist.set_season_mode(SHOW_ID, 1, SeasonFollowMode.PACK_NOT_FOUND, tried=True)
        watchlist.set_season_mode(SHOW_ID, 1, SeasonFollowMode.PACK_RETRY_SEEDERS)
        torrent_client.pick_torrent.side_effect = [_pack(1), None]
        await poller.tick()
        state = watchlist.season_state(SHOW_ID, 1)
        pack_job = next(j for j in store.list_jobs() if j.episode is None)
        assert state is not None
        assert (state.mode, state.attempts, state.job_id) == (SeasonFollowMode.PACK, 2, pack_job.id)

    async def test_episodes_mode_takes_the_episode_path(
        self, poller: FollowPoller, watchlist: WatchlistStore, torrent_client
    ):
        watchlist.set_season_mode(SHOW_ID, 1, SeasonFollowMode.PACK_NOT_FOUND, tried=True)
        watchlist.set_season_mode(SHOW_ID, 1, SeasonFollowMode.EPISODES)
        await poller.tick()
        # The follow starts at S01E02, so that is the first episode asked for alone.
        assert _pick_calls(torrent_client)[:2] == [
            (1, 2, MIN_SEEDERS, None),
            (2, 1, MIN_SEEDERS, None),
        ]

    async def test_an_episode_already_in_the_library_sends_the_rest_one_by_one(
        self, poller: FollowPoller, torrent_client, jellyfin_client
    ):
        jellyfin_client.library_episodes.return_value = LibraryEpisodesResponse(
            tmdb_id=SHOW_ID, episodes=[{"season": 1, "episode": 1}]
        )
        await poller.tick()
        assert _pick_calls(torrent_client)[0] == (1, 2, MIN_SEEDERS, None)

    async def test_a_pack_counts_once_against_the_tick_cap(
        self, poller: FollowPoller, store: JobStore, torrent_client, mocker
    ):
        mocker.patch.object(follow_module.config, "follow_max_submissions_per_tick", 1)
        torrent_client.pick_torrent.side_effect = [_pack(1), _pick(2, 1)]
        await poller.tick()
        assert [(j.season, j.episode) for j in store.list_jobs()] == [(1, None)]
        assert torrent_client.pick_torrent.await_count == 1

    async def test_downloader_error_on_the_pack_stops_the_show_for_the_tick(
        self, poller: FollowPoller, store: JobStore, watchlist: WatchlistStore, torrent_client
    ):
        torrent_client.pick_torrent.side_effect = [_downloader_down(), _pick(2, 1)]
        await poller.tick()
        assert store.list_jobs() == []
        assert watchlist.season_states(SHOW_ID) == {}


class TestPickClient:
    async def test_pick_sends_the_scope_and_treats_404_as_no_candidate(self, mocker):
        request = mocker.patch.object(TorrentDownloaderClient, "request", AsyncMock())
        request.return_value = None
        mocker.patch.object(torrent_downloader.config, "torrent_downloader_url", "http://td")
        client = TorrentDownloaderClient()
        assert (
            await client.pick_torrent(
                TITLE, season=2, episode=5, resolution=RESOLUTION, min_seeders=MIN_SEEDERS
            )
            is None
        )
        request.assert_awaited_once_with(
            "GET",
            "/api/v1/search/torrents/pick",
            params={
                "query": TITLE,
                "season": 2,
                "episode": 5,
                "resolution": RESOLUTION,
                "min_seeders": MIN_SEEDERS,
            },
            accept=(404,),
        )

    async def test_season_pick_omits_the_episode_and_carries_the_timeout(self, mocker):
        request = mocker.patch.object(TorrentDownloaderClient, "request", AsyncMock())
        request.return_value = _pack(2)
        mocker.patch.object(torrent_downloader.config, "torrent_downloader_url", "http://td")
        await TorrentDownloaderClient().pick_torrent(
            TITLE, season=2, resolution=RESOLUTION, min_seeders=PACK_SEEDERS, timeout_seconds=90
        )
        assert request.await_args.kwargs["params"] == {
            "query": TITLE,
            "season": 2,
            "resolution": RESOLUTION,
            "min_seeders": PACK_SEEDERS,
            "timeout_seconds": 90,
        }

    async def test_pick_returns_the_result_body(self, mocker):
        request = mocker.patch.object(TorrentDownloaderClient, "request", AsyncMock())
        request.return_value = _pick(2, 5)
        mocker.patch.object(torrent_downloader.config, "torrent_downloader_url", "http://td")
        result = await TorrentDownloaderClient().pick_torrent(
            TITLE, season=2, episode=5, resolution=RESOLUTION, min_seeders=MIN_SEEDERS
        )
        assert result == _pick(2, 5)


EPISODES_URL = f"/api/v1/watchlist/show/{SHOW_ID}/episodes"
CHECK_URL = f"/api/v1/watchlist/show/{SHOW_ID}/follow/check"
DECISION_URL = f"/api/v1/watchlist/show/{SHOW_ID}/seasons/1/decision"


@pytest.mark.usefixtures("followed", "notice")
class TestSeasonRoutes:
    def test_episodes_carry_the_season_states(self, app_client, watchlist: WatchlistStore):
        assert app_client.get(EPISODES_URL).json()["seasons_follow"] == []
        watchlist.set_season_mode(SHOW_ID, 1, SeasonFollowMode.PACK_NOT_FOUND, tried=True)
        states = app_client.get(EPISODES_URL).json()["seasons_follow"]
        assert [(s["season"], s["mode"], s["attempts"]) for s in states] == [
            (1, "pack_not_found", 1)
        ]

    def test_decision_needs_a_not_found_season(self, app_client, watchlist: WatchlistStore):
        assert app_client.post(DECISION_URL, json={"mode": "episodes"}).status_code == 409
        watchlist.set_season_mode(SHOW_ID, 1, SeasonFollowMode.EPISODES)
        assert app_client.post(DECISION_URL, json={"mode": "pack"}).status_code == 409

    def test_decision_sets_the_mode_and_keeps_the_attempts(
        self, app_client, watchlist: WatchlistStore
    ):
        watchlist.set_season_mode(SHOW_ID, 1, SeasonFollowMode.PACK_NOT_FOUND, tried=True)
        resp = app_client.post(DECISION_URL, json={"mode": "pack_retry_seeders"})
        assert resp.status_code == 200
        assert (resp.json()["mode"], resp.json()["attempts"]) == ("pack_retry_seeders", 1)
        state = watchlist.season_state(SHOW_ID, 1)
        assert state is not None and state.mode is SeasonFollowMode.PACK_RETRY_SEEDERS

    def test_decision_rejects_not_found_and_needs_a_follow(
        self, app_client, watchlist: WatchlistStore
    ):
        watchlist.set_season_mode(SHOW_ID, 1, SeasonFollowMode.PACK_NOT_FOUND, tried=True)
        assert app_client.post(DECISION_URL, json={"mode": "pack_not_found"}).status_code == 422
        watchlist.unfollow(SHOW_ID)
        assert app_client.post(DECISION_URL, json={"mode": "episodes"}).status_code == 404


@pytest.mark.usefixtures("followed", "notice")
class TestRoutes:
    def test_episodes_carry_submitted_and_wanted(self, app_client, watchlist: WatchlistStore):
        watchlist.record_submission(SHOW_ID, 1, 1, "job-1")
        resp = app_client.get(EPISODES_URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == TITLE
        assert body["watchlist_kind"] == WatchlistKind.FOLLOWING.value
        by_key = {
            (e["season"], e["episode"]): (e["submitted"], e["wanted"]) for e in body["episodes"]
        }
        assert by_key == {
            (1, 1): ("submitted", False),
            (1, 2): (None, True),
            (1, 3): (None, False),
            (2, 1): (None, True),
        }

    def test_episodes_need_a_follow(self, app_client, watchlist: WatchlistStore):
        watchlist.unfollow(SHOW_ID)
        resp = app_client.get(EPISODES_URL)
        assert resp.status_code == 404
        assert resp.json()["code"] == ErrorCode.WATCHLIST_ITEM_NOT_FOUND.value

    def test_episodes_on_a_movie_is_422(self, app_client):
        assert app_client.get(f"/api/v1/watchlist/movie/{SHOW_ID}/episodes").status_code == 422

    def test_retry_clears_the_submission(self, app_client, watchlist: WatchlistStore):
        watchlist.record_submission(SHOW_ID, 1, 1, "job-1")
        watchlist.ignore_submission_for_job("job-1")
        resp = app_client.delete(f"{EPISODES_URL}/1/1/submission")
        assert resp.status_code == 204
        assert watchlist.submissions(SHOW_ID) == {}
        assert app_client.delete(f"{EPISODES_URL}/1/1/submission").status_code == 204

    def test_check_runs_one_show_and_returns_what_it_submitted(
        self, app_client, store: JobStore, watchlist: WatchlistStore, torrent_client
    ):
        watchlist.add(MediaType.SHOW, OTHER_SHOW_ID, WatchlistAddRequest(title="other"))
        watchlist.follow(OTHER_SHOW_ID, FollowRequest(start=FollowStart(mode="beginning")))
        torrent_client.pick_torrent.side_effect = [_pick(1, 1), None, _pick(2, 1)]
        resp = app_client.post(CHECK_URL)
        assert resp.status_code == 200
        assert resp.json() == {"submitted": ["S01E01", "S02E01"]}
        assert {j.tmdb_id for j in store.list_jobs()} == {SHOW_ID}
        torrent_client.series_episodes.assert_awaited_once_with(SHOW_ID)

    def test_check_needs_a_follow(self, app_client, watchlist: WatchlistStore):
        watchlist.unfollow(SHOW_ID)
        assert app_client.post(CHECK_URL).status_code == 404

    def test_check_relays_tmdb_unavailable(self, app_client, torrent_client):
        torrent_client.series_episodes.side_effect = AppException(
            status_code=503, code=ErrorCode.TMDB_UNAVAILABLE, detail="down"
        )
        assert app_client.post(CHECK_URL).status_code == 503

    def test_routes_require_api_key(self, unauthed_client):
        assert unauthed_client.get(EPISODES_URL).status_code == 403
        assert unauthed_client.post(CHECK_URL).status_code == 403
