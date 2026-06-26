"""SQLite-backed job store for the pipeline lifecycle.

The ``pipeline_job`` table is the orchestrator's spine: one row per torrent,
advanced one state at a time and persisted after each transition so a restart
resumes from the last committed state.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from medialab_contracts import MediaType
from pydantic import BaseModel


class JobStatus(str, Enum):
    """The lifecycle states a pipeline job advances through.

    Forward-only happy path: ``DOWNLOAD_SUBMITTED`` -> ... -> ``DONE``.
    ``FAILED`` is terminal-but-retryable (retry re-enters from the last good
    state). Wire values are the enum names so a row reads as its status.
    """

    DOWNLOAD_SUBMITTED = "DOWNLOAD_SUBMITTED"
    DOWNLOADING = "DOWNLOADING"
    STOP_SEEDING = "STOP_SEEDING"
    RESOLVE_META = "RESOLVE_META"
    RENAME = "RENAME"
    REGISTER = "REGISTER"
    SCAN = "SCAN"
    DONE = "DONE"
    FAILED = "FAILED"


class PipelineJob(BaseModel):
    """A single job row. Mirrors the ``pipeline_job`` table columns."""

    id: int
    torrent_hash: str
    release_name: str
    media_type: MediaType
    tmdb_id: int
    resolved_title: str | None = None
    resolved_year: int | None = None
    source_path: str | None = None
    dest_path: str | None = None
    status: JobStatus
    last_error: str | None = None
    attempts: int = 0
    created_at: str
    updated_at: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_job (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    torrent_hash   TEXT    NOT NULL UNIQUE,
    release_name   TEXT    NOT NULL,
    media_type     TEXT    NOT NULL,
    tmdb_id        INTEGER NOT NULL,
    resolved_title TEXT,
    resolved_year  INTEGER,
    source_path    TEXT,
    dest_path      TEXT,
    status         TEXT    NOT NULL,
    last_error     TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pipeline_job_status ON pipeline_job (status);
"""

# Columns a caller may update via update_job; id/torrent_hash/created_at are
# immutable after insert, so they are excluded to keep updates safe.
_UPDATABLE_COLUMNS = frozenset(
    {
        "release_name",
        "media_type",
        "tmdb_id",
        "resolved_title",
        "resolved_year",
        "source_path",
        "dest_path",
        "status",
        "last_error",
        "attempts",
    }
)


def _now() -> str:
    """UTC timestamp in ISO 8601, second precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _normalise_hash(torrent_hash: str) -> str:
    """qBittorrent hashes are case-insensitive; store and look up lowercase."""
    return torrent_hash.lower()


class JobNotFoundError(Exception):
    """Raised when a lookup or update targets a hash with no job row."""


class JobStore:
    """Synchronous SQLite job store.

    A thin wrapper over ``sqlite3``: the job volume is a handful per day on a
    single host, so a connection-per-operation store is the lightest thing that
    fits. The async worker calls these from a thread executor.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # A shared in-memory DB would vanish between connections, so :memory:
        # keeps one connection open for the store's lifetime; file-backed DBs
        # open per operation.
        self._shared: sqlite3.Connection | None = self._connect() if db_path == ":memory:" else None
        with self._cursor() as cur:
            cur.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # A file-backed DB opens per operation, so it stays on the calling
        # thread. The single shared :memory: connection, by contrast, is reused
        # across threads (FastAPI runs sync handlers in a threadpool, the worker
        # in to_thread), so it must allow cross-thread use; _lock serialises it.
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
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

    def create_job(
        self,
        *,
        torrent_hash: str,
        release_name: str,
        media_type: MediaType,
        tmdb_id: int,
        status: JobStatus = JobStatus.DOWNLOAD_SUBMITTED,
    ) -> PipelineJob:
        """Insert a new job at ``status`` (default ``DOWNLOAD_SUBMITTED``)."""
        now = _now()
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO pipeline_job
                    (torrent_hash, release_name, media_type, tmdb_id,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _normalise_hash(torrent_hash),
                    release_name,
                    media_type.value,
                    tmdb_id,
                    status.value,
                    now,
                    now,
                ),
            )
            job_id = cur.lastrowid
        if job_id is None:  # pragma: no cover - INSERT always sets lastrowid
            raise RuntimeError("INSERT did not return a row id")
        return self.get_job_by_id(job_id)

    def get_job_by_id(self, job_id: int) -> PipelineJob:
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM pipeline_job WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(f"No job with id {job_id}")
        return _row_to_job(row)

    def get_job_by_hash(self, torrent_hash: str) -> PipelineJob:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT * FROM pipeline_job WHERE torrent_hash = ?",
                (_normalise_hash(torrent_hash),),
            ).fetchone()
        if row is None:
            raise JobNotFoundError(f"No job with hash {torrent_hash}")
        return _row_to_job(row)

    def list_jobs(self, *, status: JobStatus | None = None) -> list[PipelineJob]:
        """All jobs, newest first, optionally filtered by status."""
        query = "SELECT * FROM pipeline_job"
        params: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status.value,)
        query += " ORDER BY id DESC"
        with self._cursor() as cur:
            rows = cur.execute(query, params).fetchall()
        return [_row_to_job(row) for row in rows]

    def update_job(self, torrent_hash: str, **fields: object) -> PipelineJob:
        """Patch the named columns on a job, bumping ``updated_at``.

        Enum values are accepted and unwrapped. Unknown or immutable columns
        raise ``ValueError`` so a typo cannot silently no-op.
        """
        unknown = set(fields) - _UPDATABLE_COLUMNS
        if unknown:
            raise ValueError(f"Cannot update columns: {sorted(unknown)}")
        if not fields:
            return self.get_job_by_hash(torrent_hash)

        normalised = {k: _unwrap(v) for k, v in fields.items()}
        assignments = ", ".join(f"{col} = ?" for col in normalised)
        params = [*normalised.values(), _now(), _normalise_hash(torrent_hash)]
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE pipeline_job SET {assignments}, updated_at = ? WHERE torrent_hash = ?",
                params,
            )
            if cur.rowcount == 0:
                raise JobNotFoundError(f"No job with hash {torrent_hash}")
        return self.get_job_by_hash(torrent_hash)


def _unwrap(value: object) -> object:
    """Enum -> its value, everything else unchanged."""
    return value.value if isinstance(value, Enum) else value


def _row_to_job(row: sqlite3.Row) -> PipelineJob:
    return PipelineJob(
        id=row["id"],
        torrent_hash=row["torrent_hash"],
        release_name=row["release_name"],
        media_type=MediaType(row["media_type"]),
        tmdb_id=row["tmdb_id"],
        resolved_title=row["resolved_title"],
        resolved_year=row["resolved_year"],
        source_path=row["source_path"],
        dest_path=row["dest_path"],
        status=JobStatus(row["status"]),
        last_error=row["last_error"],
        attempts=row["attempts"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
