"""Gateway HTTP tests: download creates a job, jobs/transfers/storage, auth."""

from unittest.mock import AsyncMock

import pytest
from medialab_contracts import MediaType, SubmissionState

from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.routers import gateway as gateway_module
from medialab_orchestrator.services import deletion as deletion_module
from medialab_orchestrator.store import JobStatus, JobStore, WatchlistStore

HASH = "abcdef0123456789abcdef0123456789abcdef01"
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Foo"
TORRENT_URL = "https://www.torlock.com/tor/1924049.torrent"


class TestDownload:
    def test_creates_job_and_forwards(self, app_client, store: JobStore, torrent_client: AsyncMock):
        # The downloader resolves and returns the hash; the gateway stamps it.
        torrent_client.download.return_value = {"status": "success", "torrent_hash": HASH}
        resp = app_client.post(
            "/api/v1/download",
            json={"source_url": MAGNET, "media_type": "movie", "tmdb_id": 99},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["job"]["torrent_hash"] == HASH
        assert body["job"]["status"] == JobStatus.DOWNLOAD_SUBMITTED.value
        assert isinstance(body["job"]["id"], str) and body["job"]["id"]
        # Job persisted and downstream called.
        assert store.get_job_by_hash(HASH).tmdb_id == 99
        torrent_client.download.assert_awaited_once()

    def test_forwards_source_url_to_downloader(self, app_client, torrent_client: AsyncMock):
        torrent_client.download.return_value = {"status": "success", "torrent_hash": HASH}
        app_client.post(
            "/api/v1/download",
            json={"source_url": TORRENT_URL, "media_type": "show", "tmdb_id": 42},
        )
        assert torrent_client.download.await_args.kwargs["source_url"] == TORRENT_URL

    def test_hashless_downloader_response_leaves_job_unstamped(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        # Readback failed downstream: the job exists but has no hash yet; the
        # completion webhook will backfill it.
        torrent_client.download.return_value = {"status": "success", "torrent_hash": None}
        resp = app_client.post(
            "/api/v1/download",
            json={"source_url": TORRENT_URL, "media_type": "show", "tmdb_id": 42},
        )
        assert resp.status_code == 202
        assert resp.json()["job"]["torrent_hash"] is None
        assert len(store.list_jobs()) == 1

    def test_resolves_title_at_submit(self, app_client, store: JobStore, torrent_client: AsyncMock):
        # tmdb_id is known at submit, so the gateway resolves Title (Year) now.
        torrent_client.download.return_value = {"status": "success", "torrent_hash": HASH}
        torrent_client.tmdb_detail.return_value = {
            "status": "success",
            "data": {"title": "Dune", "release_date": "2021-10-22"},
        }
        resp = app_client.post(
            "/api/v1/download",
            json={"source_url": MAGNET, "media_type": "movie", "tmdb_id": 438631},
        )
        assert resp.status_code == 202
        assert resp.json()["job"]["resolved_title"] == "Dune"
        assert store.get_job_by_hash(HASH).resolved_year == 2021

    def test_download_proceeds_when_title_resolve_fails(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        # A metadata hiccup must not block the actual download.
        torrent_client.download.return_value = {"status": "success", "torrent_hash": HASH}
        torrent_client.tmdb_detail.side_effect = AppException(
            status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="tmdb down"
        )
        resp = app_client.post(
            "/api/v1/download",
            json={"source_url": MAGNET, "media_type": "movie", "tmdb_id": 1},
        )
        assert resp.status_code == 202
        assert store.get_job_by_hash(HASH).resolved_title is None
        torrent_client.download.assert_awaited_once()


class TestJobs:
    def test_list_and_filter(self, app_client, store: JobStore):
        job = store.create_job(release_name="r", media_type=MediaType.MOVIE, tmdb_id=1)
        store.update_job(job.id, status=JobStatus.DONE)
        assert len(app_client.get("/api/v1/jobs").json()["jobs"]) == 1
        assert len(app_client.get("/api/v1/jobs?status=DONE").json()["jobs"]) == 1
        assert len(app_client.get("/api/v1/jobs?status=FAILED").json()["jobs"]) == 0

    def test_get_single(self, app_client, store: JobStore):
        job = store.create_job(release_name="r", media_type=MediaType.MOVIE, tmdb_id=1)
        resp = app_client.get(f"/api/v1/jobs/{job.id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == job.id

    def test_get_missing_404(self, app_client):
        resp = app_client.get("/api/v1/jobs/deadbeef")
        assert resp.status_code == 404
        assert resp.json()["code"] == "JOB_NOT_FOUND"


class TestTransfersAndStorage:
    def test_transfers_merges(self, app_client, store: JobStore, torrent_client: AsyncMock):
        torrent_client.transfers.return_value = {"data": []}
        store.create_job(release_name="r", media_type=MediaType.MOVIE, tmdb_id=1)
        body = app_client.get("/api/v1/transfers").json()
        assert "transfers" in body
        assert len(body["jobs"]) == 1

    def test_storage_measures_the_media_mount(self, app_client, tmp_path, mocker):
        mocker.patch.object(gateway_module.config, "media_mount_path", str(tmp_path))
        body = app_client.get("/api/v1/storage").json()
        assert body["status"] == "success"
        assert body["path"] == str(tmp_path)
        assert body["total_gb"] > 0
        assert 0 <= body["used_percent"] <= 100

    def test_storage_missing_mount_is_500(self, app_client, tmp_path, mocker):
        mocker.patch.object(gateway_module.config, "media_mount_path", str(tmp_path / "gone"))
        resp = app_client.get("/api/v1/storage")
        assert resp.status_code == 500
        assert resp.json()["code"] == "INTERNAL_ERROR"

    def test_stop_seeding_proxied_with_accepted(self, app_client, torrent_client: AsyncMock):
        torrent_client.stop_seeding.return_value = {
            "status": "success",
            "message": "All seeding transfers stopped.",
        }
        resp = app_client.post("/api/v1/transfers/stop-seeding")
        assert resp.status_code == 202
        assert resp.json()["message"] == "All seeding transfers stopped."
        torrent_client.stop_seeding.assert_awaited_once()

    def test_stop_seeding_creates_no_job(self, app_client, store: JobStore, torrent_client):
        torrent_client.stop_seeding.return_value = {"status": "success", "message": "ok"}
        app_client.post("/api/v1/transfers/stop-seeding")
        assert store.list_jobs() == []


class TestSearchTorrentsProxy:
    def test_requires_media_type(self, app_client, torrent_client: AsyncMock):
        torrent_client.search_torrents.return_value = {"data": {}}
        resp = app_client.get("/api/v1/search/torrents", params={"query": "the wire"})
        assert resp.status_code == 422

    def test_movie_search_forwards_scope(self, app_client, torrent_client: AsyncMock):
        torrent_client.search_torrents.return_value = {"data": {}}
        resp = app_client.get(
            "/api/v1/search/torrents", params={"query": "dune", "media_type": "movie"}
        )
        assert resp.status_code == 200
        scope = torrent_client.search_torrents.await_args.args[1]
        assert scope.media_type is MediaType.MOVIE
        assert scope.season is None
        assert scope.episode is None

    def test_show_season_search_forwards_scope(self, app_client, torrent_client: AsyncMock):
        torrent_client.search_torrents.return_value = {"data": {}}
        resp = app_client.get(
            "/api/v1/search/torrents",
            params={"query": "the wire", "media_type": "show", "season": 2},
        )
        assert resp.status_code == 200
        scope = torrent_client.search_torrents.await_args.args[1]
        assert scope.media_type is MediaType.SHOW
        assert scope.season == 2
        assert scope.episode is None

    def test_show_episode_search_forwards_scope(self, app_client, torrent_client: AsyncMock):
        torrent_client.search_torrents.return_value = {"data": {}}
        resp = app_client.get(
            "/api/v1/search/torrents",
            params={"query": "the wire", "media_type": "show", "season": 2, "episode": 5},
        )
        assert resp.status_code == 200
        scope = torrent_client.search_torrents.await_args.args[1]
        assert scope.season == 2
        assert scope.episode == 5

    def test_movie_with_season_rejected(self, app_client, torrent_client: AsyncMock):
        resp = app_client.get(
            "/api/v1/search/torrents",
            params={"query": "dune", "media_type": "movie", "season": 1},
        )
        assert resp.status_code == 422

    def test_orphan_episode_rejected(self, app_client, torrent_client: AsyncMock):
        resp = app_client.get(
            "/api/v1/search/torrents",
            params={"query": "show", "media_type": "show", "episode": 3},
        )
        assert resp.status_code == 422


class TestAuth:
    def test_missing_key_rejected(self, unauthed_client):
        resp = unauthed_client.get("/api/v1/jobs")
        assert resp.status_code == 403
        assert resp.json()["code"] == "UNAUTHORIZED"


class TestRetryResetsBudgets:
    def test_retry_of_needs_attention_resets_counters(
        self, app_client, store: JobStore, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ):
        job = store.create_job(
            torrent_hash=HASH, release_name="Foo.2021", media_type=MediaType.MOVIE, tmdb_id=1
        )
        store.update_job(
            job.id, status=JobStatus.NEEDS_ATTENTION, attempts=5, remediations=3, last_error="x"
        )
        # Downstream fails immediately so the job lands back in FAILED with attempts == 1,
        # proving the counters were reset before the pipeline ran.
        torrent_client.remove_transfer.side_effect = RuntimeError("boom")
        resp = app_client.post(f"/api/v1/jobs/{job.id}/retry")
        assert resp.status_code == 200
        after = store.get_job_by_id(job.id)
        assert after.status is JobStatus.FAILED
        assert (after.attempts, after.remediations) == (1, 0)


class TestDeletion:
    def test_plan_is_read_only(self, app_client, store: JobStore, torrent_client: AsyncMock):
        job = store.create_job(
            torrent_hash=HASH, release_name="Foo.2021", media_type=MediaType.MOVIE, tmdb_id=1
        )
        body = app_client.get(f"/api/v1/jobs/{job.id}/deletion-plan").json()
        assert body["torrent"] is True
        assert body["download_folder"].endswith("Foo.2021")
        assert body["refused"] is None
        assert store.get_job_by_id(job.id).status is JobStatus.DOWNLOAD_SUBMITTED
        torrent_client.remove_transfer.assert_not_awaited()

    def test_delete_marks_deleted_and_removes_torrent_with_files(
        self, app_client, store: JobStore, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ):
        job = store.create_job(
            torrent_hash=HASH, release_name="Foo.2021", media_type=MediaType.MOVIE, tmdb_id=1
        )
        resp = app_client.delete(f"/api/v1/jobs/{job.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "DELETED"
        torrent_client.remove_transfer.assert_awaited_once_with(HASH, delete_files=True)

    def test_delete_marks_the_follow_submission_ignored(
        self, app_client, store: JobStore, watchlist: WatchlistStore, torrent_client: AsyncMock
    ):
        job = store.create_job(
            torrent_hash=HASH, release_name="S.S02E05", media_type=MediaType.SHOW, tmdb_id=2
        )
        watchlist.record_submission(2, 2, 5, job.id)
        assert app_client.delete(f"/api/v1/jobs/{job.id}").status_code == 200
        assert watchlist.submissions(2) == {(2, 5): (SubmissionState.IGNORED, job.id)}

    def test_refused_delete_leaves_the_submission(
        self, app_client, store: JobStore, watchlist: WatchlistStore
    ):
        job = store.create_job(
            torrent_hash=HASH, release_name="S.S02E05", media_type=MediaType.SHOW, tmdb_id=2
        )
        store.update_job(job.id, status=JobStatus.DELETED)
        watchlist.record_submission(2, 2, 5, job.id)
        assert app_client.delete(f"/api/v1/jobs/{job.id}").status_code == 409
        assert watchlist.submissions(2) == {(2, 5): (SubmissionState.SUBMITTED, job.id)}

    def test_delete_unknown_job_is_404(self, app_client):
        assert app_client.delete("/api/v1/jobs/nope").status_code == 404

    def test_retry_of_deleted_job_is_409(self, app_client, store: JobStore):
        job = store.create_job(
            torrent_hash=HASH, release_name="Foo.2021", media_type=MediaType.MOVIE, tmdb_id=1
        )
        store.update_job(job.id, status=JobStatus.DELETED)
        assert app_client.post(f"/api/v1/jobs/{job.id}/retry").status_code == 409


class TestSearchCacheProxy:
    def test_clear_cache_proxied(self, app_client, torrent_client: AsyncMock):
        torrent_client.clear_search_cache.return_value = {"status": "success", "cleared": True}
        resp = app_client.delete("/api/v1/search/cache")
        assert resp.status_code == 200
        assert resp.json()["cleared"] is True
        torrent_client.clear_search_cache.assert_awaited_once()


class TestSearchAltQuery:
    def test_alt_query_forwarded(self, app_client, torrent_client: AsyncMock):
        torrent_client.search_torrents.return_value = {"data": {}}
        resp = app_client.get(
            "/api/v1/search/torrents",
            params={
                "query": "Lee Cronin's The Mummy 2026",
                "media_type": "movie",
                "alt_query": "The Mummy 2026",
            },
        )
        assert resp.status_code == 200
        assert torrent_client.search_torrents.await_args.kwargs["alt_query"] == "The Mummy 2026"


class TestDownloadReleaseName:
    def test_release_name_stored_at_submit(self, app_client, torrent_client: AsyncMock):
        torrent_client.download.return_value = {"status": "success", "torrent_hash": HASH}
        resp = app_client.post(
            "/api/v1/download",
            json={
                "source_url": MAGNET,
                "media_type": "movie",
                "tmdb_id": 1,
                "release_name": " The.Mummy.2026.1080p ",
            },
        )
        assert resp.status_code == 202
        assert resp.json()["job"]["release_name"] == "The.Mummy.2026.1080p"

    def test_release_name_optional(self, app_client, torrent_client: AsyncMock):
        torrent_client.download.return_value = {"status": "success", "torrent_hash": HASH}
        resp = app_client.post(
            "/api/v1/download", json={"source_url": MAGNET, "media_type": "movie", "tmdb_id": 1}
        )
        assert resp.json()["job"]["release_name"] == ""


class TestDownloadScope:
    def test_season_and_episode_stored_at_submit(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        torrent_client.download.return_value = {"status": "success", "torrent_hash": HASH}
        resp = app_client.post(
            "/api/v1/download",
            json={
                "source_url": MAGNET,
                "media_type": "show",
                "tmdb_id": 1,
                "season": 2,
                "episode": 5,
            },
        )
        assert resp.status_code == 202
        assert (resp.json()["job"]["season"], resp.json()["job"]["episode"]) == (2, 5)
        assert store.get_job_by_hash(HASH).episode == 5

    def test_scope_optional_for_existing_callers(
        self, app_client, store: JobStore, torrent_client: AsyncMock
    ):
        torrent_client.download.return_value = {"status": "success", "torrent_hash": HASH}
        resp = app_client.post(
            "/api/v1/download", json={"source_url": MAGNET, "media_type": "show", "tmdb_id": 1}
        )
        assert resp.status_code == 202
        assert (resp.json()["job"]["season"], resp.json()["job"]["episode"]) == (None, None)
        assert store.get_job_by_hash(HASH).season is None


class TestRedo:
    """``POST /jobs/{id}/redo``: create the replacement, delete the old, submit the new."""

    NEW_HASH = "0123456789abcdef0123456789abcdef01234567"
    NEW_MAGNET = f"magnet:?xt=urn:btih:{NEW_HASH}&dn=Bar"

    @pytest.fixture
    def media(self, tmp_path, mocker):
        mocker.patch.object(deletion_module.config, "media_mount_path", str(tmp_path))
        (tmp_path / "Movies").mkdir()
        (tmp_path / "Shows").mkdir()
        return tmp_path

    def _done_movie(self, store: JobStore, media):
        job = store.create_job(
            torrent_hash=HASH, release_name="Foo.2021", media_type=MediaType.MOVIE, tmdb_id=1
        )
        return store.update_job(
            job.id,
            status=JobStatus.DONE,
            seeding_removed_at="t",
            dest_path=str(media / "Movies" / "Foo (2021)"),
            placed_paths=[str(media / "Movies" / "Foo (2021)" / "Foo (2021).mkv")],
        )

    def _done_episode(self, store: JobStore, media):
        job = store.create_job(
            torrent_hash=HASH,
            release_name="Show.S02E05",
            media_type=MediaType.SHOW,
            tmdb_id=2,
            season=2,
            episode=5,
        )
        return store.update_job(
            job.id,
            status=JobStatus.DONE,
            seeding_removed_at="t",
            dest_path=str(media / "Shows" / "Show (2019)"),
            placed_paths=[str(media / "Shows" / "Show (2019)" / "Season 02" / "Show S02E05.mkv")],
        )

    def _redo(self, app_client, job_id: str, media_type: str, tmdb_id: int):
        return app_client.post(
            f"/api/v1/jobs/{job_id}/redo",
            json={
                "source_url": self.NEW_MAGNET,
                "media_type": media_type,
                "tmdb_id": tmdb_id,
                "release_name": "Bar.2021",
            },
        )

    def test_unknown_job_is_404(self, app_client, media):
        assert self._redo(app_client, "nope", "movie", 1).status_code == 404

    def test_non_done_job_is_409(self, app_client, store: JobStore, media, torrent_client):
        job = store.create_job(
            torrent_hash=HASH, release_name="Foo.2021", media_type=MediaType.MOVIE, tmdb_id=1
        )
        resp = self._redo(app_client, job.id, "movie", 1)
        assert resp.status_code == 409
        assert resp.json()["code"] == "JOB_NOT_DONE"
        assert len(store.list_jobs()) == 1
        torrent_client.download.assert_not_awaited()

    def test_refused_plan_is_409(self, app_client, store: JobStore, media, torrent_client):
        # A DONE show without placed_paths cannot be deleted safely.
        job = store.create_job(
            torrent_hash=HASH, release_name="Show.S01", media_type=MediaType.SHOW, tmdb_id=2
        )
        store.update_job(
            job.id,
            status=JobStatus.DONE,
            seeding_removed_at="t",
            dest_path=str(media / "Shows" / "Show (2019)"),
        )
        resp = self._redo(app_client, job.id, "show", 2)
        assert resp.status_code == 409
        assert store.get_job_by_id(job.id).status is JobStatus.DONE
        assert len(store.list_jobs()) == 1
        torrent_client.download.assert_not_awaited()

    @pytest.mark.parametrize(("media_type", "tmdb_id"), [("show", 1), ("movie", 2)])
    def test_mismatched_title_is_422(
        self, app_client, store: JobStore, media, torrent_client, media_type, tmdb_id
    ):
        job = self._done_movie(store, media)
        resp = self._redo(app_client, job.id, media_type, tmdb_id)
        assert resp.status_code == 422
        assert store.get_job_by_id(job.id).status is JobStatus.DONE
        assert len(store.list_jobs()) == 1
        torrent_client.download.assert_not_awaited()

    def test_success_replaces_the_job(
        self, app_client, store: JobStore, media, torrent_client, jellyfin_client
    ):
        torrent_client.download.return_value = {"status": "success", "torrent_hash": self.NEW_HASH}
        old = self._done_episode(store, media)
        resp = self._redo(app_client, old.id, "show", 2)
        assert resp.status_code == 202
        new = resp.json()["job"]
        assert new["id"] != old.id
        assert new["redo_of"] == old.id
        assert new["status"] == JobStatus.DOWNLOAD_SUBMITTED.value
        assert new["torrent_hash"] == self.NEW_HASH
        assert (new["season"], new["episode"]) == (2, 5)
        assert new["release_name"] == "Bar.2021"
        assert store.get_job_by_id(old.id).status is JobStatus.DELETED
        assert torrent_client.download.await_args.kwargs["source_url"] == self.NEW_MAGNET
        jellyfin_client.scan.assert_awaited_once_with(
            path=str(media / "Shows" / "Show (2019)"), update_type="Deleted"
        )

    def test_success_repoints_the_follow_submission(
        self, app_client, store: JobStore, watchlist: WatchlistStore, media, torrent_client
    ):
        torrent_client.download.return_value = {"status": "success", "torrent_hash": self.NEW_HASH}
        old = self._done_episode(store, media)
        watchlist.record_submission(2, 2, 5, old.id)
        resp = self._redo(app_client, old.id, "show", 2)
        assert resp.status_code == 202
        new_id = resp.json()["job"]["id"]
        assert watchlist.submissions(2) == {(2, 5): (SubmissionState.SUBMITTED, new_id)}

    def test_success_resolves_title_best_effort(
        self, app_client, store: JobStore, media, torrent_client
    ):
        torrent_client.download.return_value = {"status": "success", "torrent_hash": self.NEW_HASH}
        torrent_client.tmdb_detail.return_value = {
            "status": "success",
            "data": {"title": "Foo", "release_date": "2021-01-01"},
        }
        old = self._done_movie(store, media)
        resp = self._redo(app_client, old.id, "movie", 1)
        assert resp.status_code == 202
        assert resp.json()["job"]["resolved_title"] == "Foo"

    def test_deletion_failure_is_502_and_the_replacement_is_reused(
        self, app_client, store: JobStore, media, torrent_client, jellyfin_client
    ):
        jellyfin_client.scan.side_effect = AppException(
            status_code=502, code=ErrorCode.DOWNSTREAM_UNAVAILABLE, detail="jellyfin down"
        )
        old = self._done_movie(store, media)
        resp = self._redo(app_client, old.id, "movie", 1)
        assert resp.status_code == 502
        assert resp.json()["code"] == "REDO_DELETION_FAILED"
        assert store.get_job_by_id(old.id).status is JobStatus.DONE
        torrent_client.download.assert_not_awaited()
        jobs = store.list_jobs()
        assert len(jobs) == 2
        replacement = next(job for job in jobs if job.id != old.id)
        assert replacement.redo_of == old.id
        assert replacement.status is JobStatus.DOWNLOAD_SUBMITTED
        assert replacement.torrent_hash is None

        jellyfin_client.scan.side_effect = None
        torrent_client.download.return_value = {"status": "success", "torrent_hash": self.NEW_HASH}
        resp = self._redo(app_client, old.id, "movie", 1)
        assert resp.status_code == 202
        assert resp.json()["job"]["id"] == replacement.id
        assert len(store.list_jobs()) == 2
        assert store.get_job_by_id(old.id).status is JobStatus.DELETED
        assert store.get_job_by_id(replacement.id).torrent_hash == self.NEW_HASH

    def test_redone_by_on_the_old_job_view(
        self, app_client, store: JobStore, media, torrent_client
    ):
        torrent_client.download.return_value = {"status": "success", "torrent_hash": self.NEW_HASH}
        torrent_client.transfers.return_value = {"data": []}
        old = self._done_movie(store, media)
        new_id = self._redo(app_client, old.id, "movie", 1).json()["job"]["id"]
        single = app_client.get(f"/api/v1/jobs/{old.id}").json()
        assert single["redone_by"] == new_id
        assert single["redo_of"] is None
        listed = {job["id"]: job for job in app_client.get("/api/v1/jobs").json()["jobs"]}
        assert listed[old.id]["redone_by"] == new_id
        assert listed[new_id]["redo_of"] == old.id
        assert listed[new_id]["redone_by"] is None
