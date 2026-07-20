"""Gateway HTTP tests: download creates a job, jobs/transfers/storage, auth."""

from unittest.mock import AsyncMock

from medialab_contracts import MediaType

from medialab_orchestrator.store import JobStatus, JobStore

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
        from medialab_orchestrator.core.errors import AppException, ErrorCode

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

    def test_storage_proxied(self, app_client, torrent_client: AsyncMock):
        torrent_client.storage.return_value = {"free": 1}
        assert app_client.get("/api/v1/storage").json() == {"free": 1}


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
