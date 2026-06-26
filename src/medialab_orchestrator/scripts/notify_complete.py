"""qBittorrent completion relay.

qBittorrent's "Run external program on torrent completion" execs this with the
torrent hash and name. It cannot make an HTTP call itself, so this dumb relay
turns those args into one POST to the orchestrator webhook. No business logic
lives here - all real work stays in the service, pytest-testable.

Configure qBittorrent's completion command as:
    python -m medialab_orchestrator.scripts.notify_complete "%I" "%N"
(%I = info-hash, %N = torrent name). Reads its own env, separate from the
service container:
    ORCHESTRATOR_URL  - base URL of the orchestrator (e.g. http://localhost:8000)
    ORCHESTRATOR_API_KEY - the gateway X-API-Key
"""

from __future__ import annotations

import os
import sys

import httpx

_WEBHOOK_PATH = "/api/v1/webhooks/torrent-complete"
_TIMEOUT_SECONDS = 10.0
_EXPECTED_ARGS = 2


def main(argv: list[str] | None = None) -> int:
    args = (argv if argv is not None else sys.argv[1:])[:_EXPECTED_ARGS]
    if len(args) != _EXPECTED_ARGS:
        print("usage: notify_complete.py <hash> <name>", file=sys.stderr)
        return 2

    torrent_hash, name = args
    base_url = os.environ.get("ORCHESTRATOR_URL", "").rstrip("/")
    if not base_url:
        print("ORCHESTRATOR_URL is not set", file=sys.stderr)
        return 1

    headers = {}
    api_key = os.environ.get("ORCHESTRATOR_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key

    try:
        response = httpx.post(
            f"{base_url}{_WEBHOOK_PATH}",
            json={"hash": torrent_hash, "name": name},
            headers=headers,
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"webhook POST failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
