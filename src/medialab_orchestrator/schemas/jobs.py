"""Request/response schemas for the gateway's stateful surface."""

from __future__ import annotations

from medialab_contracts import JobProgress, MediaType
from pydantic import BaseModel, Field

from medialab_orchestrator.services.attention import AttentionCause, attention_cause
from medialab_orchestrator.store import JobStatus, PipelineJob


class DownloadRequest(BaseModel):
    """Body for ``POST /download``. Mirrors a bot download confirmation.

    ``source_url`` is a magnet URI or an http ``.torrent`` file URL - whatever
    the picked search result carried.
    """

    source_url: str = Field(min_length=1)
    media_type: MediaType
    tmdb_id: int
    # The picked torrent's name, so the job is identifiable while it downloads;
    # completion overwrites it with the on-disk name.
    release_name: str = ""
    # The scope the torrent was searched with: both None for a whole series or
    # a movie, season alone for a season pack, both for a single episode.
    season: int | None = None
    episode: int | None = None


class JobView(BaseModel):
    """A pipeline job as exposed to the bot. Wraps the store's PipelineJob."""

    id: str
    torrent_hash: str | None = None
    release_name: str
    media_type: MediaType
    tmdb_id: int
    season: int | None = None
    episode: int | None = None
    resolved_title: str | None
    resolved_year: int | None
    source_path: str | None
    dest_path: str | None
    status: JobStatus
    last_error: str | None
    attempts: int
    remediations: int = 0
    seeding_removed_at: str | None = None
    placed_paths: list[str] = []
    deleted_at: str | None = None
    deleted_hash: str | None = None
    """The hash a DELETED job owned; ``torrent_hash`` is released on deletion."""
    redo_of: str | None = None
    """The job this one replaces, when submitted through ``POST /jobs/{id}/redo``."""
    redone_by: str | None = None
    """The newest job that replaces this one; computed on read, never stored."""
    dismissed_at: str | None = None
    attention_cause: AttentionCause | None = None
    """Why a FAILED or NEEDS_ATTENTION job waits on a human; derived on read
    from ``last_error`` so clients can offer the action that resolves it."""
    created_at: str
    updated_at: str
    progress: JobProgress | None = None
    """Live qBittorrent progress, attached on read to active downloads only."""

    @classmethod
    def from_job(cls, job: PipelineJob) -> JobView:
        return cls(**job.model_dump(), attention_cause=attention_cause(job))


class DeletionPlanView(BaseModel):
    """What ``DELETE /jobs/{id}`` would remove; shown to the user before confirming."""

    status: str = "success"
    job_id: str
    torrent: bool
    download_folder: str | None
    placed_paths: list[str]
    scan_path: str | None
    refused: str | None


BULK_JOBS_MAX = 100
"""Most job ids one bulk plan or delete request may carry."""


class BulkJobsRequest(BaseModel):
    """Body of ``POST /jobs/deletion-plan``, ``POST /jobs/delete`` and
    ``POST /jobs/dismiss``."""

    job_ids: list[str] = Field(min_length=1, max_length=BULK_JOBS_MAX)

    def unique_ids(self) -> list[str]:
        """The ids in request order, each once."""
        return list(dict.fromkeys(self.job_ids))


class JobDeletionPlanView(BaseModel):
    """One entry of a bulk plan. ``job`` is None for an unknown id, whose plan
    is refused."""

    job: JobView | None
    plan: DeletionPlanView


class BulkDeletionPlanView(BaseModel):
    status: str = "success"
    plans: list[JobDeletionPlanView]


class JobDeleteResultView(BaseModel):
    """One entry of a bulk delete. ``error`` carries the refusal reason or the
    downstream failure; ``job`` is the row as it stands afterwards."""

    job_id: str
    job: JobView | None
    error: str | None = None


class BulkDeleteView(BaseModel):
    status: str = "success"
    results: list[JobDeleteResultView]


class JobDismissResultView(BaseModel):
    """One entry of a bulk dismiss. ``error`` carries the refusal reason or
    "no such job"; ``job`` is the row as it stands afterwards."""

    job_id: str
    job: JobView | None
    error: str | None = None


class BulkDismissView(BaseModel):
    status: str = "success"
    results: list[JobDismissResultView]


class DownloadResponse(BaseModel):
    """Returned from ``POST /download`` - the bot tracks the job by hash."""

    status: str = "success"
    job: JobView


class JobsResponse(BaseModel):
    status: str = "success"
    jobs: list[JobView]


class WebhookPayload(BaseModel):
    """Body posted by ``scripts/notify_complete.py`` on torrent completion."""

    hash: str = Field(min_length=1)
    name: str = Field(min_length=1)
    content_path: str = ""
    """qBittorrent's %F: absolute path of the root file or folder. Optional so
    an older hook command without it still works; the pipeline then falls back
    to the transfer list at STOP_SEEDING."""


class DiskUsageView(BaseModel):
    """Free space on the media mount, measured by the gateway itself: it is
    the only service with the library mounted."""

    status: str
    path: str
    total_gb: float
    used_gb: float
    free_gb: float
    used_percent: float
