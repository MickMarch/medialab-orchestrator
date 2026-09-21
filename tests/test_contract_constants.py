"""The gateway and its downstream clients agree on the shared wire constants."""

from medialab_contracts import API_KEY_HEADER, API_PREFIX, HEALTH_PATH

from medialab_orchestrator.main import app


def test_every_route_is_under_the_shared_prefix() -> None:
    api_paths = list(app.openapi()["paths"])
    assert api_paths
    assert all(p.startswith(API_PREFIX) for p in api_paths)
    assert HEALTH_PATH in api_paths


def test_openapi_security_scheme_uses_the_shared_header() -> None:
    schemes = app.openapi()["components"]["securitySchemes"]
    assert any(s.get("name") == API_KEY_HEADER for s in schemes.values())
