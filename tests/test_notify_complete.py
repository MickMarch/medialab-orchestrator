"""Tests for the standalone qBittorrent completion relay.

The relay must run on the host (where qBittorrent lives) with only the standard
library - no third-party imports, no package imports - so it can be dropped in
as a single file and invoked by qBittorrent's completion command.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from medialab_orchestrator.scripts import notify_complete

HASH = "abcdef0123456789abcdef0123456789abcdef01"
NAME = "Show.Name.S01.1080p.GROUP"
URL = "http://localhost:8000"
KEY = "test-key"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_URL", URL)
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", KEY)


def test_uses_only_stdlib() -> None:
    # Guard the core requirement: no third-party dependency the host lacks.
    import inspect

    source = inspect.getsource(notify_complete)
    assert "import httpx" not in source
    assert "import requests" not in source
    assert "urllib" in source


class TestArgs:
    def test_missing_args_returns_2(self):
        assert notify_complete.main([]) == 2
        assert notify_complete.main([HASH]) == 2

    def test_missing_url_returns_1(self, monkeypatch):
        monkeypatch.delenv("ORCHESTRATOR_URL", raising=False)
        assert notify_complete.main([HASH, NAME]) == 1


class TestPost:
    def test_posts_hash_and_name_with_key(self, env, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode())
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            return MagicMock(status=202)

        monkeypatch.setattr(notify_complete.urllib.request, "urlopen", fake_urlopen)

        rc = notify_complete.main([HASH, NAME])

        assert rc == 0
        assert captured["url"] == f"{URL}/api/v1/webhooks/torrent-complete"
        assert captured["body"] == {"hash": HASH, "name": NAME}
        assert captured["headers"]["x-api-key"] == KEY
        assert captured["headers"]["content-type"] == "application/json"

    def test_omits_key_header_when_unset(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRATOR_URL", URL)
        monkeypatch.delenv("ORCHESTRATOR_API_KEY", raising=False)
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            return MagicMock(status=202)

        monkeypatch.setattr(notify_complete.urllib.request, "urlopen", fake_urlopen)

        notify_complete.main([HASH, NAME])
        assert "x-api-key" not in captured["headers"]

    def test_http_error_returns_1(self, env, monkeypatch):
        import urllib.error

        def boom(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(notify_complete.urllib.request, "urlopen", boom)

        assert notify_complete.main([HASH, NAME]) == 1
