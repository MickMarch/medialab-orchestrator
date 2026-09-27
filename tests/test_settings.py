"""Runtime settings: engine on the orchestrator's config, hot poll interval,
and the gateway's aggregation and relay."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from medialab_contracts import SettingSource

from medialab_orchestrator.core.config import AppConfig
from medialab_orchestrator.core.settings import RuntimeSettings, SettingsStore, UnknownSettingError
from medialab_orchestrator.routers import settings as settings_router
from medialab_orchestrator.services import health_poll
from medialab_orchestrator.services.health_poll import HealthPoller


def _runtime(tmp_path: Path, **env) -> tuple[AppConfig, RuntimeSettings, Path]:
    cfg = AppConfig(_env_file=None, **env)
    path = tmp_path / "settings.json"
    return cfg, RuntimeSettings(cfg, SettingsStore(path)), path


class TestEngine:
    def test_declared_keys_and_sources(self, tmp_path: Path) -> None:
        cfg, rt, _ = _runtime(tmp_path, auto_retry_max=5)
        assert [v.key for v in rt.views()] == [
            "health_poll_interval_seconds",
            "auto_resume_max",
            "auto_retry_max",
        ]
        assert rt.view("auto_retry_max").source is SettingSource.ENV
        assert rt.view("auto_resume_max").source is SettingSource.DEFAULT
        assert rt.view("health_poll_interval_seconds").value == 300
        with pytest.raises(UnknownSettingError):
            rt.set("api_key", "x")

    def test_set_persists_and_reset_restores(self, tmp_path: Path) -> None:
        cfg, rt, path = _runtime(tmp_path)
        rt.set("health_poll_interval_seconds", "120")
        assert cfg.health_poll_interval_seconds == 120
        assert json.loads(path.read_text()) == {"health_poll_interval_seconds": 120}
        rt.reset("health_poll_interval_seconds")
        assert cfg.health_poll_interval_seconds == 300.0

    def test_bounds(self, tmp_path: Path) -> None:
        _, rt, _ = _runtime(tmp_path)
        with pytest.raises(ValueError, match="at most"):
            rt.set("auto_resume_max", 11)


class TestHotPoll:
    async def test_budgets_read_config_on_every_use(self, store, torrent_client, mocker) -> None:
        poller = HealthPoller(store=store, torrent_client=torrent_client, worker=AsyncMock())
        mocker.patch.object(health_poll.config, "auto_resume_max", 7)
        mocker.patch.object(health_poll.config, "auto_retry_max", 1)
        assert poller._auto_resume_max == 7
        assert poller._auto_retry_max == 1
        mocker.patch.object(health_poll.config, "auto_resume_max", 2)
        assert poller._auto_resume_max == 2

    async def test_run_rereads_the_interval_each_sleep(self, store, torrent_client, mocker) -> None:
        poller = HealthPoller(store=store, torrent_client=torrent_client, worker=AsyncMock())
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 1:
                mocker.patch.object(health_poll.config, "health_poll_interval_seconds", 0)
            elif len(sleeps) == 2:
                mocker.patch.object(health_poll.config, "health_poll_interval_seconds", 90)
            elif len(sleeps) >= 3:
                raise asyncio.CancelledError

        mocker.patch.object(health_poll.config, "health_poll_interval_seconds", 300)
        mocker.patch.object(health_poll.asyncio, "sleep", fake_sleep)
        tick = mocker.patch.object(poller, "tick", AsyncMock())
        with pytest.raises(asyncio.CancelledError):
            await poller.run()
        assert sleeps == [300.0, health_poll.PAUSED_POLL_RECHECK_SECONDS, 90.0]
        assert tick.await_count == 1


def _view(key: str, value, service_default=10) -> dict:
    return {
        "key": key,
        "value": value,
        "default": service_default,
        "source": "override",
        "type": "int",
        "description": "d",
        "applies": "next search",
        "min": 0,
        "max": 1000,
    }


class TestGateway:
    @pytest.fixture(autouse=True)
    def _isolated_runtime(self, tmp_path: Path, mocker) -> None:
        _, rt, _ = _runtime(tmp_path)
        mocker.patch.object(settings_router, "runtime_settings", rt)

    def test_list_aggregates_both_services(self, app_client: TestClient, torrent_client: AsyncMock):
        torrent_client.settings.return_value = {
            "status": "success",
            "settings": [_view("minimum_seeders", 15)],
        }
        body = app_client.get("/api/v1/settings").json()
        assert body["status"] == "success"
        assert [s["key"] for s in body["services"]["torrent-downloader"]] == ["minimum_seeders"]
        assert "auto_retry_max" in [s["key"] for s in body["services"]["medialab-orchestrator"]]

    def test_put_local_and_relayed(self, app_client: TestClient, torrent_client: AsyncMock):
        resp = app_client.put(
            "/api/v1/settings/medialab-orchestrator/auto_retry_max", json={"value": 4}
        )
        assert resp.status_code == 200
        assert resp.json()["value"] == 4 and resp.json()["source"] == "override"

        torrent_client.set_setting.return_value = _view("minimum_seeders", 3)
        resp = app_client.put(
            "/api/v1/settings/torrent-downloader/minimum_seeders", json={"value": 3}
        )
        assert resp.status_code == 200
        torrent_client.set_setting.assert_awaited_once_with("minimum_seeders", 3)

    def test_relayed_errors_keep_their_status(
        self, app_client: TestClient, torrent_client: AsyncMock
    ):
        torrent_client.set_setting.return_value = {
            "status": "error",
            "code": "INVALID_INPUT",
            "detail": "minimum_seeders must be at most 1000.",
        }
        resp = app_client.put(
            "/api/v1/settings/torrent-downloader/minimum_seeders", json={"value": 5000}
        )
        assert resp.status_code == 422
        torrent_client.reset_setting.return_value = {
            "status": "error",
            "code": "INVALID_INPUT",
            "detail": "Unknown setting: nope",
        }
        assert app_client.delete("/api/v1/settings/torrent-downloader/nope").status_code == 404

    def test_delete_local_and_errors(self, app_client: TestClient):
        app_client.put("/api/v1/settings/medialab-orchestrator/auto_retry_max", json={"value": 4})
        resp = app_client.delete("/api/v1/settings/medialab-orchestrator/auto_retry_max")
        assert resp.status_code == 200 and resp.json()["source"] == "default"
        assert (
            app_client.put(
                "/api/v1/settings/medialab-orchestrator/auto_retry_max", json={"value": 99}
            ).status_code
            == 422
        )
        assert (
            app_client.put(
                "/api/v1/settings/medialab-orchestrator/api_key", json={"value": "x"}
            ).status_code
            == 404
        )
        assert (
            app_client.put("/api/v1/settings/medialab-jellyfin/x", json={"value": 1}).status_code
            == 404
        )

    def test_requires_api_key(self, unauthed_client: TestClient):
        assert unauthed_client.get("/api/v1/settings").status_code == 403
