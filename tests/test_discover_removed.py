"""The Jellyfin Discover libraries and in-app requests are gone: no route, no
loop, no module (MickMarch/medialab#168)."""

import dataclasses
import importlib

import pytest
from fastapi.testclient import TestClient

from medialab_orchestrator.core.config import AppConfig
from medialab_orchestrator.core.deps import AppContext

EVENTS_PATH = "/api/v1/jellyfin/events"
WEBHOOK_SECRET_HEADER = "X-Medialab-Webhook-Secret"
REMOVED_MODULES = (
    "medialab_orchestrator.services.discover_reconcile",
    "medialab_orchestrator.services.requests",
    "medialab_orchestrator.store.requests",
    "medialab_orchestrator.routers.jellyfin_events",
)
REMOVED_CONTEXT_FIELDS = {"discover_reconciler", "request_poller"}
REMOVED_CONFIG_FIELDS = {
    "jellyfin_request_users",
    "discover_refresh_seconds",
    "discover_poll_seconds",
    "discover_recommendations_per_title",
    "jellyfin_request_retry_hours",
    "jellyfin_webhook_secret",
    "jellyfin_request_resolution",
}


class TestJellyfinEventsRoute:
    def test_post_is_not_found(self, unauthed_client: TestClient):
        response = unauthed_client.post(EVENTS_PATH, headers={WEBHOOK_SECRET_HEADER: "x"})
        assert response.status_code == 404

    def test_post_with_the_gateway_key_is_not_found(self, app_client: TestClient):
        assert app_client.post(EVENTS_PATH).status_code == 404


class TestNoDiscoverOrRequestLoop:
    def test_context_carries_no_discover_or_request_loop(self):
        fields = {field.name for field in dataclasses.fields(AppContext)}
        assert fields.isdisjoint(REMOVED_CONTEXT_FIELDS)

    @pytest.mark.parametrize("module", REMOVED_MODULES)
    def test_module_is_gone(self, module: str):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)

    def test_config_has_no_discover_or_request_field(self):
        assert set(AppConfig.model_fields).isdisjoint(REMOVED_CONFIG_FIELDS)
