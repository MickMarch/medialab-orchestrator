"""Dismissing flagged jobs and the attention cause derived for the clients.

Spec: ``docs/specs/dismiss-attention-jobs.md`` in the workspace.
"""

from unittest.mock import AsyncMock

import pytest
from medialab_contracts import MediaType, SubmissionState

from medialab_orchestrator.services.attention import (
    TORRENT_GONE_MESSAGE,
    AttentionCause,
    attention_cause,
    download_error_message,
)
from medialab_orchestrator.store import JobStatus, JobStore, WatchlistStore

HASH = "d" * 40
RENAME_ERROR = "RENAME: No single season number in file name: 'info.mkv'"
SCAN_ERROR = "SCAN: jellyfin down"


def _job(store: JobStore, status: JobStatus, **fields):
    job = store.create_job(
        torrent_hash=HASH,
        release_name="Show.S06E04",
        media_type=MediaType.SHOW,
        tmdb_id=2,
        season=6,
        episode=4,
    )
    return store.update_job(job.id, status=status, **fields)


class TestAttentionCause:
    @pytest.mark.parametrize(
        ("last_error", "placed", "expected"),
        [
            (TORRENT_GONE_MESSAGE, [], AttentionCause.TORRENT_GONE),
            (download_error_message("error", 3), [], AttentionCause.DOWNLOAD_ERROR),
            (RENAME_ERROR, [], AttentionCause.RENAME),
            (SCAN_ERROR, ["/media/Shows/x.mkv"], AttentionCause.SCAN),
            ("something else", [], AttentionCause.OTHER),
            (None, [], AttentionCause.OTHER),
        ],
    )
    def test_needs_attention_causes(self, store, last_error, placed, expected):
        job = _job(store, JobStatus.NEEDS_ATTENTION, last_error=last_error, placed_paths=placed)
        assert attention_cause(job) is expected

    def test_failed_is_classified_too(self, store):
        job = _job(store, JobStatus.FAILED, last_error=RENAME_ERROR)
        assert attention_cause(job) is AttentionCause.RENAME

    def test_torrent_gone_with_placed_files_is_other(self, store):
        # Files in the library mean a redo is not a free replacement.
        job = _job(
            store,
            JobStatus.NEEDS_ATTENTION,
            last_error=TORRENT_GONE_MESSAGE,
            placed_paths=["/media/Shows/x.mkv"],
        )
        assert attention_cause(job) is AttentionCause.OTHER

    @pytest.mark.parametrize(
        "status",
        [JobStatus.DONE, JobStatus.DOWNLOADING, JobStatus.DELETED, JobStatus.DISMISSED],
    )
    def test_other_statuses_have_no_cause(self, store, status):
        job = _job(store, status, last_error=RENAME_ERROR)
        assert attention_cause(job) is None

    def test_view_carries_the_cause(self, app_client, store):
        job = _job(store, JobStatus.NEEDS_ATTENTION, last_error=TORRENT_GONE_MESSAGE)
        body = app_client.get(f"/api/v1/jobs/{job.id}").json()
        assert body["attention_cause"] == "TORRENT_GONE"
        assert body["dismissed_at"] is None


class TestDismissOne:
    @pytest.mark.parametrize("status", [JobStatus.NEEDS_ATTENTION, JobStatus.FAILED])
    def test_dismiss_sets_status_and_timestamp_keeping_the_error(
        self, app_client, store: JobStore, status
    ):
        job = _job(store, status, last_error=TORRENT_GONE_MESSAGE)
        resp = app_client.post(f"/api/v1/jobs/{job.id}/dismiss")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == JobStatus.DISMISSED.value
        assert body["dismissed_at"]
        assert body["last_error"] == TORRENT_GONE_MESSAGE
        assert body["attention_cause"] is None
        after = store.get_job_by_id(job.id)
        assert after.status is JobStatus.DISMISSED
        assert after.torrent_hash == HASH

    def test_dismiss_marks_the_follow_submission_ignored(
        self, app_client, store: JobStore, watchlist: WatchlistStore
    ):
        job = _job(store, JobStatus.NEEDS_ATTENTION, last_error=TORRENT_GONE_MESSAGE)
        watchlist.record_submission(2, 6, 4, job.id)
        app_client.post(f"/api/v1/jobs/{job.id}/dismiss")
        assert watchlist.submissions(2) == {(6, 4): (SubmissionState.IGNORED, job.id)}

    def test_dismissing_a_dismissed_job_is_a_no_op(self, app_client, store: JobStore):
        job = _job(store, JobStatus.NEEDS_ATTENTION, last_error="x")
        first = app_client.post(f"/api/v1/jobs/{job.id}/dismiss").json()
        second = app_client.post(f"/api/v1/jobs/{job.id}/dismiss")
        assert second.status_code == 200
        assert second.json()["dismissed_at"] == first["dismissed_at"]

    @pytest.mark.parametrize(
        "status",
        [
            JobStatus.DOWNLOAD_SUBMITTED,
            JobStatus.DOWNLOADING,
            JobStatus.RENAME,
            JobStatus.DONE,
            JobStatus.DELETED,
        ],
    )
    def test_other_statuses_are_409(self, app_client, store: JobStore, status):
        job = _job(store, status)
        resp = app_client.post(f"/api/v1/jobs/{job.id}/dismiss")
        assert resp.status_code == 409
        assert resp.json()["code"] == "JOB_NOT_DISMISSABLE"
        assert store.get_job_by_id(job.id).status is status

    def test_unknown_job_is_404(self, app_client):
        assert app_client.post("/api/v1/jobs/nope/dismiss").status_code == 404

    def test_dismissed_leaves_the_attention_count(
        self, app_client, store: JobStore, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ):
        torrent_client.is_reachable.return_value = True
        jellyfin_client.is_reachable.return_value = True
        job = _job(store, JobStatus.NEEDS_ATTENTION, last_error="x")
        assert app_client.get("/api/v1/health").json()["needs_attention"] == 1
        app_client.post(f"/api/v1/jobs/{job.id}/dismiss")
        assert app_client.get("/api/v1/health").json()["needs_attention"] == 0

    def test_dismissed_is_listable_by_status(self, app_client, store: JobStore):
        job = _job(store, JobStatus.NEEDS_ATTENTION, last_error="x")
        app_client.post(f"/api/v1/jobs/{job.id}/dismiss")
        listed = app_client.get("/api/v1/jobs?status=DISMISSED").json()["jobs"]
        assert [j["id"] for j in listed] == [job.id]


class TestDismissBulk:
    def test_each_id_gets_a_result_in_request_order(self, app_client, store: JobStore):
        a = _job(store, JobStatus.NEEDS_ATTENTION, last_error="x")
        b = store.create_job(release_name="Done", media_type=MediaType.MOVIE, tmdb_id=1)
        store.update_job(b.id, status=JobStatus.DONE)
        resp = app_client.post("/api/v1/jobs/dismiss", json={"job_ids": [b.id, "nope", a.id]})
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert [r["job_id"] for r in results] == [b.id, "nope", a.id]
        assert results[0]["error"] and results[0]["job"]["status"] == "DONE"
        assert results[1]["error"] == "no such job" and results[1]["job"] is None
        assert results[2]["error"] is None and results[2]["job"]["status"] == "DISMISSED"
        assert store.get_job_by_id(b.id).status is JobStatus.DONE
        assert store.get_job_by_id(a.id).status is JobStatus.DISMISSED

    def test_duplicates_collapse(self, app_client, store: JobStore):
        a = _job(store, JobStatus.FAILED, last_error="x")
        results = app_client.post("/api/v1/jobs/dismiss", json={"job_ids": [a.id, a.id]}).json()[
            "results"
        ]
        assert len(results) == 1 and results[0]["error"] is None

    def test_empty_and_oversized_bodies_are_422(self, app_client):
        assert app_client.post("/api/v1/jobs/dismiss", json={"job_ids": []}).status_code == 422
        too_many = [f"id{i}" for i in range(101)]
        assert (
            app_client.post("/api/v1/jobs/dismiss", json={"job_ids": too_many}).status_code == 422
        )


class TestRetryAfterDismiss:
    @pytest.mark.parametrize("status", [JobStatus.DISMISSED, JobStatus.DELETED])
    def test_retry_is_409_with_a_named_code(self, app_client, store: JobStore, status):
        job = _job(store, status)
        resp = app_client.post(f"/api/v1/jobs/{job.id}/retry")
        assert resp.status_code == 409
        assert resp.json()["code"] == "JOB_NOT_RETRYABLE"
        assert store.get_job_by_id(job.id).status is status
