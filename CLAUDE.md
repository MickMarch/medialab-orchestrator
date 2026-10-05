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
`ORCHESTRATOR_API_KEY`) because it runs as a qBittorrent child process. It is
deprecated: the compose layout posts the webhook with a `curl` autorun command
from inside the qBittorrent container (README, "Wiring the completion hook").

## Job lifecycle (`pipeline_job`)

```
DOWNLOAD_SUBMITTED   POST /download accepted, forwarded to torrent-downloader
DOWNLOADING          set once qBittorrent is actively fetching, by the health poll or a
                     /jobs read (one shared rule, services/progress.py)
STOP_SEEDING         webhook or poll -> record the on-disk root (content_path basename) if the
                     webhook did not carry it, then torrent-downloader DELETE /transfers/{hash}
RESOLVE_META         GET /search/tmdb/{type}/{tmdb_id} -> canonical title + year
RENAME               from <media>/_incoming/<subdir>/<root name> (legacy: the library root)
                     per video file: show -> <root>/Title (Year)/Season NN/Title SNNEMM.ext
                     movie -> <root>/Title (Year)/Title (Year).ext (+ extras/); subs follow;
                     a show video with no SxxEyy in its name takes it from the nearest parent
                     folder under the download root, then the release name (one video per
                     code, logged); still none -> <root>/Title (Year)/extras/ (logged, in
                     placed_paths); a pack where no video parses fails EPISODE_UNPARSEABLE
SCAN                 medialab-jellyfin POST /library/scan
DONE                 removes the job's (media_type, tmdb_id) from the watchlist when it is only
                     saved (a followed show stays); orphans skip it
FAILED               any step error; last_error stored; POST /jobs/{id}/retry re-enters
                     from the last good state; the health poll retries it AUTO_RETRY_MAX times
NEEDS_ATTENTION      the poll's budget for a job is spent; a human retry, redo or dismiss moves it
DELETED              undone via DELETE /jobs/{id} (marks the job's follow_submission ignored), or
                     replaced via POST /jobs/{id}/redo (repoints the submission; from DONE, or from
                     NEEDS_ATTENTION with nothing placed: replacement row created first with
                     redo_of, then the old job's deletion plan runs, then the new download is
                     submitted); terminal, kept for the record
DISMISSED            POST /jobs/{id}/dismiss from FAILED or NEEDS_ATTENTION: a human judged the job
                     not worth pursuing; last_error and files kept, follow_submission ignored;
                     terminal, out of the poll and the needs_attention count
```

Columns: `id` (surrogate uuid PK), `torrent_hash` (nullable, unique when
present, lowercase; stamped from the downloader's `POST /download` response
or backfilled by the webhook `%I`), `seq` (rowid, newest-first ordering),
`release_name`, `media_type`, `tmdb_id`, `season` and `episode` (nullable search
scope; both null is the whole title), `resolved_title`, `resolved_year`,
`source_path` (the on-disk root name from qBittorrent's content path; the display
`release_name` is not it), `dest_path`, `status`, `last_error`, `attempts`,
`remediations`, `seeding_removed_at`, `placed_paths`, `deleted_at`, `redo_of`
(nullable; the id of the job this one replaces, its inverse `redone_by` is
computed on read from one query per listing), `dismissed_at`, `created_at`,
`updated_at`. `attention_cause` on the view is derived from `last_error` by
`services/attention.py`, which owns the message shapes the poll and the worker
write. A
job is born at download submit, never at search. The webhook
resolves by hash, then updates by id; an unmatched hash orphan-inserts a job so
the event is still tracked. A `DELETED` job releases its hash into
`deleted_hash` (store-level, on the status update), so the same torrent can be
downloaded again and a late webhook never advances a deleted job.
`stamp_hash` raises `HashInUseError` when a live job owns the hash; the submit
path turns that into a `FAILED` job pointing at the owner.

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
- **Follow poll beside the health poll.** `services/follow.py` is a second
  loop with the same shape (interval re-read from config every sleep, `tick`
  never raises, one show raising is logged and skipped). `wanted_episodes`
  is pure and shared by the poll and `GET /watchlist/show/{id}/episodes`;
  the pick rule lives in torrent-downloader; submission goes through
  `services/download.py` so a follow job is an ordinary job. A downloader
  error stops one show for the tick; `mark_checked` always runs. Spec:
  `docs/specs/watchlist.md`.
- **Discord webhook, not the bot.** `services/notify.py` is the one httpx
  use outside `clients/`: a webhook has no key or error envelope, so the
  downstream client base does not fit. It never raises; no URL, no notice.
- **Search proxies create no job.** `GET /search/*` and `GET /discover/*`
  are stateless passthroughs, the accepted exception to "every gateway
  endpoint binds a job". The watchlist is user state, not a job.
- **Relayed downstream codes.** A downstream error becomes `502`
  `DOWNSTREAM_UNAVAILABLE` unless the client call lists its code in `relay`
  (`clients/base.py`); discover relays `TMDB_UNAVAILABLE` (503) and
  `INVALID_INPUT` (422) as-is.
- **SQLite + in-process asyncio worker.** Lightest durable store and worker
  for single-host scale; the scale-up path is documented, not built.

## Module layout

```
src/medialab_orchestrator/
├── core/        config, auth, deps, limiter, middleware, logger, errors,
│                settings (declared runtime tunables, JSON override store, applied onto config)
├── clients/     base (httpx + X-API-Key), torrent_downloader, jellyfin
├── store/       jobs (JobStatus, PipelineJob, JobStore over sqlite3),
│                watchlist (WatchlistStore, same DB file: watchlist_item, renamed from
│                wishlist_item at startup, plus follow_submission keyed by show, season, episode,
│                and follow_season: the per-season pack mode of a follow)
├── services/    worker (asyncio pipeline), health_poll (periodic remediation), deletion (undo a
│                download: plan + execute), download (the submit path shared by
│                POST /download, redo and the follow poll), redo (replace a DONE or
│                flagged-and-unplaced job; redone_by on read), dismiss (close a flagged job),
│                attention (error message shapes + attention_cause), metadata (TMDB resolve),
│                rename (pure plan_rename to Jellyfin layout + apply_plan mover),
│                discover (watchlist kind + best-effort library annotation),
│                shows (episodes joined with library presence and queued jobs),
│                follow (wanted_episodes + FollowPoller: pick and auto-submit per follow),
│                notify (Discord webhook notice; the one httpx use outside clients/)
├── routers/     system (/health), search (proxies), discover (annotated proxies), shows, watchlist
│                (saved titles, follow / unfollow / pause / resume / check, the episode view
│                with submitted + wanted, Retry),
│                settings (suite settings, local + relayed),
│                gateway (download/transfers/jobs/storage),
│                webhooks (torrent-complete)
├── schemas/     jobs, errors, watchlist (FollowCheckResponse)
├── scripts/     notify_complete.py (standalone relay)
└── main.py      app, lifespan (health and follow pollers), middleware, exception handlers,
                 routers under /api/v1
```

## Testing patterns

- `store` / `watchlist` fixtures (`tests/conftest.py`): fresh in-memory
  `JobStore` / `WatchlistStore(db_path=":memory:")` per test. Never a real DB
  file, except the schema and migration tests, which use `tmp_path`.
- Downstream HTTP mocked at the client-class boundary. The webhook is
  exercised by posting to the endpoint. No live qBittorrent or Jellyfin.
- Season parsing is validated against real release-name samples; no episode
  parseable in a whole pack -> `FAILED` with a clear `last_error`; a single
  unparseable video goes to `extras/` and is logged, never silently skipped.
