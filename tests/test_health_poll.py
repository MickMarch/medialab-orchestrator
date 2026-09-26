"""Health poll rules, one test per row of the spec table, plus budgets and resets."""

from unittest.mock import AsyncMock

import pytest
from medialab_contracts import MediaType

from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.services.health_poll import HealthPoller
from medialab_orchestrator.store import JobStatus, JobStore

HASH = "c" * 40
RESUME_MAX = 3
RETRY_MAX = 2


@pytest.fixture
def worker() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def poller(store: JobStore, torrent_client: AsyncMock, worker: AsyncMock) -> HealthPoller:
    return HealthPoller(
        store=store,
        torrent_client=torrent_client,
        worker=worker,
        auto_resume_max=RESUME_MAX,
        auto_retry_max=RETRY_MAX,
    )


def _transfer(state: str, *, progress: float = 0.5, name: str = "Foo.2021.1080p") -> dict:
    return {"hash": HASH, "state": state, "progress": progress, "name": name}


def _seed(store: JobStore, status: JobStatus, **fields):
    job = store.create_job(
        torrent_hash=HASH, release_name="Foo.2021", media_type=MediaType.MOVIE, tmdb_id=1
    )
    return store.update_job(job.id, status=status, **fields)


class TestAwaitingCompletion:
    async def test_errored_download_is_resumed_and_counted(
        self, poller, store, torrent_client, worker
    ):
        job = _seed(store, JobStatus.DOWNLOAD_SUBMITTED)
        torrent_client.transfers.return_value = {"data": [_transfer("error")]}
        await poller.tick()
        torrent_client.resume_transfer.assert_awaited_once_with(HASH)
        assert store.get_job_by_id(job.id).remediations == 1
        worker.process.assert_not_awaited()

    async def test_missing_files_counts_as_errored(self, poller, store, torrent_client):
        _seed(store, JobStatus.DOWNLOADING)
        torrent_client.transfers.return_value = {"data": [_transfer("missingFiles")]}
        await poller.tick()
        torrent_client.resume_transfer.assert_awaited_once()

    async def test_resume_budget_exhausted_flags_needs_attention(
        self, poller, store, torrent_client
    ):
        job = _seed(store, JobStatus.DOWNLOAD_SUBMITTED, remediations=RESUME_MAX)
        torrent_client.transfers.return_value = {"data": [_transfer("error")]}
        await poller.tick()
        torrent_client.resume_transfer.assert_not_awaited()
        flagged = store.get_job_by_id(job.id)
        assert flagged.status is JobStatus.NEEDS_ATTENTION
        assert "error" in (flagged.last_error or "")
        assert f"{RESUME_MAX} resumes" in (flagged.last_error or "")

    @pytest.mark.parametrize(
        "transfer",
        [
            _transfer("stoppedUP", progress=1.0),
            _transfer("downloading", progress=1.0),
            _transfer("uploading", progress=0.999),
        ],
    )
    async def test_completed_but_unnoticed_runs_the_pipeline(
        self, poller, store, torrent_client, worker, transfer
    ):
        _seed(store, JobStatus.DOWNLOAD_SUBMITTED)
        torrent_client.transfers.return_value = {"data": [transfer]}
        await poller.tick()
        worker.process.assert_awaited_once_with(HASH)

    async def test_missed_completion_fills_an_empty_release_name(
        self, poller, store, torrent_client, worker
    ):
        job = store.create_job(
            torrent_hash=HASH, release_name="", media_type=MediaType.MOVIE, tmdb_id=1
        )
        torrent_client.transfers.return_value = {
            "data": [_transfer("stoppedUP", progress=1.0, name="Real.Name.2021")]
        }
        await poller.tick()
        assert store.get_job_by_id(job.id).release_name == "Real.Name.2021"
        worker.process.assert_awaited_once_with(HASH)

    async def test_torrent_gone_flags_needs_attention(self, poller, store, torrent_client, worker):
        job = _seed(store, JobStatus.DOWNLOAD_SUBMITTED)
        torrent_client.transfers.return_value = {"data": []}
        await poller.tick()
        flagged = store.get_job_by_id(job.id)
        assert flagged.status is JobStatus.NEEDS_ATTENTION
        assert "no longer in qBittorrent" in (flagged.last_error or "")
        worker.process.assert_not_awaited()

    async def test_still_downloading_is_left_alone(self, poller, store, torrent_client, worker):
        _seed(store, JobStatus.DOWNLOADING)
        torrent_client.transfers.return_value = {"data": [_transfer("downloading", progress=0.4)]}
        await poller.tick()
        torrent_client.resume_transfer.assert_not_awaited()
        worker.process.assert_not_awaited()

    async def test_stalled_is_not_an_error(self, poller, store, torrent_client):
        _seed(store, JobStatus.DOWNLOADING)
        torrent_client.transfers.return_value = {"data": [_transfer("stalledDL", progress=0.4)]}
        await poller.tick()
        torrent_client.resume_transfer.assert_not_awaited()

    async def test_job_without_hash_is_left_alone(self, poller, store, torrent_client, worker):
        store.create_job(release_name="x", media_type=MediaType.MOVIE, tmdb_id=1)
        torrent_client.transfers.return_value = {"data": []}
        await poller.tick()
        assert store.list_jobs()[0].status is JobStatus.DOWNLOAD_SUBMITTED
        worker.process.assert_not_awaited()


class TestFailed:
    async def test_failed_within_budget_is_retried(self, poller, store, torrent_client, worker):
        _seed(store, JobStatus.FAILED, attempts=1, last_error="RENAME: boom")
        torrent_client.transfers.return_value = {"data": []}
        await poller.tick()
        worker.process.assert_awaited_once_with(HASH)

    async def test_failed_over_budget_flags_keeping_last_error(
        self, poller, store, torrent_client, worker
    ):
        job = _seed(store, JobStatus.FAILED, attempts=RETRY_MAX + 1, last_error="RENAME: boom")
        torrent_client.transfers.return_value = {"data": []}
        await poller.tick()
        flagged = store.get_job_by_id(job.id)
        assert flagged.status is JobStatus.NEEDS_ATTENTION
        assert flagged.last_error == "RENAME: boom"
        worker.process.assert_not_awaited()


class TestTerminalAndRobustness:
    @pytest.mark.parametrize("status", [JobStatus.DONE, JobStatus.NEEDS_ATTENTION])
    async def test_terminal_jobs_are_untouched(self, poller, store, torrent_client, worker, status):
        job = _seed(store, status)
        torrent_client.transfers.return_value = {"data": [_transfer("error")]}
        await poller.tick()
        assert store.get_job_by_id(job.id).status is status
        torrent_client.resume_transfer.assert_not_awaited()
        worker.process.assert_not_awaited()

    async def test_unreachable_downloader_skips_the_tick(
        self, poller, store, torrent_client, worker
    ):
        job = _seed(store, JobStatus.DOWNLOAD_SUBMITTED)
        torrent_client.transfers.side_effect = AppException(
            status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="down"
        )
        await poller.tick()
        assert store.get_job_by_id(job.id).status is JobStatus.DOWNLOAD_SUBMITTED

    async def test_a_raising_job_does_not_stop_the_sweep(
        self, poller, store, torrent_client, worker
    ):
        first = _seed(store, JobStatus.DOWNLOAD_SUBMITTED)
        other_hash = "d" * 40
        second = store.create_job(
            torrent_hash=other_hash, release_name="Bar", media_type=MediaType.MOVIE, tmdb_id=2
        )
        torrent_client.transfers.return_value = {
            "data": [_transfer("error"), {"hash": other_hash, "state": "error", "progress": 0.1}]
        }
        torrent_client.resume_transfer.side_effect = [RuntimeError("boom"), None]
        await poller.tick()
        assert torrent_client.resume_transfer.await_count == 2
        # Newest first: `second` raised, `first` still got its resume counted.
        assert store.get_job_by_id(first.id).remediations == 1
        assert store.get_job_by_id(second.id).remediations == 0
