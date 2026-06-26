"""Webhook entry + aggregated health tests."""

from unittest.mock import AsyncMock

from medialab_contracts import MediaType

from medialab_orchestrator.store import JobStore

HASH = "abcdef0123456789abcdef0123456789abcdef01"


class TestWebhook:
    def test_known_hash_records_name_and_returns_202(
        self, app_client, store: JobStore, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ):
        store.create_job(torrent_hash=HASH, release_name="", media_type=MediaType.MOVIE, tmdb_id=5)
        torrent_client.transfer_info.return_value = {
            "media_type": "movie",
            "host_path": "/media/Movies",
            "tmdb_id": 5,
        }
        torrent_client.tmdb_detail.return_value = {"title": "Foo", "release_date": "2021-01-01"}

        resp = app_client.post(
            "/api/v1/webhooks/torrent-complete",
            json={"hash": HASH, "name": "Foo.2021.1080p"},
        )
        assert resp.status_code == 202
        # Release name recorded; background task ran the pipeline (TestClient runs
        # background tasks synchronously on response).
        assert store.get_job_by_hash(HASH).release_name == "Foo.2021.1080p"

    def test_orphan_hash_tracked(self, app_client, store: JobStore, torrent_client: AsyncMock):
        # No job exists; the webhook inserts one so the event is tracked. It will
        # fail at RESOLVE_META (orphan tmdb_id), which is acceptable.
        torrent_client.transfer_info.side_effect = Exception("no info")
        resp = app_client.post(
            "/api/v1/webhooks/torrent-complete",
            json={"hash": HASH, "name": "Mystery.Show"},
        )
        assert resp.status_code == 202
        assert store.get_job_by_hash(HASH).release_name == "Mystery.Show"

    def test_webhook_requires_key(self, unauthed_client):
        resp = unauthed_client.post(
            "/api/v1/webhooks/torrent-complete", json={"hash": HASH, "name": "x"}
        )
        assert resp.status_code == 403


class TestHealth:
    def test_public_and_aggregates(
        self, app_client, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ):
        torrent_client.is_reachable.return_value = True
        jellyfin_client.is_reachable.return_value = False
        body = app_client.get("/api/v1/health").json()
        assert body["status"] == "online"
        assert body["downstream"] == {"torrent_downloader": True, "medialab_jellyfin": False}

    def test_health_no_key_needed(
        self, unauthed_client, torrent_client: AsyncMock, jellyfin_client: AsyncMock
    ):
        torrent_client.is_reachable.return_value = True
        jellyfin_client.is_reachable.return_value = True
        assert unauthed_client.get("/api/v1/health").status_code == 200
