"""qBittorrent completion relay (standalone, stdlib-only).

qBittorrent's "Run external program on torrent completion" execs this with the
torrent hash and name. It cannot make an HTTP call itself, so this dumb relay
turns those args into one POST to the orchestrator webhook. No business logic
lives here - all real work stays in the service.

This file is deliberately standalone: it imports only the Python standard
library and nothing from the medialab_orchestrator package, so it runs anywhere
Python is present - the host next to qBittorrent, or inside a container - with
no install step. Drop it anywhere and point qBittorrent at it directly.

Configure qBittorrent's completion command as either:
    python -m medialab_orchestrator.scripts.notify_complete "%I" "%N" "%F"
    python C:\\path\\to\\notify_complete.py "%I" "%N" "%F"
(%I = info-hash, %N = torrent name, %F = content path: the root file or folder
on disk, which the display name is not). %F is optional for older hook
commands; the orchestrator then reads it from the transfer list instead. Reads
its own env, separate from the
service container:
    ORCHESTRATOR_URL     - base URL of the orchestrator (e.g. http://localhost:8000)
    ORCHESTRATOR_API_KEY - the gateway X-API-Key
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

_WEBHOOK_PATH = "/api/v1/webhooks/torrent-complete"
_TIMEOUT_SECONDS = 10.0
_MIN_ARGS = 2
_MAX_ARGS = 3


def main(argv: list[str] | None = None) -> int:
    args = (argv if argv is not None else sys.argv[1:])[:_MAX_ARGS]
    if len(args) < _MIN_ARGS:
        print("usage: notify_complete.py <hash> <name> [<content_path>]", file=sys.stderr)
        return 2

    torrent_hash, name = args[0], args[1]
    content_path = args[2] if len(args) > _MIN_ARGS else ""
    base_url = os.environ.get("ORCHESTRATOR_URL", "").rstrip("/")
    if not base_url:
        print("ORCHESTRATOR_URL is not set", file=sys.stderr)
        return 1

    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("ORCHESTRATOR_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key

    payload = {"hash": torrent_hash, "name": name}
    if content_path:
        payload["content_path"] = content_path
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{base_url}{_WEBHOOK_PATH}", data=body, headers=headers, method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS):
            pass
    except urllib.error.URLError as exc:
        print(f"webhook POST failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
