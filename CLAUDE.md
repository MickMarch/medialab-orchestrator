# CLAUDE.md - medialab-orchestrator

Workspace rules, conventions, standards and workflow live in the root
[`medialab/CLAUDE.md`](../CLAUDE.md); it is the authority when anything here
disagrees. This file holds only what is specific to this code.

## What this service is

The front-door gateway. The Discord bot talks to exactly one service, this
one; it brokers the whole media lifecycle and fans out to torrent-downloader
and medialab-jellyfin, which are never client-facing. A SQLite `pipeline_job`
table is the spine: one row per torrent, advanced one state at a time by an
in-process asyncio worker, persisted after each transition, so a restart
resumes from the last committed state. Endpoint table and the completion-hook
setup: [README](README.md).

## Commands

```bash
uv sync --dev
uv run medialab-orchestrator       # production
uv run medialab-orchestrator-dev   # dev, hot-reload
uv run pytest
```

## Config

`.env.example` is the authoritative variable list; `core/config.py` holds the
defaults. Every field is optional at import time and required at runtime.
`MEDIA_MOUNT_PATH` is the in-container mount of the host media dir; file moves
go through it (shared volume, never a host shell-out).
`scripts/notify_complete.py` reads its own minimal env (`ORCHESTRATOR_URL`,
`ORCHESTRATOR_API_KEY`) because it runs as a qBittorrent child process.

## Job lifecycle (`pipeline_job`)

```
DOWNLOAD_SUBMITTED   POST /download accepted, forwarded to torrent-downloader
DOWNLOADING          qBittorrent working (read-through from /transfers on request)
STOP_SEEDING         webhook or poll -> record the on-disk root (content_path basename) if the
                     webhook did not carry it, then torrent-downloader DELETE /transfers/{hash}
RESOLVE_META         GET /search/tmdb/{type}/{tmdb_id} -> canonical title + year
RENAME               per video file: show -> <root>/Title (Year)/Season NN/Title SNNEMM.ext
                     movie -> <root>/Title (Year)/Title (Year).ext (+ extras/); subs follow
SCAN                 medialab-jellyfin POST /library/scan
DONE
FAILED               any step error; last_error stored; POST /jobs/{id}/retry re-enters
                     from the last good state; the health poll retries it AUTO_RETRY_MAX times
NEEDS_ATTENTION      the poll's budget for a job is spent; only a human retry moves it
```

Columns: `id` (surrogate uuid PK), `torrent_hash` (nullable, unique when
present, lowercase; stamped from the downloader's `POST /download` response
or backfilled by the webhook `%I`), `seq` (rowid, newest-first ordering),
`release_name`, `media_type`, `tmdb_id`, `resolved_title`, `resolved_year`,
`source_path` (the on-disk root name from qBittorrent's content path; the display
`release_name` is not it), `dest_path`, `status`, `last_error`, `attempts`,
`remediations`, `seeding_removed_at`, `created_at`, `updated_at`. A job is born at download submit, never at search. The webhook
resolves by hash, then updates by id; an unmatched hash orphan-inserts a job so
the event is still tracked.

**Idempotency (required for safe retry):** STOP_SEEDING treats an
already-removed torrent (404) as done. RESOLVE_META is pure reads. RENAME skips every
file whose destination exists or whose source is gone, so a partial run
finishes on retry. SCAN (`Media/Updated`) is safe to repeat.

**No per-download REGISTER step.** Library roots are registered once at setup;
Jellyfin recursively scans them and 404s on a sub-path of a registered root.
`JellyfinClient.register_path` exists for setup use only. See
`docs/decisions/0005-register-once.md` in the workspace.

## Decisions that shape the code

- **Keyed webhook.** `POST /webhooks/torrent-complete` requires the gateway
  `X-API-Key` like everything else; localhost inside the compose network is
  not a trust boundary. Returns `202` immediately, never blocks qBittorrent.
- **Standalone relay.** `scripts/notify_complete.py` is stdlib-only (`urllib`,
  no package imports) so the same file runs on the host today and inside a
  qBittorrent container later.
- **TMDB id threaded, no title guessing.** The bot knows the id; it flows
  bot -> gateway -> downloader (cached vs hash) -> back at completion.
  Canonical `Title (Year)` comes from TMDB via torrent-downloader (the sole
  TMDB-key holder). PTN parses season and episode per file, nothing else.
- **Webhook plus poll.** The completion webhook is the fast path; the health
  poll (`services/health_poll.py`, every `HEALTH_POLL_INTERVAL_SECONDS`) is the
  safety net: resume errored downloads, run the pipeline for missed
  completions, retry FAILED jobs, flag `NEEDS_ATTENTION` past the budgets.
  `docs/decisions/0003-webhook-plus-poll.md`.
- **Search proxies create no job.** `GET /search/*` are stateless
  passthroughs, the one accepted exception to "every gateway endpoint binds a
  job".
- **SQLite + in-process asyncio worker.** Lightest durable store and worker
  for single-host scale; the scale-up path is documented, not built.

## Module layout

```
src/medialab_orchestrator/
├── core/        config, auth, deps, limiter, middleware, logger, errors
├── clients/     base (httpx + X-API-Key), torrent_downloader, jellyfin
├── store/       jobs (JobStatus, PipelineJob, JobStore over sqlite3)
├── services/    worker (asyncio pipeline), health_poll (periodic remediation), metadata (TMDB resolve),
│                rename (pure plan_rename to Jellyfin layout + apply_plan mover)
├── routers/     system (/health), search (proxies), gateway (download/transfers/jobs/storage),
│                webhooks (torrent-complete)
├── schemas/     jobs, errors
├── scripts/     notify_complete.py (standalone relay)
└── main.py      app, lifespan (worker), middleware, exception handlers, routers under /api/v1
```

## Testing patterns

- `store` fixture (`tests/conftest.py`): fresh in-memory
  `JobStore(db_path=":memory:")` per test. Never a real DB file.
- Downstream HTTP mocked at the client-class boundary. The webhook is
  exercised by posting to the endpoint. No live qBittorrent or Jellyfin.
- Season parsing is validated against real release-name samples; no season
  parseable -> `FAILED` with a clear `last_error`, never silent half-processing.
