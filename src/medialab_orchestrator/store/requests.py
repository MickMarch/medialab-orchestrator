"""Jellyfin in-app requests: one row per requested title.

A request is filed when an allowed Jellyfin user favorites a Discover
placeholder. Jobs carry no request link; the title key ``(media_type,
tmdb_id)`` joins a request to its jobs, which is sound because a placeholder
only exists for a title not yet in the library. Same DB file and locking
pattern as ``WatchlistStore``.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from medialab_contracts import MediaType
from pydantic import BaseModel

_IN_MEMORY = ":memory:"
REQUEST_TABLE = "jellyfin_request"


class RequestStatus(str, Enum):
    REQUESTED = "requested"
    """Filed; nothing picked or followed yet."""
    SUBMITTED = "submitted"
    """A movie job was submitted, or the show is followed."""
    NOT_FOUND = "not_found"
    """The movie pick found nothing; picked again after the retry interval."""
    CLOSED = "closed"
    """The title reached the library; the placeholder is gone."""
    CANCELLED = "cancelled"
    """Every requester dropped the favorite before anything was submitted."""


OPEN_STATUSES = frozenset(
    {RequestStatus.REQUESTED, RequestStatus.SUBMITTED, RequestStatus.NOT_FOUND}
)


class JellyfinRequest(BaseModel):
    media_type: MediaType
    tmdb_id: int
    title: str
    year: int | None = None
    status: RequestStatus = RequestStatus.REQUESTED
    users: frozenset[str] = frozenset()
    last_tried_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {REQUEST_TABLE} (
    media_type    TEXT    NOT NULL,
    tmdb_id       INTEGER NOT NULL,
    title         TEXT    NOT NULL,
    year          INTEGER,
    status        TEXT    NOT NULL,
    users         TEXT    NOT NULL,
    last_tried_at TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    PRIMARY KEY (media_type, tmdb_id)
)
"""

# Filing again reopens a closed or cancelled row as a fresh request.
_FILE = f"""
INSERT INTO {REQUEST_TABLE}
    (media_type, tmdb_id, title, year, status, users, last_tried_at, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
ON CONFLICT (media_type, tmdb_id) DO UPDATE SET
    title = excluded.title, year = excluded.year, status = excluded.status,
    users = excluded.users, last_tried_at = NULL, created_at = excluded.created_at,
    updated_at = excluded.updated_at
"""

_SELECT_ONE = f"SELECT * FROM {REQUEST_TABLE} WHERE media_type = ? AND tmdb_id = ?"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _users_json(users: Iterable[str]) -> str:
    return json.dumps(sorted(users))


def _row_to_request(row: sqlite3.Row) -> JellyfinRequest:
    return JellyfinRequest(
        media_type=MediaType(row["media_type"]),
        tmdb_id=row["tmdb_id"],
        title=row["title"],
        year=row["year"],
        status=RequestStatus(row["status"]),
        users=frozenset(json.loads(row["users"])),
        last_tried_at=datetime.fromisoformat(row["last_tried_at"])
        if row["last_tried_at"]
        else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class RequestNotFoundError(KeyError):
    """No request row for the title."""


class RequestStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        if db_path != _IN_MEMORY:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._shared: sqlite3.Connection | None = self._connect() if db_path == _IN_MEMORY else None
        with self._cursor() as cur:
            cur.execute(_SCHEMA)

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

    def get(self, media_type: MediaType, tmdb_id: int) -> JellyfinRequest | None:
        with self._cursor() as cur:
            row = cur.execute(_SELECT_ONE, (media_type.value, tmdb_id)).fetchone()
        return _row_to_request(row) if row else None

    def list_open(self) -> builtins.list[JellyfinRequest]:
        """Open requests, oldest first, so the earliest asker is served first."""
        placeholders = ", ".join("?" for _ in OPEN_STATUSES)
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT * FROM {REQUEST_TABLE} WHERE status IN ({placeholders})"
                " ORDER BY created_at, rowid",
                [status.value for status in OPEN_STATUSES],
            ).fetchall()
        return [_row_to_request(row) for row in rows]

    def file(
        self,
        media_type: MediaType,
        tmdb_id: int,
        *,
        title: str,
        year: int | None,
        users: Iterable[str],
    ) -> JellyfinRequest:
        """A new ``requested`` row, or a closed or cancelled one reopened."""
        now = _now()
        with self._cursor() as cur:
            cur.execute(
                _FILE,
                (
                    media_type.value,
                    tmdb_id,
                    title,
                    year,
                    RequestStatus.REQUESTED.value,
                    _users_json(users),
                    now,
                    now,
                ),
            )
            row = cur.execute(_SELECT_ONE, (media_type.value, tmdb_id)).fetchone()
        return _row_to_request(row)

    def update(
        self,
        media_type: MediaType,
        tmdb_id: int,
        *,
        status: RequestStatus | None = None,
        users: Iterable[str] | None = None,
        last_tried_at: datetime | None = None,
    ) -> JellyfinRequest:
        fields: dict[str, object] = {"updated_at": _now()}
        if status is not None:
            fields["status"] = status.value
        if users is not None:
            fields["users"] = _users_json(users)
        if last_tried_at is not None:
            fields["last_tried_at"] = last_tried_at.astimezone(UTC).isoformat(timespec="seconds")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE {REQUEST_TABLE} SET {assignments} WHERE media_type = ? AND tmdb_id = ?",
                [*fields.values(), media_type.value, tmdb_id],
            )
            row = cur.execute(_SELECT_ONE, (media_type.value, tmdb_id)).fetchone()
        if row is None:
            raise RequestNotFoundError((media_type, tmdb_id))
        return _row_to_request(row)
