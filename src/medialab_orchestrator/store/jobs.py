"""SQLite-backed job store for the pipeline lifecycle.

The ``pipeline_job`` table is the orchestrator's spine: one row per torrent,
advanced one state at a time and persisted after each transition so a restart
resumes from the last committed state.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator
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
    state). ``NEEDS_ATTENTION`` is where the health poll parks a job once its
    automatic budget is spent; a human retry, redo or dismiss moves it on.
    ``DISMISSED`` is terminal: a human judged the job not worth pursuing; the
    row and its error stay for the record. Wire values are the enum names so
    a row reads as its status.
    """

    DOWNLOAD_SUBMITTED = "DOWNLOAD_SUBMITTED"
    DOWNLOADING = "DOWNLOADING"
    STOP_SEEDING = "STOP_SEEDING"
    RESOLVE_META = "RESOLVE_META"
    RENAME = "RENAME"
    SCAN = "SCAN"
    DONE = "DONE"
    FAILED = "FAILED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    DELETED = "DELETED"
    DISMISSED = "DISMISSED"


class PipelineJob(BaseModel):
    """A single job row. Mirrors the ``pipeline_job`` table columns.

    ``id`` is a surrogate uuid assigned at creation; ``torrent_hash`` is the
    real BTIH info-hash, which is not known up front for a ``.torrent``-URL
    download and so is nullable until stamped (by the download readback or the
    completion webhook).
    """

    id: str
    torrent_hash: str | None = None
    release_name: str
    media_type: MediaType
    tmdb_id: int
    season: int | None = None
    """Search scope: None with ``episode`` None means the whole title."""
    episode: int | None = None
    resolved_title: str | None = None
    resolved_year: int | None = None
    source_path: str | None = None
    dest_path: str | None = None
    status: JobStatus
    last_error: str | None = None
    attempts: int = 0
    remediations: int = 0
    seeding_removed_at: str | None = None
    placed_paths: list[str] = []
    deleted_at: str | None = None
    deleted_hash: str | None = None
    """The info-hash a DELETED job used to own, kept for the record. The live
    ``torrent_hash`` column is released on deletion so the same torrent can be
    downloaded again under a new job."""
    redo_of: str | None = None
    """The id of the job this one replaces, set by ``POST /jobs/{id}/redo``."""
    dismissed_at: str | None = None
    """When a human dismissed the job; set with ``DISMISSED``."""
    created_at: str
    updated_at: str


# The row order is preserved by a monotonic rowid so "newest first" listing is
# stable even though the primary key is now an unordered uuid.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_job (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    id             TEXT    NOT NULL UNIQUE,
    torrent_hash   TEXT    UNIQUE,
    release_name   TEXT    NOT NULL,
    media_type     TEXT    NOT NULL,
    tmdb_id        INTEGER NOT NULL,
    season         INTEGER,
    episode        INTEGER,
    resolved_title TEXT,
    resolved_year  INTEGER,
    source_path    TEXT,
    dest_path      TEXT,
    status         TEXT    NOT NULL,
    last_error     TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    remediations   INTEGER NOT NULL DEFAULT 0,
    seeding_removed_at TEXT,
    placed_paths   TEXT,
    deleted_at     TEXT,
    deleted_hash   TEXT,
    redo_of        TEXT,
    dismissed_at   TEXT,
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
        "remediations",
        "seeding_removed_at",
        "placed_paths",
        "deleted_at",
        "dismissed_at",
    }
)

# Columns added after the first release. Applied with ALTER TABLE at startup
# when an existing database lacks them (SQLite, no migration tool).
_ADDED_COLUMNS: dict[str, str] = {
    "remediations": "INTEGER NOT NULL DEFAULT 0",
    "seeding_removed_at": "TEXT",
    "placed_paths": "TEXT",
    "deleted_at": "TEXT",
    "season": "INTEGER",
    "episode": "INTEGER",
    "redo_of": "TEXT",
    "deleted_hash": "TEXT",
    "dismissed_at": "TEXT",
}

# A DELETED job must not hold the unique torrent_hash: the same torrent may be
# downloaded again. Appended to the UPDATE that marks a job DELETED, and run at
# startup for rows deleted before deleted_hash existed.
_RELEASE_HASH_ASSIGNMENTS = (
    ", deleted_hash = COALESCE(deleted_hash, torrent_hash), torrent_hash = NULL"
)
_RELEASE_DELETED_HASHES = """
UPDATE pipeline_job
SET deleted_hash = COALESCE(deleted_hash, torrent_hash), torrent_hash = NULL
WHERE status = 'DELETED' AND torrent_hash IS NOT NULL
"""


def _now() -> str:
    """UTC timestamp in ISO 8601, second precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _normalise_hash(torrent_hash: str) -> str:
    """qBittorrent hashes are case-insensitive; store and look up lowercase."""
    return torrent_hash.lower()


class JobNotFoundError(Exception):
    """Raised when a lookup or update targets an id/hash with no job row."""


class HashInUseError(Exception):
    """Raised when stamping a hash another live (non-DELETED) job already owns."""

    def __init__(self, torrent_hash: str, job_id: str) -> None:
        super().__init__(f"Torrent {torrent_hash} already belongs to job {job_id}")
        self.torrent_hash = torrent_hash
        self.job_id = job_id


def _new_id() -> str:
    """A fresh surrogate job id (uuid4 hex, url-safe and compact)."""
    return uuid.uuid4().hex


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
            present = {row["name"] for row in cur.execute("PRAGMA table_info(pipeline_job)")}
            for column, definition in _ADDED_COLUMNS.items():
                if column not in present:
                    cur.execute(f"ALTER TABLE pipeline_job ADD COLUMN {column} {definition}")
            cur.execute(_RELEASE_DELETED_HASHES)

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
        release_name: str,
        media_type: MediaType,
        tmdb_id: int,
        torrent_hash: str | None = None,
        status: JobStatus = JobStatus.DOWNLOAD_SUBMITTED,
        season: int | None = None,
        episode: int | None = None,
        redo_of: str | None = None,
    ) -> PipelineJob:
        """Insert a new job at ``status`` (default ``DOWNLOAD_SUBMITTED``).

        A surrogate ``id`` is assigned here. ``torrent_hash`` is optional: it is
        omitted for a ``.torrent``-URL download whose hash is not yet known and
        stamped later via ``stamp_hash``. ``season`` and ``episode`` record the
        search scope; both None means the whole title. ``redo_of`` links a
        replacement to the job it redoes.
        """
        now = _now()
        job_id = _new_id()
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO pipeline_job
                    (id, torrent_hash, release_name, media_type, tmdb_id, season, episode,
                     redo_of, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    _normalise_hash(torrent_hash) if torrent_hash is not None else None,
                    release_name,
                    media_type.value,
                    tmdb_id,
                    season,
                    episode,
                    redo_of,
                    status.value,
                    now,
                    now,
                ),
            )
        return self.get_job_by_id(job_id)

    def stamp_hash(self, job_id: str, torrent_hash: str) -> PipelineJob:
        """Backfill the real info-hash onto a job once qBittorrent knows it.

        Raises ``HashInUseError`` when a live job already owns the hash; a
        DELETED job never does, since deletion releases it.
        """
        normalised = _normalise_hash(torrent_hash)
        with self._cursor() as cur:
            owner = cur.execute(
                "SELECT id FROM pipeline_job WHERE torrent_hash = ? AND id != ?",
                (normalised, job_id),
            ).fetchone()
            if owner is not None:
                raise HashInUseError(normalised, owner["id"])
            cur.execute(
                "UPDATE pipeline_job SET torrent_hash = ?, updated_at = ? WHERE id = ?",
                (normalised, _now(), job_id),
            )
            if cur.rowcount == 0:
                raise JobNotFoundError(f"No job with id {job_id}")
        return self.get_job_by_id(job_id)

    def get_job_by_id(self, job_id: str) -> PipelineJob:
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
        query += " ORDER BY seq DESC"
        with self._cursor() as cur:
            rows = cur.execute(query, params).fetchall()
        return [_row_to_job(row) for row in rows]

    def list_jobs_for_title(self, media_type: MediaType, tmdb_id: int) -> list[PipelineJob]:
        """Every job for one title, newest first, whatever its status."""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM pipeline_job WHERE media_type = ? AND tmdb_id = ? ORDER BY seq DESC",
                (media_type.value, tmdb_id),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def redone_by(self, job_ids: Iterable[str]) -> dict[str, str]:
        """Map each given job id to the id of the newest job that redoes it.

        One query for the whole listing; ids with no replacement are absent.
        """
        ids = list(job_ids)
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT redo_of, id FROM pipeline_job WHERE redo_of IN ({placeholders})"
                " ORDER BY seq ASC",
                ids,
            ).fetchall()
        # Ascending seq so the last write per key is the newest replacement.
        return {row["redo_of"]: row["id"] for row in rows}

    def find_replacement(self, redo_of: str) -> PipelineJob | None:
        """The newest replacement for ``redo_of`` whose download was never
        submitted (still ``DOWNLOAD_SUBMITTED`` with no hash), else None."""
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT * FROM pipeline_job WHERE redo_of = ? AND torrent_hash IS NULL"
                " AND status = ? ORDER BY seq DESC LIMIT 1",
                (redo_of, JobStatus.DOWNLOAD_SUBMITTED.value),
            ).fetchone()
        return _row_to_job(row) if row is not None else None

    def update_job(self, job_id: str, **fields: object) -> PipelineJob:
        """Patch the named columns on a job (keyed by id), bumping ``updated_at``.

        Enum values are accepted and unwrapped. Unknown or immutable columns
        raise ``ValueError`` so a typo cannot silently no-op. The hash is stamped
        via ``stamp_hash``, not here.
        """
        unknown = set(fields) - _UPDATABLE_COLUMNS
        if unknown:
            raise ValueError(f"Cannot update columns: {sorted(unknown)}")
        if not fields:
            return self.get_job_by_id(job_id)

        normalised = {k: _unwrap(v) for k, v in fields.items()}
        if isinstance(normalised.get("placed_paths"), list):
            normalised["placed_paths"] = json.dumps(normalised["placed_paths"])
        assignments = ", ".join(f"{col} = ?" for col in normalised)
        if normalised.get("status") == JobStatus.DELETED.value:
            # Deletion releases the unique hash so the torrent can come back
            # under a new job; the old value stays on the row for the record.
            assignments += _RELEASE_HASH_ASSIGNMENTS
        params = [*normalised.values(), _now(), job_id]
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE pipeline_job SET {assignments}, updated_at = ? WHERE id = ?",
                params,
            )
            if cur.rowcount == 0:
                raise JobNotFoundError(f"No job with id {job_id}")
        return self.get_job_by_id(job_id)


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
        season=row["season"],
        episode=row["episode"],
        resolved_title=row["resolved_title"],
        resolved_year=row["resolved_year"],
        source_path=row["source_path"],
        dest_path=row["dest_path"],
        status=JobStatus(row["status"]),
        last_error=row["last_error"],
        attempts=row["attempts"],
        remediations=row["remediations"],
        seeding_removed_at=row["seeding_removed_at"],
        placed_paths=json.loads(row["placed_paths"]) if row["placed_paths"] else [],
        deleted_at=row["deleted_at"],
        deleted_hash=row["deleted_hash"],
        redo_of=row["redo_of"],
        dismissed_at=row["dismissed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
