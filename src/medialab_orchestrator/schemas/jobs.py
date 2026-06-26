"""Request/response schemas for the gateway's stateful surface."""

from __future__ import annotations

from medialab_contracts import MediaType
from pydantic import BaseModel, Field

from medialab_orchestrator.store import JobStatus, PipelineJob


class DownloadRequest(BaseModel):
    """Body for ``POST /download``. Mirrors a bot download confirmation."""

    magnet_uri: str = Field(min_length=1)
    media_type: MediaType
    tmdb_id: int


class JobView(BaseModel):
    """A pipeline job as exposed to the bot. Wraps the store's PipelineJob."""

    id: int
    torrent_hash: str
    release_name: str
    media_type: MediaType
    tmdb_id: int
    resolved_title: str | None
    resolved_year: int | None
    source_path: str | None
    dest_path: str | None
    status: JobStatus
    last_error: str | None
    attempts: int
    created_at: str
    updated_at: str

    @classmethod
    def from_job(cls, job: PipelineJob) -> JobView:
        return cls(**job.model_dump())


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
