---
name: build-state
description: orchestrator MVP shipped v0.1.0; only the medialab-bot gateway rewrite remains under roadmap item 6
metadata:
  type: project
---

**MVP COMPLETE - released v0.1.0 (2026-06-26).** Public repo
(github.com/MickMarch/medialab-orchestrator), branch-protected on the `quality`
CI check (matching the other services), submodule pinned in the root. CI green
on main (ruff, format-check, mypy, pytest, pip-audit). Root PR #3 landed the
submodule pin + compose + README + roadmap mark.

**Shipped:** full engineering standards from commit one; `core/` (config, errors,
auth, limiter, middleware, logger, deps/AppContext + lifespan); the SQLite job
store (`store/jobs.py`); async downstream clients (`clients/` - base with
`DOWNSTREAM_UNAVAILABLE` mapping + reachability probe, torrent-downloader,
jellyfin); the forward-retry asyncio worker (`services/worker.py`); the
TV-rename service (`services/rename.py`, PTN season-only); the gateway routers
(search proxies, `POST /download`, `GET /transfers` read-through merge,
`GET/POST /jobs*`, `GET /storage`, public aggregated health); the keyed
`POST /webhooks/torrent-complete` + `scripts/notify_complete.py` relay;
Dockerfile; 48 tests green. Consumes `medialab-contracts` v0.2.0.

**Still outstanding under roadmap item 6 (the only remaining piece):** the
**medialab-bot rewrite onto the gateway** - point every bot call at the
orchestrator, drop torrent-downloader + jellyfin URLs/keys, save-path config,
and the direct health check. The gateway it targets now exists. That work lives
in the medialab-bot repo, not here.

**Known follow-ups (not blocking):** the stale `_version.py` regenerates from
the v0.1.0 tag on next build; verify the stack against live services before
relying on it (the v0.1.0 tag was cut from green CI, not a live run).

**How to apply:** This service is done - design lives in `CLAUDE.md`. See
[[impl-decisions]], [[source-of-truth]]. Next orchestrator-adjacent work is in
medialab-bot.
