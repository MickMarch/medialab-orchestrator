"""WatchlistStore unit tests: idempotent upsert, no-op remove, ordering, kinds,
follow state, submissions, and the wishlist_item -> watchlist_item migration."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from medialab_contracts import (
    DEFAULT_FOLLOW_RESOLUTION,
    FollowRequest,
    FollowStart,
    FollowStartMode,
    MediaType,
    SubmissionState,
    WatchlistAddRequest,
    WatchlistKind,
)

from medialab_orchestrator.store import JobStore, WatchlistItemNotFoundError, WatchlistStore
from medialab_orchestrator.store.watchlist import LEGACY_WATCHLIST_TABLE, WATCHLIST_TABLE

DUNE_ID = 438631
SHOW_ID = 1396
JOB_ID = "job-1"
NEW_JOB_ID = "job-2"
LEGACY_ADDED_AT = "2026-01-01T00:00:00+00:00"

_LEGACY_SCHEMA = f"""
CREATE TABLE {LEGACY_WATCHLIST_TABLE} (
    tmdb_id     INTEGER NOT NULL,
    media_type  TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    year        TEXT,
    poster_path TEXT,
    overview    TEXT    NOT NULL DEFAULT '',
    added_at    TEXT    NOT NULL,
    PRIMARY KEY (media_type, tmdb_id)
);
"""


def _request(title: str = "Dune", **overrides) -> WatchlistAddRequest:
    fields = {"title": title, "year": "2021", "poster_path": "/dune.jpg", "overview": "Sand."}
    fields.update(overrides)
    return WatchlistAddRequest(**fields)


def _follow(
    mode: FollowStartMode = FollowStartMode.NEW_ONLY,
    resolution: str = DEFAULT_FOLLOW_RESOLUTION,
    **start,
) -> FollowRequest:
    return FollowRequest(start=FollowStart(mode=mode, **start), resolution=resolution)


def _tables(db_path: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db_path: str, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


class TestAdd:
    def test_add_returns_the_stored_item(self, watchlist: WatchlistStore):
        item = watchlist.add(MediaType.MOVIE, DUNE_ID, _request())
        assert item.tmdb_id == DUNE_ID
        assert item.media_type is MediaType.MOVIE
        assert item.kind is WatchlistKind.SAVED
        assert item.title == "Dune"
        assert item.year == "2021"
        assert item.poster_path == "/dune.jpg"
        assert item.overview == "Sand."
        assert item.in_library is False
        assert item.follow is None

    def test_add_is_idempotent_and_keeps_added_at(self, watchlist: WatchlistStore):
        first = watchlist.add(MediaType.MOVIE, DUNE_ID, _request())
        second = watchlist.add(MediaType.MOVIE, DUNE_ID, _request(title="Dune: Part One"))
        assert len(watchlist.list()) == 1
        assert second.added_at == first.added_at
        assert second.title == "Dune: Part One"

    def test_repeat_add_keeps_the_follow(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, SHOW_ID, _request())
        watchlist.follow(SHOW_ID, _follow())
        item = watchlist.add(MediaType.SHOW, SHOW_ID, _request(title="renamed"))
        assert item.kind is WatchlistKind.FOLLOWING
        assert item.follow is not None

    def test_same_id_different_media_type_is_a_separate_row(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.MOVIE, DUNE_ID, _request())
        watchlist.add(MediaType.SHOW, DUNE_ID, _request())
        assert len(watchlist.list()) == 2

    def test_get_absent_is_none(self, watchlist: WatchlistStore):
        assert watchlist.get(MediaType.MOVIE, DUNE_ID) is None


class TestRemove:
    def test_remove_deletes_only_the_matching_row(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.MOVIE, DUNE_ID, _request())
        watchlist.add(MediaType.SHOW, DUNE_ID, _request())
        watchlist.remove(MediaType.MOVIE, DUNE_ID)
        assert watchlist.keys(MediaType.MOVIE) == {}
        assert watchlist.keys(MediaType.SHOW) == {DUNE_ID: WatchlistKind.SAVED}

    def test_remove_absent_is_a_noop(self, watchlist: WatchlistStore):
        watchlist.remove(MediaType.MOVIE, DUNE_ID)
        assert watchlist.list() == []

    def test_remove_drops_a_follow_entirely(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, SHOW_ID, _request())
        watchlist.follow(SHOW_ID, _follow())
        watchlist.remove(MediaType.SHOW, SHOW_ID)
        assert watchlist.list() == []

    def test_remove_saved_leaves_a_follow(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, SHOW_ID, _request())
        watchlist.follow(SHOW_ID, _follow())
        watchlist.add(MediaType.SHOW, DUNE_ID, _request())
        watchlist.remove_saved(MediaType.SHOW, SHOW_ID)
        watchlist.remove_saved(MediaType.SHOW, DUNE_ID)
        assert watchlist.keys(MediaType.SHOW) == {SHOW_ID: WatchlistKind.FOLLOWING}


class TestList:
    def test_newest_first(self, watchlist: WatchlistStore):
        for tmdb_id in (1, 2, 3):
            watchlist.add(MediaType.MOVIE, tmdb_id, _request(title=str(tmdb_id)))
        assert [item.tmdb_id for item in watchlist.list()] == [3, 2, 1]

    def test_filter_by_media_type(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.MOVIE, 1, _request())
        watchlist.add(MediaType.SHOW, 2, _request())
        assert [item.tmdb_id for item in watchlist.list(MediaType.SHOW)] == [2]

    def test_filter_by_kind(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, 1, _request())
        watchlist.add(MediaType.SHOW, 2, _request())
        watchlist.follow(2, _follow())
        assert [item.tmdb_id for item in watchlist.list(kind=WatchlistKind.FOLLOWING)] == [2]
        assert [item.tmdb_id for item in watchlist.list(kind=WatchlistKind.SAVED)] == [1]

    def test_filter_by_media_type_and_kind(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.MOVIE, 1, _request())
        watchlist.add(MediaType.SHOW, 2, _request())
        assert watchlist.list(MediaType.MOVIE, WatchlistKind.FOLLOWING) == []
        assert [i.tmdb_id for i in watchlist.list(MediaType.MOVIE, WatchlistKind.SAVED)] == [1]

    def test_keys_are_scoped_to_media_type_with_kind(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.MOVIE, 1, _request())
        watchlist.add(MediaType.MOVIE, 2, _request())
        watchlist.add(MediaType.SHOW, 3, _request())
        watchlist.follow(3, _follow())
        assert watchlist.keys(MediaType.MOVIE) == {1: WatchlistKind.SAVED, 2: WatchlistKind.SAVED}
        assert watchlist.keys(MediaType.SHOW) == {3: WatchlistKind.FOLLOWING}


class TestFollow:
    def test_follow_stores_start_and_resolution(self, watchlist: WatchlistStore):
        added = watchlist.add(MediaType.SHOW, SHOW_ID, _request())
        item = watchlist.follow(
            SHOW_ID, _follow(FollowStartMode.FROM, season=2, episode=5, resolution="4K")
        )
        assert item.kind is WatchlistKind.FOLLOWING
        assert item.added_at == added.added_at
        assert item.follow is not None
        assert item.follow.start.mode is FollowStartMode.FROM
        assert (item.follow.start.season, item.follow.start.episode) == (2, 5)
        assert item.follow.resolution == "4K"
        assert item.follow.paused is False
        assert item.follow.followed_at.tzinfo is not None
        assert item.follow.last_checked_at is None
        assert item.follow.last_submitted is None

    def test_follow_defaults_resolution(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, SHOW_ID, _request())
        item = watchlist.follow(SHOW_ID, _follow(FollowStartMode.BEGINNING))
        assert item.follow is not None
        assert (
            item.follow.resolution == FollowRequest(start=FollowStart(mode="new_only")).resolution
        )

    def test_follow_is_repeatable_and_replaces_the_start(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, SHOW_ID, _request())
        watchlist.follow(SHOW_ID, _follow(FollowStartMode.FROM, season=1, episode=1))
        item = watchlist.follow(SHOW_ID, _follow(FollowStartMode.NEW_ONLY))
        assert item.follow is not None
        assert item.follow.start.mode is FollowStartMode.NEW_ONLY
        assert item.follow.start.season is None
        assert len(watchlist.list()) == 1

    def test_follow_unpauses(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, SHOW_ID, _request())
        watchlist.follow(SHOW_ID, _follow())
        watchlist.set_paused(SHOW_ID, True)
        item = watchlist.follow(SHOW_ID, _follow())
        assert item.follow is not None
        assert item.follow.paused is False

    def test_follow_needs_the_saved_row(self, watchlist: WatchlistStore):
        with pytest.raises(WatchlistItemNotFoundError):
            watchlist.follow(SHOW_ID, _follow())

    def test_follow_ignores_a_movie_with_the_same_id(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.MOVIE, SHOW_ID, _request())
        with pytest.raises(WatchlistItemNotFoundError):
            watchlist.follow(SHOW_ID, _follow())
        assert watchlist.keys(MediaType.MOVIE) == {SHOW_ID: WatchlistKind.SAVED}

    def test_unfollow_keeps_the_row_as_saved_and_clears_follow_columns(
        self, watchlist: WatchlistStore
    ):
        watchlist.add(MediaType.SHOW, SHOW_ID, _request())
        watchlist.follow(SHOW_ID, _follow())
        watchlist.mark_checked(SHOW_ID, datetime.now(UTC), last_submitted="S01E01")
        watchlist.unfollow(SHOW_ID)
        item = watchlist.get(MediaType.SHOW, SHOW_ID)
        assert item is not None
        assert item.kind is WatchlistKind.SAVED
        assert item.follow is None
        refollowed = watchlist.follow(SHOW_ID, _follow())
        assert refollowed.follow is not None
        assert refollowed.follow.last_submitted is None
        assert refollowed.follow.last_checked_at is None

    def test_unfollow_absent_is_a_noop(self, watchlist: WatchlistStore):
        watchlist.unfollow(SHOW_ID)
        assert watchlist.list() == []

    def test_pause_and_resume_round_trip(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, SHOW_ID, _request())
        watchlist.follow(SHOW_ID, _follow())
        paused = watchlist.set_paused(SHOW_ID, True)
        assert paused.follow is not None
        assert paused.follow.paused is True
        resumed = watchlist.set_paused(SHOW_ID, False)
        assert resumed.follow is not None
        assert resumed.follow.paused is False

    def test_pause_needs_a_follow(self, watchlist: WatchlistStore):
        watchlist.add(MediaType.SHOW, SHOW_ID, _request())
        with pytest.raises(WatchlistItemNotFoundError):
            watchlist.set_paused(SHOW_ID, True)

    def test_mark_checked_stamps_time_and_optionally_last_submitted(
        self, watchlist: WatchlistStore
    ):
        watchlist.add(MediaType.SHOW, SHOW_ID, _request())
        watchlist.follow(SHOW_ID, _follow())
        when = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
        watchlist.mark_checked(SHOW_ID, when, last_submitted="S02E05")
        item = watchlist.get(MediaType.SHOW, SHOW_ID)
        assert item is not None and item.follow is not None
        assert item.follow.last_checked_at == when
        assert item.follow.last_submitted == "S02E05"

        later = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
        watchlist.mark_checked(SHOW_ID, later)
        item = watchlist.get(MediaType.SHOW, SHOW_ID)
        assert item is not None and item.follow is not None
        assert item.follow.last_checked_at == later
        assert item.follow.last_submitted == "S02E05"


class TestSubmissions:
    def test_record_and_read(self, watchlist: WatchlistStore):
        watchlist.record_submission(SHOW_ID, 2, 5, JOB_ID)
        assert watchlist.submissions(SHOW_ID) == {(2, 5): (SubmissionState.SUBMITTED, JOB_ID)}
        assert watchlist.submissions(DUNE_ID) == {}

    def test_record_repeat_overwrites_the_job(self, watchlist: WatchlistStore):
        watchlist.record_submission(SHOW_ID, 2, 5, JOB_ID)
        watchlist.record_submission(SHOW_ID, 2, 5, NEW_JOB_ID)
        assert watchlist.submissions(SHOW_ID) == {(2, 5): (SubmissionState.SUBMITTED, NEW_JOB_ID)}

    def test_ignore_by_job_marks_only_that_job(self, watchlist: WatchlistStore):
        watchlist.record_submission(SHOW_ID, 2, 5, JOB_ID)
        watchlist.record_submission(SHOW_ID, 2, 6, NEW_JOB_ID)
        watchlist.ignore_submission_for_job(JOB_ID)
        assert watchlist.submissions(SHOW_ID) == {
            (2, 5): (SubmissionState.IGNORED, JOB_ID),
            (2, 6): (SubmissionState.SUBMITTED, NEW_JOB_ID),
        }

    def test_ignore_unknown_job_is_a_noop(self, watchlist: WatchlistStore):
        watchlist.ignore_submission_for_job("nope")
        assert watchlist.submissions(SHOW_ID) == {}

    def test_repoint_moves_the_submission_to_the_new_job(self, watchlist: WatchlistStore):
        watchlist.record_submission(SHOW_ID, 2, 5, JOB_ID)
        watchlist.repoint_submission(JOB_ID, NEW_JOB_ID)
        assert watchlist.submissions(SHOW_ID) == {(2, 5): (SubmissionState.SUBMITTED, NEW_JOB_ID)}
        watchlist.ignore_submission_for_job(JOB_ID)
        assert watchlist.submissions(SHOW_ID)[(2, 5)][0] is SubmissionState.SUBMITTED

    def test_clear_forgets_one_episode(self, watchlist: WatchlistStore):
        watchlist.record_submission(SHOW_ID, 2, 5, JOB_ID)
        watchlist.record_submission(SHOW_ID, 2, 6, NEW_JOB_ID)
        watchlist.clear_submission(SHOW_ID, 2, 5)
        assert set(watchlist.submissions(SHOW_ID)) == {(2, 6)}

    def test_submissions_survive_removing_the_title(self, watchlist: WatchlistStore):
        # The record is what stops a loop; it outlives the follow on purpose.
        watchlist.add(MediaType.SHOW, SHOW_ID, _request())
        watchlist.record_submission(SHOW_ID, 1, 1, JOB_ID)
        watchlist.remove(MediaType.SHOW, SHOW_ID)
        assert (1, 1) in watchlist.submissions(SHOW_ID)


class TestSchema:
    def test_fresh_db_has_both_tables_with_every_column(self, tmp_path: Path):
        db_path = str(tmp_path / "orchestrator.db")
        WatchlistStore(db_path=db_path)
        assert {WATCHLIST_TABLE, "follow_submission"} <= _tables(db_path)
        assert LEGACY_WATCHLIST_TABLE not in _tables(db_path)
        assert {"kind", "follow_mode", "follow_paused", "followed_at", "last_submitted"} <= (
            _columns(db_path, WATCHLIST_TABLE)
        )

    def test_legacy_wishlist_table_is_renamed_with_rows_intact(self, tmp_path: Path):
        db_path = str(tmp_path / "orchestrator.db")
        with sqlite3.connect(db_path) as conn:
            conn.executescript(_LEGACY_SCHEMA)
            conn.execute(
                f"INSERT INTO {LEGACY_WATCHLIST_TABLE} "
                "(tmdb_id, media_type, title, year, poster_path, overview, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    DUNE_ID,
                    "movie",
                    "Dune",
                    "2021",
                    "/dune.jpg",
                    "Sand.",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        watchlist = WatchlistStore(db_path=db_path)

        tables = _tables(db_path)
        assert WATCHLIST_TABLE in tables
        assert LEGACY_WATCHLIST_TABLE not in tables
        assert set(_columns(db_path, WATCHLIST_TABLE)) >= {"kind", "follow_mode", "last_submitted"}
        items = watchlist.list()
        assert [(i.tmdb_id, i.title, i.kind) for i in items] == [
            (DUNE_ID, "Dune", WatchlistKind.SAVED)
        ]
        assert items[0].follow is None
        # The migrated row behaves like a new one.
        watchlist.add(MediaType.SHOW, SHOW_ID, _request())
        assert watchlist.follow(SHOW_ID, _follow()).kind is WatchlistKind.FOLLOWING

    def test_migration_is_idempotent_across_restarts(self, tmp_path: Path):
        db_path = str(tmp_path / "orchestrator.db")
        first = WatchlistStore(db_path=db_path)
        first.add(MediaType.SHOW, SHOW_ID, _request())
        first.follow(SHOW_ID, _follow())
        second = WatchlistStore(db_path=db_path)
        item = second.get(MediaType.SHOW, SHOW_ID)
        assert item is not None
        assert item.kind is WatchlistKind.FOLLOWING

    def test_table_creation_on_an_existing_db_leaves_pipeline_job_intact(self, tmp_path: Path):
        db_path = str(tmp_path / "orchestrator.db")
        jobs = JobStore(db_path=db_path)
        job = jobs.create_job(release_name="r", media_type=MediaType.MOVIE, tmdb_id=DUNE_ID)

        watchlist = WatchlistStore(db_path=db_path)
        watchlist.add(MediaType.MOVIE, DUNE_ID, _request())
        WatchlistStore(db_path=db_path)

        assert jobs.get_job_by_id(job.id).tmdb_id == DUNE_ID
        assert len(watchlist.list()) == 1
        assert {"pipeline_job", WATCHLIST_TABLE, "follow_submission"} <= _tables(db_path)
