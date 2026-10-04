"""Redo a download: replace it with a newly picked torrent.

Accepted for a DONE job, and for a flagged job that placed nothing (its
torrent is gone and there is no other way to resolve it).

Order matters: the replacement row is created first, then the old job's
deletion plan is executed, then the new download is submitted. A crash or a
failed deletion between the steps leaves both rows visible, the old one not
``DELETED`` and the new one ``DOWNLOAD_SUBMITTED`` with no hash, so a repeat
of the redo reuses that replacement instead of creating another.
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import status as fastapi_status

from medialab_orchestrator.clients import JellyfinClient, TorrentDownloaderClient
from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.schemas.jobs import DownloadRequest, JobView
from medialab_orchestrator.services.deletion import DeletionService, plan_deletion
from medialab_orchestrator.services.download import create_submitted_job, submit_job
from medialab_orchestrator.store import JobStatus, JobStore, PipelineJob, WatchlistStore


def attach_redone_by(views: Iterable[JobView], *, store: JobStore) -> list[JobView]:
    """Stamp ``redone_by`` on each view from one store query for the whole batch."""
    views = list(views)
    replacements = store.redone_by(view.id for view in views)
    for view in views:
        view.redone_by = replacements.get(view.id)
    return views


def is_redoable(job: PipelineJob) -> bool:
    """DONE, or flagged with nothing in the library to replace."""
    if job.status is JobStatus.DONE:
        return True
    return job.status is JobStatus.NEEDS_ATTENTION and not job.placed_paths


def _check_redoable(old: PipelineJob, payload: DownloadRequest) -> None:
    if not is_redoable(old):
        raise AppException(
            status_code=fastapi_status.HTTP_409_CONFLICT,
            code=ErrorCode.JOB_NOT_DONE,
            detail=(
                f"Job {old.id} is {old.status.value}; only a DONE job, or a flagged job that "
                "placed nothing, can be redone."
            ),
        )
    if (payload.media_type, payload.tmdb_id) != (old.media_type, old.tmdb_id):
        raise AppException(
            status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
            code=ErrorCode.INVALID_INPUT,
            detail=(
                f"A redo must keep the title: job {old.id} is {old.media_type.value} {old.tmdb_id}."
            ),
        )
    plan = plan_deletion(old)
    if plan.refused:
        raise AppException(
            status_code=fastapi_status.HTTP_409_CONFLICT,
            code=ErrorCode.INVALID_INPUT,
            detail=plan.refused,
        )


def _replacement_request(old: PipelineJob, payload: DownloadRequest) -> DownloadRequest:
    """The picked torrent with the old job's scope: a redo never changes what
    the title covers, only which torrent supplies it."""
    return payload.model_copy(update={"season": old.season, "episode": old.episode})


async def redo_job(
    old: PipelineJob,
    payload: DownloadRequest,
    *,
    store: JobStore,
    watchlist: WatchlistStore,
    torrent: TorrentDownloaderClient,
    jellyfin: JellyfinClient,
) -> PipelineJob:
    """Replace ``old`` with the download in ``payload``; returns the new job.

    A follow's submission for the episode moves to the replacement as soon as
    it exists, so a redo that stalls at deletion still counts as submitted.
    """
    _check_redoable(old, payload)
    request = _replacement_request(old, payload)
    replacement = store.find_replacement(old.id) or create_submitted_job(
        store, request, redo_of=old.id
    )
    watchlist.repoint_submission(old.id, replacement.id)
    deletion = DeletionService(store=store, torrent_client=torrent, jellyfin_client=jellyfin)
    try:
        await deletion.execute(old)
    except (AppException, OSError) as exc:
        app_logger.warning("Redo of %s: deleting the original failed: %s", old.id, exc)
        raise AppException(
            status_code=fastapi_status.HTTP_502_BAD_GATEWAY,
            code=ErrorCode.REDO_DELETION_FAILED,
            detail=(
                f"Deleting job {old.id} failed; replacement {replacement.id} is waiting. "
                "Retry the redo."
            ),
        ) from exc
    return await submit_job(replacement, request, store=store, torrent=torrent)
