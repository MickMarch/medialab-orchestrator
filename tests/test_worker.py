"""PipelineWorker tests: full advance, failure capture, idempotent retry."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from medialab_contracts import MediaType

from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.services import worker as worker_module
from medialab_orchestrator.services.worker import PipelineWorker
from medialab_orchestrator.store import JobStatus, JobStore

HASH = "a" * 40
TV_RELEASE = "Show.Name.S01.1080p.GROUP"


@pytest.fixture
def worker(
    store: JobStore, torrent_client: AsyncMock, jellyfin_client: AsyncMock
) -> PipelineWorker:
    return PipelineWorker(
        store=store, torrent_client=torrent_client, jellyfin_client=jellyfin_client
    )


def _seed_tv_job(store: JobStore, *, release: str = TV_RELEASE) -> None:
    store.create_job(
        torrent_hash=HASH,
        release_name=release,
        media_type=MediaType.SHOW,
        tmdb_id=42,
        status=JobStatus.DOWNLOADING,
    )


def _wire_downstream(
    torrent_client: AsyncMock, jellyfin_client: AsyncMock, *, host_subdir: str = "Shows"
) -> None:
    torrent_client.transfer_info.return_value = {
        "media_type": "show",
        "host_path": f"/media/{host_subdir}",
        "tmdb_id": 42,
    }
    # torrent-downloader wraps the raw TMDB body under `data`.
    torrent_client.tmdb_detail.return_value = {
        "status": "success",
        "message": "",
        "data": {"name": "Show Name", "first_air_date": "2019-03-01"},
    }


class TestHappyPath:
    async def test_tv_runs_to_done(
        self,
        worker: PipelineWorker,
        store: JobStore,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
        tmp_path: Path,
        mocker,
    ):
        mocker.patch.object(worker_module.config, "media_mount_path", str(tmp_path))
        (tmp_path / "Shows" / TV_RELEASE).mkdir(parents=True)
        _seed_tv_job(store)
        _wire_downstream(torrent_client, jellyfin_client)

        job = await worker.process(HASH)

        assert job.status is JobStatus.DONE
        assert job.resolved_title == "Show Name"
        assert job.resolved_year == 2019
        assert job.dest_path == str(tmp_path / "Shows" / "Show Name (2019)" / "Season 01")
        torrent_client.stop_seeding.assert_awaited_once()
        # The library root is registered once at setup, not per-download, so the
        # pipeline scans the already-covered path rather than registering it.
        jellyfin_client.register_path.assert_not_awaited()
        jellyfin_client.scan.assert_awaited_once()
        # The moved folder is in place.
        assert (tmp_path / "Shows" / "Show Name (2019)" / "Season 01").exists()


class TestFailure:
    async def test_unparseable_season_marks_failed(
        self,
        worker: PipelineWorker,
        store: JobStore,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
        tmp_path: Path,
        mocker,
    ):
        mocker.patch.object(worker_module.config, "media_mount_path", str(tmp_path))
        _seed_tv_job(store, release="Show.Name.NoSeason.1080p")
        _wire_downstream(torrent_client, jellyfin_client)

        job = await worker.process(HASH)

        assert job.status is JobStatus.FAILED
        assert "RENAME" in (job.last_error or "")
        assert job.attempts == 1
        jellyfin_client.register_path.assert_not_awaited()

    async def test_downstream_failure_marks_failed(
        self,
        worker: PipelineWorker,
        store: JobStore,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
    ):
        _seed_tv_job(store)
        torrent_client.stop_seeding.side_effect = AppException(
            status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="boom"
        )
        job = await worker.process(HASH)
        assert job.status is JobStatus.FAILED
        assert "STOP_SEEDING" in (job.last_error or "")


class TestRetry:
    async def test_retry_after_fix_completes(
        self,
        worker: PipelineWorker,
        store: JobStore,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
        tmp_path: Path,
        mocker,
    ):
        mocker.patch.object(worker_module.config, "media_mount_path", str(tmp_path))
        _seed_tv_job(store, release="Show.Name.NoSeason.1080p")
        _wire_downstream(torrent_client, jellyfin_client)

        failed = await worker.process(HASH)
        assert failed.status is JobStatus.FAILED

        # Operator fixes the folder name; retry re-enters from STOP_SEEDING.
        (tmp_path / "Shows" / TV_RELEASE).mkdir(parents=True)
        store.update_job(store.get_job_by_hash(HASH).id, release_name=TV_RELEASE)
        recovered = await worker.process(HASH)
        assert recovered.status is JobStatus.DONE
        assert torrent_client.stop_seeding.await_count == 2  # re-ran the early step
