"""SQLite-backed watchlist store: one shared list of saved titles and followed
shows, plus the record of what each follow has already submitted.

Lives in the same database file as ``pipeline_job``. Title, year, poster and
overview are stored at add time so listing never calls TMDB. A followed show
is a saved row with ``kind = following`` and the follow columns set; a
``follow_submission`` row per (show, season, episode) keeps a follow from
re-queueing an episode whose job was later deleted, failed or replaced.
"""

from __future__ import annotations

import builtins
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from medialab_contracts import (
    FollowRequest,
    FollowStart,
    FollowStartMode,
    FollowState,
    MediaType,
    SubmissionState,
    WatchlistAddRequest,
    WatchlistItem,
    WatchlistKind,
)

_IN_MEMORY = ":memory:"

WATCHLIST_TABLE = "watchlist_item"
LEGACY_WATCHLIST_TABLE = "wishlist_item"
SUBMISSION_TABLE = "follow_submission"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {WATCHLIST_TABLE} (
    tmdb_id           INTEGER NOT NULL,
    media_type        TEXT    NOT NULL,
    title             TEXT    NOT NULL,
    year              TEXT,
    poster_path       TEXT,
    overview          TEXT    NOT NULL DEFAULT '',
    added_at          TEXT    NOT NULL,
    kind              TEXT    NOT NULL DEFAULT '{WatchlistKind.SAVED.value}',
    follow_mode       TEXT,
    follow_season     INTEGER,
    follow_episode    INTEGER,
    follow_resolution TEXT,
    follow_paused     INTEGER NOT NULL DEFAULT 0,
    followed_at       TEXT,
    last_checked_at   TEXT,
    last_submitted    TEXT,
    PRIMARY KEY (media_type, tmdb_id)
);
CREATE TABLE IF NOT EXISTS {SUBMISSION_TABLE} (
    tmdb_id      INTEGER NOT NULL,
    season       INTEGER NOT NULL,
    episode      INTEGER NOT NULL,
    job_id       TEXT,
    state        TEXT    NOT NULL,
    submitted_at TEXT    NOT NULL,
    PRIMARY KEY (tmdb_id, season, episode)
);
"""

# Columns added to the pre-watchlist table. Applied with ALTER TABLE at
# startup when an existing database lacks them (SQLite, no migration tool).
_ADDED_COLUMNS: dict[str, str] = {
    "kind": f"TEXT NOT NULL DEFAULT '{WatchlistKind.SAVED.value}'",
    "follow_mode": "TEXT",
    "follow_season": "INTEGER",
    "follow_episode": "INTEGER",
    "follow_resolution": "TEXT",
    "follow_paused": "INTEGER NOT NULL DEFAULT 0",
    "followed_at": "TEXT",
    "last_checked_at": "TEXT",
    "last_submitted": "TEXT",
}

# Re-adding refreshes the stored metadata but keeps the original added_at and
# the follow state, so a repeat add from a second UI neither reorders the list
# nor drops a follow.
_UPSERT = f"""
INSERT INTO {WATCHLIST_TABLE}
    (tmdb_id, media_type, title, year, poster_path, overview, added_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (media_type, tmdb_id) DO UPDATE SET
    title = excluded.title,
    year = excluded.year,
    poster_path = excluded.poster_path,
    overview = excluded.overview
"""

_SELECT_ITEM = f"SELECT * FROM {WATCHLIST_TABLE} WHERE media_type = ? AND tmdb_id = ?"

_FOLLOW = f"""
UPDATE {WATCHLIST_TABLE} SET
    kind = ?, follow_mode = ?, follow_season = ?, follow_episode = ?,
    follow_resolution = ?, follow_paused = 0, followed_at = ?
WHERE media_type = ? AND tmdb_id = ?
"""

_UNFOLLOW = f"""
UPDATE {WATCHLIST_TABLE} SET
    kind = ?, follow_mode = NULL, follow_season = NULL, follow_episode = NULL,
    follow_resolution = NULL, follow_paused = 0, followed_at = NULL,
    last_checked_at = NULL, last_submitted = NULL
WHERE media_type = ? AND tmdb_id = ?
"""

_RECORD_SUBMISSION = f"""
INSERT INTO {SUBMISSION_TABLE} (tmdb_id, season, episode, job_id, state, submitted_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (tmdb_id, season, episode) DO UPDATE SET
    job_id = excluded.job_id,
    state = excluded.state,
    submitted_at = excluded.submitted_at
"""

# rowid breaks ties between adds within the same second.
_NEWEST_FIRST = " ORDER BY added_at DESC, rowid DESC"

EpisodeKeyTuple = tuple[int, int]
SubmissionRecord = tuple[SubmissionState, str | None]


class WatchlistItemNotFoundError(Exception):
    """Raised when a follow operation targets a title that is not on the list."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


class WatchlistStore:
    """Synchronous SQLite watchlist store, same connection and locking pattern
    as ``JobStore``: per-operation connections for a file, one shared
    connection for ``:memory:``."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        if db_path != _IN_MEMORY:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._shared: sqlite3.Connection | None = self._connect() if db_path == _IN_MEMORY else None
        with self._cursor() as cur:
            _migrate(cur)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        conn = self._shared or self._connect()
        self._lock.acquire()
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
        finally:
            self._lock.release()
            if self._shared is None:
                conn.close()

    # Saved titles

    def add(
        self, media_type: MediaType, tmdb_id: int, request: WatchlistAddRequest
    ) -> WatchlistItem:
        """Idempotent upsert; returns the stored row. A repeat keeps ``added_at``
        and any follow state."""
        with self._cursor() as cur:
            cur.execute(
                _UPSERT,
                (
                    tmdb_id,
                    media_type.value,
                    request.title,
                    request.year,
                    request.poster_path,
                    request.overview,
                    _now(),
                ),
            )
            row = cur.execute(_SELECT_ITEM, (media_type.value, tmdb_id)).fetchone()
        return _row_to_item(row)

    def get(self, media_type: MediaType, tmdb_id: int) -> WatchlistItem | None:
        with self._cursor() as cur:
            row = cur.execute(_SELECT_ITEM, (media_type.value, tmdb_id)).fetchone()
        return _row_to_item(row) if row is not None else None

    def remove(self, media_type: MediaType, tmdb_id: int) -> None:
        """Delete the row whatever its kind; absent is a no-op."""
        with self._cursor() as cur:
            cur.execute(
                f"DELETE FROM {WATCHLIST_TABLE} WHERE media_type = ? AND tmdb_id = ?",
                (media_type.value, tmdb_id),
            )

    def remove_saved(self, media_type: MediaType, tmdb_id: int) -> None:
        """Delete the row only when it is a plain saved title; a follow stays."""
        with self._cursor() as cur:
            cur.execute(
                f"DELETE FROM {WATCHLIST_TABLE} WHERE media_type = ? AND tmdb_id = ? AND kind = ?",
                (media_type.value, tmdb_id, WatchlistKind.SAVED.value),
            )

    def list(
        self, media_type: MediaType | None = None, kind: WatchlistKind | None = None
    ) -> builtins.list[WatchlistItem]:
        """All items, newest first, optionally filtered by media type and kind."""
        clauses: builtins.list[str] = []
        params: builtins.list[str] = []
        if media_type is not None:
            clauses.append("media_type = ?")
            params.append(media_type.value)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        query = f"SELECT * FROM {WATCHLIST_TABLE}"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._cursor() as cur:
            rows = cur.execute(query + _NEWEST_FIRST, tuple(params)).fetchall()
        return [_row_to_item(row) for row in rows]

    def keys(self, media_type: MediaType) -> dict[int, WatchlistKind]:
        """The TMDB ids on the list for one media type, each with its kind."""
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT tmdb_id, kind FROM {WATCHLIST_TABLE} WHERE media_type = ?",
                (media_type.value,),
            ).fetchall()
        return {row["tmdb_id"]: WatchlistKind(row["kind"]) for row in rows}

    # Follows (shows only)

    def follow(self, tmdb_id: int, request: FollowRequest) -> WatchlistItem:
        """Turn the saved show into a follow: stores the start and resolution,
        stamps ``followed_at`` now and unpauses. Repeatable; the row must exist."""
        with self._cursor() as cur:
            cur.execute(
                _FOLLOW,
                (
                    WatchlistKind.FOLLOWING.value,
                    request.start.mode.value,
                    request.start.season,
                    request.start.episode,
                    request.resolution,
                    _now(),
                    MediaType.SHOW.value,
                    tmdb_id,
                ),
            )
            row = _require_row(cur, tmdb_id)
        return _row_to_item(row)

    def unfollow(self, tmdb_id: int) -> None:
        """Back to a saved row with every follow column cleared; absent is a no-op."""
        with self._cursor() as cur:
            cur.execute(_UNFOLLOW, (WatchlistKind.SAVED.value, MediaType.SHOW.value, tmdb_id))

    def set_paused(self, tmdb_id: int, paused: bool) -> WatchlistItem:
        """Pause or resume a follow; the row must be a follow."""
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE {WATCHLIST_TABLE} SET follow_paused = ? "
                "WHERE media_type = ? AND tmdb_id = ? AND kind = ?",
                (int(paused), MediaType.SHOW.value, tmdb_id, WatchlistKind.FOLLOWING.value),
            )
            row = _require_row(cur, tmdb_id, kind=WatchlistKind.FOLLOWING)
        return _row_to_item(row)

    def mark_checked(self, tmdb_id: int, when: datetime, last_submitted: str | None = None) -> None:
        """Stamp the follow's last check, and its last submitted episode when given."""
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE {WATCHLIST_TABLE} SET last_checked_at = ? "
                "WHERE media_type = ? AND tmdb_id = ?",
                (_timestamp(when), MediaType.SHOW.value, tmdb_id),
            )
            if last_submitted is not None:
                cur.execute(
                    f"UPDATE {WATCHLIST_TABLE} SET last_submitted = ? "
                    "WHERE media_type = ? AND tmdb_id = ?",
                    (last_submitted, MediaType.SHOW.value, tmdb_id),
                )

    # Submissions

    def submissions(self, tmdb_id: int) -> dict[EpisodeKeyTuple, SubmissionRecord]:
        """What the follow has submitted, by (season, episode): state and job id."""
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT season, episode, state, job_id FROM {SUBMISSION_TABLE} WHERE tmdb_id = ?",
                (tmdb_id,),
            ).fetchall()
        return {
            (row["season"], row["episode"]): (SubmissionState(row["state"]), row["job_id"])
            for row in rows
        }

    def record_submission(self, tmdb_id: int, season: int, episode: int, job_id: str) -> None:
        """Mark the episode submitted by ``job_id`` now; a repeat overwrites."""
        with self._cursor() as cur:
            cur.execute(
                _RECORD_SUBMISSION,
                (tmdb_id, season, episode, job_id, SubmissionState.SUBMITTED.value, _now()),
            )

    def ignore_submission_for_job(self, job_id: str) -> None:
        """A job deleted through medialab keeps its episode off the wanted list."""
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE {SUBMISSION_TABLE} SET state = ? WHERE job_id = ?",
                (SubmissionState.IGNORED.value, job_id),
            )

    def repoint_submission(self, old_job_id: str, new_job_id: str) -> None:
        """A redo moves the submission to the replacement job."""
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE {SUBMISSION_TABLE} SET job_id = ? WHERE job_id = ?",
                (new_job_id, old_job_id),
            )

    def clear_submission(self, tmdb_id: int, season: int, episode: int) -> None:
        """Forget the submission so the next follow check may fetch the episode again."""
        with self._cursor() as cur:
            cur.execute(
                f"DELETE FROM {SUBMISSION_TABLE} WHERE tmdb_id = ? AND season = ? AND episode = ?",
                (tmdb_id, season, episode),
            )


def _migrate(cur: sqlite3.Cursor) -> None:
    """Rename the pre-watchlist table when only it exists, create what is
    missing, then add the follow columns an older table lacks."""
    tables = {
        row["name"] for row in cur.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if LEGACY_WATCHLIST_TABLE in tables and WATCHLIST_TABLE not in tables:
        cur.execute(f"ALTER TABLE {LEGACY_WATCHLIST_TABLE} RENAME TO {WATCHLIST_TABLE}")
    cur.executescript(_SCHEMA)
    present = {row["name"] for row in cur.execute(f"PRAGMA table_info({WATCHLIST_TABLE})")}
    for column, definition in _ADDED_COLUMNS.items():
        if column not in present:
            cur.execute(f"ALTER TABLE {WATCHLIST_TABLE} ADD COLUMN {column} {definition}")


def _require_row(
    cur: sqlite3.Cursor, tmdb_id: int, *, kind: WatchlistKind | None = None
) -> sqlite3.Row:
    row = cur.execute(_SELECT_ITEM, (MediaType.SHOW.value, tmdb_id)).fetchone()
    if row is None or (kind is not None and row["kind"] != kind.value):
        what = "followed" if kind is WatchlistKind.FOLLOWING else "on the watchlist"
        raise WatchlistItemNotFoundError(f"Show {tmdb_id} is not {what}.")
    return row


def _optional_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_follow(row: sqlite3.Row) -> FollowState | None:
    if row["kind"] != WatchlistKind.FOLLOWING.value:
        return None
    return FollowState(
        start=FollowStart(
            mode=FollowStartMode(row["follow_mode"]),
            season=row["follow_season"],
            episode=row["follow_episode"],
        ),
        resolution=row["follow_resolution"],
        paused=bool(row["follow_paused"]),
        followed_at=datetime.fromisoformat(row["followed_at"]),
        last_checked_at=_optional_timestamp(row["last_checked_at"]),
        last_submitted=row["last_submitted"],
    )


def _row_to_item(row: sqlite3.Row) -> WatchlistItem:
    return WatchlistItem(
        tmdb_id=row["tmdb_id"],
        media_type=MediaType(row["media_type"]),
        kind=WatchlistKind(row["kind"]),
        title=row["title"],
        year=row["year"],
        poster_path=row["poster_path"],
        overview=row["overview"],
        added_at=datetime.fromisoformat(row["added_at"]),
        follow=_row_to_follow(row),
    )
