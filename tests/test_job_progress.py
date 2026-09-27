"""Live progress on /jobs: one transfers read joined to active jobs, and the
shared forward-only DOWNLOADING rule applied on read."""

from unittest.mock import AsyncMock

import pytest
from medialab_contracts import ETA_UNKNOWN_SECONDS, MediaType

from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.services import health_poll, progress
from medialab_orchestrator.store import JobStatus, JobStore

HASH = "d" * 40
OTHER_HASH = "e" * 40
PROGRESS = 0.42
SPEED = 3_100_000
ETA = 720


def _transfer(
    torrent_hash: str = HASH,
    *,
    state: str = "downloading",
    eta_seconds: int = ETA,
) -> dict:
    return {
        "hash": torrent_hash,
        "name": "Foo.2021.1080p",
        "progress": PROGRESS,
        "state": state,
        "download_speed": SPEED,
        "eta_seconds": eta_seconds,
    }


def _seed(store: JobStore, status: JobStatus, torrent_hash: str | None = HASH):
    job = store.create_job(
        torrent_hash=torrent_hash, release_name="Foo.2021", media_type=MediaType.MOVIE, tmdb_id=1
    )
    return store.update_job(job.id, status=status)


def _downstream_down() -> AppException:
    return AppException(status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="down")


class TestListJobs:
    def test_no_active_job_makes_no_transfers_call(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        _seed(store, JobStatus.DONE)
        body = app_client.get("/api/v1/jobs").json()
        assert body["jobs"][0]["progress"] is None
        torrent_client.transfers.assert_not_awaited()

    def test_empty_list_makes_no_transfers_call(self, app_client, torrent_client: AsyncMock):
        assert app_client.get("/api/v1/jobs").json()["jobs"] == []
        torrent_client.transfers.assert_not_awaited()

    def test_active_job_gets_progress_mapped_from_transfer(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        _seed(store, JobStatus.DOWNLOADING)
        torrent_client.transfers.return_value = {"data": [_transfer()]}
        job = app_client.get("/api/v1/jobs").json()["jobs"][0]
        assert job["progress"] == {
            "progress": PROGRESS,
            "download_speed": SPEED,
            "eta_seconds": ETA,
            "state": "downloading",
        }
        torrent_client.transfers.assert_awaited_once()

    def test_hash_match_is_case_insensitive(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        _seed(store, JobStatus.DOWNLOADING)
        torrent_client.transfers.return_value = {"data": [_transfer(HASH.upper())]}
        job = app_client.get("/api/v1/jobs").json()["jobs"][0]
        assert job["progress"]["progress"] == PROGRESS

    def test_unknown_eta_sentinel_becomes_none(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        _seed(store, JobStatus.DOWNLOADING)
        torrent_client.transfers.return_value = {
            "data": [_transfer(eta_seconds=ETA_UNKNOWN_SECONDS)]
        }
        job = app_client.get("/api/v1/jobs").json()["jobs"][0]
        assert job["progress"]["eta_seconds"] is None

    def test_job_without_hash_gets_no_progress(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        _seed(store, JobStatus.DOWNLOAD_SUBMITTED, torrent_hash=None)
        body = app_client.get("/api/v1/jobs").json()
        assert body["jobs"][0]["progress"] is None
        torrent_client.transfers.assert_not_awaited()

    def test_one_transfers_read_for_many_active_jobs(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        _seed(store, JobStatus.DOWNLOADING)
        _seed(store, JobStatus.DOWNLOADING, torrent_hash=OTHER_HASH)
        done = _seed(store, JobStatus.DONE, torrent_hash="f" * 40)
        torrent_client.transfers.return_value = {"data": [_transfer(), _transfer(OTHER_HASH)]}
        jobs = app_client.get("/api/v1/jobs").json()["jobs"]
        with_progress = {j["torrent_hash"] for j in jobs if j["progress"] is not None}
        assert with_progress == {HASH, OTHER_HASH}
        assert next(j for j in jobs if j["id"] == done.id)["progress"] is None
        torrent_client.transfers.assert_awaited_once()

    def test_active_job_missing_from_transfers_gets_no_progress(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        _seed(store, JobStatus.DOWNLOADING)
        torrent_client.transfers.return_value = {"data": [_transfer(OTHER_HASH)]}
        assert app_client.get("/api/v1/jobs").json()["jobs"][0]["progress"] is None

    def test_failed_transfers_read_returns_jobs_without_progress(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        _seed(store, JobStatus.DOWNLOAD_SUBMITTED)
        torrent_client.transfers.side_effect = _downstream_down()
        resp = app_client.get("/api/v1/jobs")
        assert resp.status_code == 200
        job = resp.json()["jobs"][0]
        assert job["progress"] is None
        assert job["status"] == JobStatus.DOWNLOAD_SUBMITTED.value

    def test_submitted_job_with_active_transfer_is_returned_and_stored_downloading(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        job = _seed(store, JobStatus.DOWNLOAD_SUBMITTED)
        torrent_client.transfers.return_value = {"data": [_transfer(state="stalledDL")]}
        listed = app_client.get("/api/v1/jobs").json()["jobs"][0]
        assert listed["status"] == JobStatus.DOWNLOADING.value
        assert listed["progress"]["state"] == "stalledDL"
        assert store.get_job_by_id(job.id).status is JobStatus.DOWNLOADING

    @pytest.mark.parametrize("state", ["queuedDL", "pausedDL", "checkingDL"])
    def test_waiting_transfer_leaves_job_submitted(
        self, app_client, store: JobStore, torrent_client: AsyncMock, state: str
    ):
        job = _seed(store, JobStatus.DOWNLOAD_SUBMITTED)
        torrent_client.transfers.return_value = {"data": [_transfer(state=state)]}
        listed = app_client.get("/api/v1/jobs").json()["jobs"][0]
        assert listed["status"] == JobStatus.DOWNLOAD_SUBMITTED.value
        assert listed["progress"]["state"] == state
        assert store.get_job_by_id(job.id).status is JobStatus.DOWNLOAD_SUBMITTED

    def test_status_filter_still_applies(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        _seed(store, JobStatus.DOWNLOADING)
        _seed(store, JobStatus.DONE, torrent_hash=OTHER_HASH)
        body = app_client.get("/api/v1/jobs?status=DONE").json()
        assert [j["status"] for j in body["jobs"]] == [JobStatus.DONE.value]
        torrent_client.transfers.assert_not_awaited()


class TestGetJob:
    def test_active_job_gets_progress(self, app_client, store: JobStore, torrent_client: AsyncMock):
        job = _seed(store, JobStatus.DOWNLOAD_SUBMITTED)
        torrent_client.transfers.return_value = {"data": [_transfer()]}
        body = app_client.get(f"/api/v1/jobs/{job.id}").json()
        assert body["progress"]["download_speed"] == SPEED
        assert body["status"] == JobStatus.DOWNLOADING.value

    def test_inactive_job_makes_no_transfers_call(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        job = _seed(store, JobStatus.FAILED)
        body = app_client.get(f"/api/v1/jobs/{job.id}").json()
        assert body["progress"] is None
        torrent_client.transfers.assert_not_awaited()

    def test_failed_transfers_read_still_200(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        job = _seed(store, JobStatus.DOWNLOADING)
        torrent_client.transfers.side_effect = _downstream_down()
        resp = app_client.get(f"/api/v1/jobs/{job.id}")
        assert resp.status_code == 200
        assert resp.json()["progress"] is None


class TestSharedRule:
    def test_health_poll_and_read_use_the_same_rule_function(self):
        assert health_poll.advance_to_downloading is progress.advance_to_downloading

    async def test_health_poll_calls_the_rule(
        self, store: JobStore, torrent_client: AsyncMock, mocker
    ):
        spy = mocker.patch.object(health_poll, "advance_to_downloading")
        _seed(store, JobStatus.DOWNLOAD_SUBMITTED)
        torrent_client.transfers.return_value = {"data": [_transfer()]}
        poller = health_poll.HealthPoller(
            store=store, torrent_client=torrent_client, worker=AsyncMock()
        )
        await poller.tick()
        spy.assert_called_once()

    async def test_read_calls_the_rule(self, store: JobStore, torrent_client: AsyncMock, mocker):
        spy = mocker.patch.object(progress, "advance_to_downloading", wraps=lambda s, j, t: j)
        job = _seed(store, JobStatus.DOWNLOAD_SUBMITTED)
        torrent_client.transfers.return_value = {"data": [_transfer()]}
        await progress.with_progress([job], store=store, torrent=torrent_client)
        spy.assert_called_once()

    def test_rule_is_forward_only(self, store: JobStore):
        job = _seed(store, JobStatus.DOWNLOADING)
        before = store.get_job_by_id(job.id).updated_at
        result = progress.advance_to_downloading(store, job, _transfer())
        assert result.status is JobStatus.DOWNLOADING
        assert store.get_job_by_id(job.id).updated_at == before
