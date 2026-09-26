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
    # No content path known: the pipeline falls back to release_name for the folder.
    torrent_client.transfers.return_value = {"data": []}
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
        (tmp_path / "Shows" / TV_RELEASE / "Show.Name.S01E01.1080p.mkv").write_text("ep")
        _seed_tv_job(store)
        _wire_downstream(torrent_client, jellyfin_client)

        job = await worker.process(HASH)

        assert job.status is JobStatus.DONE
        assert job.resolved_title == "Show Name"
        assert job.resolved_year == 2019
        assert job.dest_path == str(tmp_path / "Shows" / "Show Name (2019)")
        torrent_client.remove_transfer.assert_awaited_once_with(HASH)
        assert job.seeding_removed_at is not None
        # The library root is registered once at setup, not per-download, so the
        # pipeline scans the already-covered path rather than registering it.
        jellyfin_client.register_path.assert_not_awaited()
        jellyfin_client.scan.assert_awaited_once()
        # The episode is placed and named; the emptied download folder is gone.
        episode = tmp_path / "Shows" / "Show Name (2019)" / "Season 01" / "Show Name S01E01.mkv"
        assert episode.read_text() == "ep"
        assert not (tmp_path / "Shows" / TV_RELEASE).exists()


class TestFailure:
    async def test_unparseable_episode_marks_failed(
        self,
        worker: PipelineWorker,
        store: JobStore,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
        tmp_path: Path,
        mocker,
    ):
        mocker.patch.object(worker_module.config, "media_mount_path", str(tmp_path))
        bad = tmp_path / "Shows" / "Show.Name.NoSeason.1080p"
        bad.mkdir(parents=True)
        (bad / "Show.Name.Bonus.mkv").write_text("x")
        _seed_tv_job(store, release="Show.Name.NoSeason.1080p")
        _wire_downstream(torrent_client, jellyfin_client)

        job = await worker.process(HASH)

        assert job.status is JobStatus.FAILED
        assert "RENAME" in (job.last_error or "")
        assert "Show.Name.Bonus.mkv" in (job.last_error or "")
        assert (bad / "Show.Name.Bonus.mkv").exists()
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
        torrent_client.remove_transfer.side_effect = AppException(
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
        bad = tmp_path / "Shows" / "Show.Name.NoSeason.1080p"
        bad.mkdir(parents=True)
        (bad / "Show.Name.Bonus.mkv").write_text("x")
        _seed_tv_job(store, release="Show.Name.NoSeason.1080p")
        _wire_downstream(torrent_client, jellyfin_client)

        failed = await worker.process(HASH)
        assert failed.status is JobStatus.FAILED

        # Operator fixes the folder and file names; retry re-enters from STOP_SEEDING.
        (tmp_path / "Shows" / TV_RELEASE).mkdir(parents=True)
        (tmp_path / "Shows" / TV_RELEASE / "Show.Name.S01E01.mkv").write_text("x")
        store.update_job(store.get_job_by_hash(HASH).id, release_name=TV_RELEASE)
        recovered = await worker.process(HASH)
        assert recovered.status is JobStatus.DONE
        assert torrent_client.remove_transfer.await_count == 2  # re-ran the early step


class TestResolveMetaWithoutCache:
    async def test_missing_transfer_info_does_not_block_the_pipeline(
        self,
        worker: PipelineWorker,
        store: JobStore,
        torrent_client: AsyncMock,
        jellyfin_client: AsyncMock,
        tmp_path: Path,
        mocker,
    ):
        # The downloader's hash cache is lost on every image rebuild; the job
        # already carries media_type and tmdb_id, so a 404 there is not fatal.
        mocker.patch.object(worker_module.config, "media_mount_path", str(tmp_path))
        (tmp_path / "Shows" / TV_RELEASE).mkdir(parents=True)
        (tmp_path / "Shows" / TV_RELEASE / "Show.Name.S01E01.1080p.mkv").write_text("ep")
        _seed_tv_job(store)
        _wire_downstream(torrent_client, jellyfin_client)
        torrent_client.transfer_info.return_value = None

        job = await worker.process(HASH)

        assert job.status is JobStatus.DONE
        assert job.source_path is None
        assert job.resolved_title == "Show Name"


class TestSourceRoot:
    async def test_stop_seeding_records_the_on_disk_root_from_content_path(
        self, worker, store, torrent_client, jellyfin_client, tmp_path, mocker
    ):
        mocker.patch.object(worker_module.config, "media_mount_path", str(tmp_path))
        on_disk = "Show.Name.S01.1080p.[GROUP]"  # display name differs from the folder
        (tmp_path / "Shows" / on_disk).mkdir(parents=True)
        (tmp_path / "Shows" / on_disk / "Show.Name.S01E01.mkv").write_text("ep")
        _seed_tv_job(store, release="Show Name S01 1080p [GROUP]")
        _wire_downstream(torrent_client, jellyfin_client)
        torrent_client.transfers.return_value = {
            "data": [{"hash": HASH, "content_path": f"F:\\Media\\Shows\\{on_disk}"}]
        }

        job = await worker.process(HASH)

        assert job.status is JobStatus.DONE
        assert job.source_path == on_disk
        assert (
            tmp_path / "Shows" / "Show Name (2019)" / "Season 01" / "Show Name S01E01.mkv"
        ).exists()
        assert job.last_error is None

    async def test_missing_source_folder_fails_loudly(
        self, worker, store, torrent_client, jellyfin_client, tmp_path, mocker
    ):
        mocker.patch.object(worker_module.config, "media_mount_path", str(tmp_path))
        _seed_tv_job(store, release="Not.On.Disk.S01")
        _wire_downstream(torrent_client, jellyfin_client)

        job = await worker.process(HASH)

        assert job.status is JobStatus.FAILED
        assert ErrorCode.SOURCE_NOT_FOUND.value not in (
            job.last_error or ""
        )  # detail text, not code
        assert "Download folder not found" in (job.last_error or "")
        jellyfin_client.scan.assert_not_awaited()

    async def test_retry_after_completed_move_is_done_not_missing(
        self, worker, store, torrent_client, jellyfin_client, tmp_path, mocker
    ):
        mocker.patch.object(worker_module.config, "media_mount_path", str(tmp_path))
        # Files already placed by an earlier run; the download folder is gone.
        placed = tmp_path / "Shows" / "Show Name (2019)" / "Season 01"
        placed.mkdir(parents=True)
        (placed / "Show Name S01E01.mkv").write_text("ep")
        _seed_tv_job(store)
        _wire_downstream(torrent_client, jellyfin_client)

        job = await worker.process(HASH)

        assert job.status is JobStatus.DONE
        jellyfin_client.scan.assert_awaited_once()

    async def test_done_clears_a_stale_last_error(
        self, worker, store, torrent_client, jellyfin_client, tmp_path, mocker
    ):
        mocker.patch.object(worker_module.config, "media_mount_path", str(tmp_path))
        (tmp_path / "Shows" / TV_RELEASE).mkdir(parents=True)
        (tmp_path / "Shows" / TV_RELEASE / "Show.Name.S01E01.mkv").write_text("ep")
        _seed_tv_job(store)
        store.update_job(store.get_job_by_hash(HASH).id, status=JobStatus.FAILED, last_error="old")
        _wire_downstream(torrent_client, jellyfin_client)

        job = await worker.process(HASH)

        assert job.status is JobStatus.DONE
        assert job.last_error is None
