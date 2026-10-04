"""Why a job is waiting on a human, derived from the error its writer left.

The health poll and the worker encode the cause in ``last_error``; this module
owns the message shapes so the writers and the reader cannot drift. Clients
use the cause to offer the one action that can end the attention state
(``docs/specs/dismiss-attention-jobs.md``).
"""

from __future__ import annotations

from enum import Enum

from medialab_orchestrator.store import JobStatus, PipelineJob

TORRENT_GONE_MESSAGE = "torrent no longer in qBittorrent"
"""Written by the health poll when a job's transfer vanished before completion."""

_DOWNLOAD_ERROR_PREFIX = "qBittorrent state "
_STEP_SEPARATOR = ": "

ATTENTION_STATUSES = frozenset({JobStatus.NEEDS_ATTENTION, JobStatus.FAILED})
"""Statuses a human may act on: retry, redo or dismiss."""


class AttentionCause(str, Enum):
    TORRENT_GONE = "TORRENT_GONE"
    DOWNLOAD_ERROR = "DOWNLOAD_ERROR"
    RENAME = "RENAME"
    SCAN = "SCAN"
    OTHER = "OTHER"


def download_error_message(state: str, resumes: int) -> str:
    """Written by the health poll once its resume budget is spent."""
    return f"{_DOWNLOAD_ERROR_PREFIX}{state} after {resumes} resumes"


def step_error_message(step: JobStatus, detail: str) -> str:
    """Written by the worker when a pipeline step raises."""
    return f"{step.value}{_STEP_SEPARATOR}{detail}"


def _step_prefix(step: JobStatus) -> str:
    return f"{step.value}{_STEP_SEPARATOR}"


def attention_cause(job: PipelineJob) -> AttentionCause | None:
    """The cause behind a flagged job, or None when it is not flagged.

    ``TORRENT_GONE`` requires that nothing was placed: with files in the
    library a redo is no longer a free replacement.
    """
    if job.status not in ATTENTION_STATUSES:
        return None
    error = job.last_error or ""
    if error == TORRENT_GONE_MESSAGE:
        return AttentionCause.TORRENT_GONE if not job.placed_paths else AttentionCause.OTHER
    if error.startswith(_DOWNLOAD_ERROR_PREFIX):
        return AttentionCause.DOWNLOAD_ERROR
    if error.startswith(_step_prefix(JobStatus.RENAME)):
        return AttentionCause.RENAME
    if error.startswith(_step_prefix(JobStatus.SCAN)):
        return AttentionCause.SCAN
    return AttentionCause.OTHER
