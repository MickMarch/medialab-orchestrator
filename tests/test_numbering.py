"""Show numbering across the orchestrator: a TMDB single-season show whose
releases are named by release seasons and absolute numbers. Covers the
follow poll, the browse view, rename parsing and the stored follow options.

Fixture show: canonical S01E01-06; release S1 = E1-2, S2 = E3-4, S3 = E5-6.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from medialab_contracts import (
    Episode,
    EpisodeKey,
    EpisodeNumber,
    FollowRequest,
    FollowStart,
    FollowStartMode,
    LibraryEpisodesResponse,
    LibraryTmdbIdsResponse,
    MediaType,
    NumberingMode,
    Season,
    SeasonFollowMode,
    SeriesEpisodesResponse,
    ShowNumbering,
    WatchlistAddRequest,
)

from medialab_orchestrator.clients import torrent_downloader
from medialab_orchestrator.clients.torrent_downloader import TorrentDownloaderClient
from medialab_orchestrator.services import follow as follow_module
from medialab_orchestrator.services.follow import FollowPoller
from medialab_orchestrator.services.numbering import numbering_group
from medialab_orchestrator.services.rename import EpisodeUnparseableError, parse_episode
from medialab_orchestrator.services.shows import browse_show
from medialab_orchestrator.store import JobStore, WatchlistStore

SHOW_ID = 95479
TITLE = "Jujutsu Kaisen"
GROUP_ID = "seasons-group"
TODAY = datetime.now(UTC).date()
RESOLUTION = "1080p"
SPLIT = (2, 2, 2)
LAST_SEASON = len(SPLIT)
FIRST_OF_LAST = 5
DETAIL = {"status": "success", "data": {"name": TITLE, "first_air_date": "2020-10-03"}}


def _numbering() -> ShowNumbering:
    episodes, canonical = [], 1
    for release_season, size in enumerate(SPLIT, start=1):
        for release_episode in range(1, size + 1):
            episodes.append(
                EpisodeNumber(
                    season=1,
                    episode=canonical,
                    release_season=release_season,
                    release_episode=release_episode,
                    absolute=canonical,
                )
            )
            canonical += 1
    return ShowNumbering(
        tmdb_id=SHOW_ID, group_id=GROUP_ID, group_name="Seasons", episodes=episodes
    )


def _listing(*, unaired: set[int] | None = None) -> SeriesEpisodesResponse:
    unaired = unaired or set()
    return SeriesEpisodesResponse(
        tmdb_id=SHOW_ID,
        status="Returning Series",
        seasons=[Season(season=1, episode_count=sum(SPLIT))],
        episodes=[
            Episode(
                season=1,
                episode=n,
                air_date=None if n in unaired else TODAY - timedelta(days=30 - n),
            )
            for n in range(1, sum(SPLIT) + 1)
        ],
    )


def _pick(name: str) -> dict:
    return {"fileName": name, "fileUrl": f"magnet:?xt=urn:btih:{name}", "nbSeeders": 80}


@pytest.fixture
def show(watchlist: WatchlistStore, torrent_client: AsyncMock, jellyfin_client: AsyncMock, mocker):
    mocker.patch.object(follow_module.config, "follow_delay_hours", 0)
    mocker.patch.object(follow_module.config, "follow_max_submissions_per_tick", 10)
    mocker.patch.object(follow_module.config, "discord_notify_webhook_url", None)
    torrent_client.series_episodes.return_value = _listing()
    torrent_client.show_numbering.side_effect = None
    torrent_client.show_numbering.return_value = _numbering()
    torrent_client.tmdb_detail.return_value = DETAIL
    torrent_client.pick_torrent.return_value = None
    torrent_client.download.side_effect = lambda **kw: {
        "torrent_hash": hashlib.sha1(kw["source_url"].encode()).hexdigest()
    }
    jellyfin_client.library_episodes.return_value = LibraryEpisodesResponse(
        tmdb_id=SHOW_ID, episodes=[EpisodeKey(season=1, episode=n) for n in (1, 2)]
    )
    jellyfin_client.library_tmdb_ids.return_value = LibraryTmdbIdsResponse(
        media_type=MediaType.SHOW, tmdb_ids=[SHOW_ID]
    )
    watchlist.add(MediaType.SHOW, SHOW_ID, WatchlistAddRequest(title=TITLE))
    return watchlist.follow(
        SHOW_ID,
        FollowRequest(
            start=FollowStart(mode=FollowStartMode.FROM, season=1, episode=FIRST_OF_LAST),
            resolution=RESOLUTION,
            prefer_multi_audio=True,
        ),
    )


@pytest.fixture
def poller(store: JobStore, watchlist: WatchlistStore, torrent_client, jellyfin_client):
    return FollowPoller(
        store=store,
        watchlist=watchlist,
        torrent_client=torrent_client,
        jellyfin_client=jellyfin_client,
    )


class TestFollowPoll:
    async def test_complete_release_season_is_fetched_as_its_pack(
        self, show, poller: FollowPoller, torrent_client, watchlist: WatchlistStore, store: JobStore
    ) -> None:
        torrent_client.pick_torrent.return_value = _pick("Jujutsu Kaisen S03 1080p")
        submitted = await poller.check_show(watchlist.get(MediaType.SHOW, SHOW_ID))
        assert submitted == ["S03"]
        call = torrent_client.pick_torrent.await_args
        assert call.kwargs["season"] == LAST_SEASON
        assert call.kwargs.get("episode") is None
        assert call.kwargs["prefer_multi_audio"] is True
        assert set(watchlist.submissions(SHOW_ID)) == {(1, 5), (1, 6)}
        job = store.list_jobs_for_title(MediaType.SHOW, SHOW_ID)[0]
        assert (job.season, job.episode) == (LAST_SEASON, None)
        assert watchlist.season_state(SHOW_ID, LAST_SEASON).mode is SeasonFollowMode.PACK

    async def test_airing_release_season_picks_by_release_code_and_absolute(
        self, show, poller: FollowPoller, torrent_client, watchlist: WatchlistStore, store: JobStore
    ) -> None:
        torrent_client.series_episodes.return_value = _listing(unaired={6})
        torrent_client.pick_torrent.return_value = _pick("[SubsPlease] Jujutsu Kaisen - 05 (1080p)")
        submitted = await poller.check_show(watchlist.get(MediaType.SHOW, SHOW_ID))
        assert submitted == ["S03E01"]
        kwargs = torrent_client.pick_torrent.await_args.kwargs
        assert (kwargs["season"], kwargs["episode"], kwargs["absolute"]) == (3, 1, FIRST_OF_LAST)
        assert set(watchlist.submissions(SHOW_ID)) == {(1, FIRST_OF_LAST)}
        job = store.list_jobs_for_title(MediaType.SHOW, SHOW_ID)[0]
        assert (job.season, job.episode) == (LAST_SEASON, 1)

    async def test_numbering_follows_the_chosen_group(
        self, show, poller: FollowPoller, torrent_client, watchlist: WatchlistStore
    ) -> None:
        watchlist.follow(
            SHOW_ID,
            FollowRequest(
                start=FollowStart(mode=FollowStartMode.FROM, season=1, episode=FIRST_OF_LAST),
                numbering=NumberingMode.GROUP,
                episode_group_id=GROUP_ID,
            ),
        )
        await poller.check_show(watchlist.get(MediaType.SHOW, SHOW_ID))
        torrent_client.show_numbering.assert_awaited_with(SHOW_ID, GROUP_ID)

    async def test_unmapped_show_keeps_tmdb_seasons_and_no_absolute(
        self, show, poller: FollowPoller, torrent_client, watchlist: WatchlistStore
    ) -> None:
        torrent_client.show_numbering.return_value = ShowNumbering(tmdb_id=SHOW_ID, episodes=[])
        torrent_client.series_episodes.return_value = _listing(unaired={6})
        await poller.check_show(watchlist.get(MediaType.SHOW, SHOW_ID))
        kwargs = torrent_client.pick_torrent.await_args.kwargs
        assert (kwargs["season"], kwargs["episode"]) == (1, FIRST_OF_LAST)
        assert kwargs.get("absolute") is None


class TestBrowse:
    async def test_episodes_carry_release_codes_and_pack_jobs_cover_their_release_season(
        self, show, torrent_client, jellyfin_client, store: JobStore, watchlist: WatchlistStore
    ) -> None:
        job = store.create_job(
            release_name="Jujutsu Kaisen S03", media_type=MediaType.SHOW, tmdb_id=SHOW_ID, season=3
        )
        browse = await browse_show(
            SHOW_ID,
            torrent=torrent_client,
            jellyfin=jellyfin_client,
            store=store,
            watchlist=watchlist,
        )
        by_episode = {e.episode: e for e in browse.episodes}
        assert (by_episode[5].release_season, by_episode[5].release_episode) == (3, 1)
        assert by_episode[5].queued_job_id == job.id
        assert by_episode[3].queued_job_id is None

    async def test_numbering_failure_is_identity(
        self, show, torrent_client, jellyfin_client, store: JobStore, watchlist: WatchlistStore
    ) -> None:
        torrent_client.show_numbering.side_effect = RuntimeError("down")
        browse = await browse_show(
            SHOW_ID,
            torrent=torrent_client,
            jellyfin=jellyfin_client,
            store=store,
            watchlist=watchlist,
        )
        assert all(e.release_season is None for e in browse.episodes)


class TestRenameParse:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Jujutsu Kaisen S03E01 1080p WEB.mkv", (1, [5])),
            ("[SubsPlease] Jujutsu Kaisen - 05 (1080p) [3D64E486].mkv", (1, [5])),
            ("Jujutsu Kaisen S01E05 1080p.mkv", (1, [5])),
            ("Jujutsu Kaisen S01E02 1080p.mkv", (1, [2])),
            ("Jujutsu Kaisen S03E01-E02 1080p.mkv", (1, [5, 6])),
        ],
    )
    def test_maps_to_canonical(self, name: str, expected: tuple[int, list[int]]) -> None:
        assert parse_episode(name, _numbering()) == expected

    def test_identity_numbering_reads_absolute_fansub_names(self) -> None:
        identity = ShowNumbering(
            tmdb_id=SHOW_ID,
            episodes=[
                EpisodeNumber(season=2, episode=1, release_season=2, release_episode=1, absolute=13)
            ],
        )
        assert parse_episode("[Group] Show - 13 (1080p) [ABCD1234].mkv", identity) == (2, [1])

    def test_without_numbering_behaviour_is_unchanged(self) -> None:
        assert parse_episode("Show S02E05 720p.mkv") == (2, [5])
        with pytest.raises(EpisodeUnparseableError):
            parse_episode("[SubsPlease] Show - 05 (1080p) [3D64E486].mkv")


class TestStoredOptions:
    def test_follow_keeps_numbering_and_audio(self, show, watchlist: WatchlistStore) -> None:
        follow = watchlist.get(MediaType.SHOW, SHOW_ID).follow
        assert follow.numbering is NumberingMode.AUTO
        assert follow.prefer_multi_audio is True
        assert numbering_group(follow) == "auto"

    def test_changing_numbering_clears_season_states(self, show, watchlist: WatchlistStore) -> None:
        watchlist.set_season_mode(SHOW_ID, LAST_SEASON, SeasonFollowMode.PACK_NOT_FOUND, tried=True)
        same = FollowRequest(
            start=FollowStart(mode=FollowStartMode.BEGINNING), prefer_multi_audio=True
        )
        watchlist.follow(SHOW_ID, same)
        assert LAST_SEASON in watchlist.season_states(SHOW_ID)
        changed = FollowRequest(
            start=FollowStart(mode=FollowStartMode.BEGINNING),
            numbering=NumberingMode.GROUP,
            episode_group_id=GROUP_ID,
        )
        item = watchlist.follow(SHOW_ID, changed)
        assert watchlist.season_states(SHOW_ID) == {}
        assert numbering_group(item.follow) == GROUP_ID

    def test_tmdb_numbering_group(self, show, watchlist: WatchlistStore) -> None:
        item = watchlist.follow(
            SHOW_ID,
            FollowRequest(start=FollowStart(mode=FollowStartMode.BEGINNING), numbering="tmdb"),
        )
        assert numbering_group(item.follow) == "tmdb"
        assert numbering_group(None) == "auto"


class TestClient:
    async def test_pick_sends_absolute_and_audio_preference(self, mocker) -> None:
        request = mocker.patch.object(
            TorrentDownloaderClient, "request", AsyncMock(return_value=None)
        )
        mocker.patch.object(torrent_downloader.config, "torrent_downloader_url", "http://td")
        await TorrentDownloaderClient().pick_torrent(
            TITLE,
            season=3,
            episode=1,
            resolution=RESOLUTION,
            min_seeders=1,
            absolute=FIRST_OF_LAST,
            prefer_multi_audio=True,
        )
        params = request.await_args.kwargs["params"]
        assert params["absolute"] == FIRST_OF_LAST
        assert params["prefer_multi_audio"] == "true"

    async def test_show_numbering(self, mocker) -> None:
        request = mocker.patch.object(
            TorrentDownloaderClient, "request", AsyncMock(return_value=_numbering().model_dump())
        )
        mocker.patch.object(torrent_downloader.config, "torrent_downloader_url", "http://td")
        numbering = await TorrentDownloaderClient().show_numbering(SHOW_ID, GROUP_ID)
        assert numbering == _numbering()
        args = request.await_args
        assert args.args == ("GET", f"/api/v1/search/tmdb/show/{SHOW_ID}/numbering")
        assert args.kwargs["params"] == {"group": GROUP_ID}


class TestEpisodeGroupsRoute:
    def test_proxies_the_downloader(self, app_client, torrent_client) -> None:
        from medialab_contracts import EpisodeGroupInfo, EpisodeGroupsResponse

        groups = EpisodeGroupsResponse(
            tmdb_id=SHOW_ID,
            groups=[EpisodeGroupInfo(id=GROUP_ID, name="Seasons", group_count=3, episode_count=6)],
            auto_group_id=GROUP_ID,
        )
        torrent_client.episode_groups.return_value = groups
        response = app_client.get(f"/api/v1/shows/{SHOW_ID}/episode-groups")
        assert response.status_code == 200
        assert response.json()["auto_group_id"] == GROUP_ID
