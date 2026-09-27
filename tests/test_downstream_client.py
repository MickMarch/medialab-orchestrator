"""DownstreamClient error relay: chosen downstream codes keep their status and code."""

from medialab_orchestrator.clients.base import relayed_error
from medialab_orchestrator.core.errors import ErrorCode

RELAYED = frozenset({ErrorCode.TMDB_UNAVAILABLE})
TMDB_BODY = {"status": "error", "code": "TMDB_UNAVAILABLE", "detail": "TMDB is unavailable."}


def test_relays_a_listed_code_with_the_downstream_status():
    error = relayed_error(503, TMDB_BODY, RELAYED)
    assert error is not None
    assert error.status_code == 503
    assert error.code is ErrorCode.TMDB_UNAVAILABLE
    assert error.detail == "TMDB is unavailable."


def test_ignores_an_unlisted_code():
    body = {"status": "error", "code": "INTERNAL_ERROR", "detail": "x"}
    assert relayed_error(500, body, RELAYED) is None


def test_ignores_an_unknown_code():
    body = {"status": "error", "code": "SOMETHING_NEW", "detail": "x"}
    assert relayed_error(500, body, RELAYED) is None


def test_ignores_a_non_error_body():
    assert relayed_error(503, "Service Unavailable", RELAYED) is None
    assert relayed_error(503, None, RELAYED) is None


def test_nothing_relayed_by_default():
    assert relayed_error(503, TMDB_BODY, frozenset()) is None
