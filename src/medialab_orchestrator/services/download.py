"""Submit a download: the one code path behind ``POST /download`` and redo.

A job is born keyed by a surrogate id; the real info-hash is not known up
front for a ``.torrent``-URL source, so it is stamped from the downloader's
response (or backfilled by the completion webhook).
"""

from __future__ import annotations

from medialab_orchestrator.clients import TorrentDownloaderClient
from medialab_orchestrator.core.errors import AppException
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.schemas.jobs import DownloadRequest
from medialab_orchestrator.services.metadata import resolve_title_year
from medialab_orchestrator.store import JobStore, PipelineJob

_RESPONSE_HASH_KEY = "torrent_hash"


def create_submitted_job(
    store: JobStore, payload: DownloadRequest, *, redo_of: str | None = None
) -> PipelineJob:
    """The ``DOWNLOAD_SUBMITTED`` row for a download request, before any
    downstream call. Completion overwrites ``release_name`` with the on-disk name."""
    return store.create_job(
        release_name=payload.release_name.strip(),
        media_type=payload.media_type,
        tmdb_id=payload.tmdb_id,
        season=payload.season,
        episode=payload.episode,
        redo_of=redo_of,
    )


async def submit_job(
    job: PipelineJob,
    payload: DownloadRequest,
    *,
    store: JobStore,
    torrent: TorrentDownloaderClient,
) -> PipelineJob:
    """Resolve the title best effort, forward the download, stamp the hash.

    The tmdb_id is known at submit, so ``/jobs`` shows "Title (Year)" from the
    start; a metadata hiccup must not block the download, and RESOLVE_META
    backfills it. The downloader returns the hash it resolved (parsed from a
    magnet, or read back from qBittorrent for a ``.torrent`` URL); when it is
    missing the completion webhook backfills it.
    """
    try:
        title, year = await resolve_title_year(torrent, payload.media_type, payload.tmdb_id)
        if title:
            job = store.update_job(job.id, resolved_title=title, resolved_year=year)
    except AppException as exc:
        app_logger.warning("Title resolve at submit failed for %s: %s", job.id, exc.detail)

    result = await torrent.download(
        source_url=payload.source_url,
        media_type=payload.media_type,
        tmdb_id=payload.tmdb_id,
    )
    torrent_hash = result.get(_RESPONSE_HASH_KEY) if isinstance(result, dict) else None
    if torrent_hash:
        job = store.stamp_hash(job.id, torrent_hash)
    return job
