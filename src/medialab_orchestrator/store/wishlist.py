"""SQLite-backed wishlist store: titles saved for later, one shared list.

Lives in the same database file as ``pipeline_job``. Title, year, poster and
overview are stored at add time so listing never calls TMDB.
"""

from __future__ import annotations

import builtins
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from medialab_contracts import MediaType, WishlistAddRequest, WishlistItem

_IN_MEMORY = ":memory:"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wishlist_item (
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

# Re-adding refreshes the stored metadata but keeps the original added_at, so
# a repeat add from a second UI does not reorder the list.
_UPSERT = """
INSERT INTO wishlist_item
    (tmdb_id, media_type, title, year, poster_path, overview, added_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (media_type, tmdb_id) DO UPDATE SET
    title = excluded.title,
    year = excluded.year,
    poster_path = excluded.poster_path,
    overview = excluded.overview
"""

# rowid breaks ties between adds within the same second.
_NEWEST_FIRST = " ORDER BY added_at DESC, rowid DESC"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class WishlistStore:
    """Synchronous SQLite wishlist store, same connection and locking pattern as
    ``JobStore``: per-operation connections for a file, one shared connection
    for ``:memory:``."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        if db_path != _IN_MEMORY:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._shared: sqlite3.Connection | None = self._connect() if db_path == _IN_MEMORY else None
        with self._cursor() as cur:
            cur.executescript(_SCHEMA)

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

    def add(self, media_type: MediaType, tmdb_id: int, request: WishlistAddRequest) -> WishlistItem:
        """Idempotent upsert; returns the stored row."""
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
            row = cur.execute(
                "SELECT * FROM wishlist_item WHERE media_type = ? AND tmdb_id = ?",
                (media_type.value, tmdb_id),
            ).fetchone()
        return _row_to_item(row)

    def remove(self, media_type: MediaType, tmdb_id: int) -> None:
        """Delete the row if present; absent is a no-op."""
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM wishlist_item WHERE media_type = ? AND tmdb_id = ?",
                (media_type.value, tmdb_id),
            )

    def list(self, media_type: MediaType | None = None) -> builtins.list[WishlistItem]:
        """All items, newest first, optionally filtered by media type."""
        query = "SELECT * FROM wishlist_item"
        params: tuple[str, ...] = ()
        if media_type is not None:
            query += " WHERE media_type = ?"
            params = (media_type.value,)
        with self._cursor() as cur:
            rows = cur.execute(query + _NEWEST_FIRST, params).fetchall()
        return [_row_to_item(row) for row in rows]

    def keys(self, media_type: MediaType) -> set[int]:
        """The TMDB ids wishlisted for one media type."""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT tmdb_id FROM wishlist_item WHERE media_type = ?", (media_type.value,)
            ).fetchall()
        return {row["tmdb_id"] for row in rows}


def _row_to_item(row: sqlite3.Row) -> WishlistItem:
    return WishlistItem(
        tmdb_id=row["tmdb_id"],
        media_type=MediaType(row["media_type"]),
        title=row["title"],
        year=row["year"],
        poster_path=row["poster_path"],
        overview=row["overview"],
        added_at=datetime.fromisoformat(row["added_at"]),
    )
