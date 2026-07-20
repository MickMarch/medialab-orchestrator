# CLAUDE.md - medialab-orchestrator

Front-door orchestrating gateway for the medialab media lifecycle. Independent
git repo inside the `medialab/` workspace, pinned there as a submodule like the
other services.

> **Source of truth is the workspace root.** The root repo at
> `medialab/` (`medialab/CLAUDE.md` + `medialab/medialab-orchestrator-spec.md`,
> the frozen design draft) governs this service's design, roadmap order, and
> shared conventions. When this file and the root disagree, the root wins, and
> a session started inside this directory should read the root CLAUDE.md and the
> spec before making design decisions.

---

## What this service is

The Discord bot talks to exactly one service - this one. The orchestrator
brokers the entire media lifecycle and fans out to downstream worker services
(torrent-downloader, medialab-jellyfin), which are never client-facing.

```
medialab-bot ──> medialab-orchestrator ──┬──> torrent-downloader ──> qBittorrent + TMDB
 (one dependency)        │ (the gateway)  └──> medialab-jellyfin   ──> Jellyfin
                         └── owns the job lifecycle (SQLite pipeline_job)
```

A SQLite `pipeline_job` table is the spine: one row per torrent, advanced one
state at a time by an in-process asyncio worker, persisted after each
transition, so a restart resumes from the last committed state. This is the
difference between an orchestrator and a relay.

## Commands

```bash
uv sync --dev
uv run medialab-orchestrator       # production
uv run medialab-orchestrator-dev   # dev, hot-reload
uv run pytest
uv run ruff check src tests
uv run mypy src
```

## Stack / conventions

Same stack as the other services: FastAPI, `uv`, `hatchling` + `hatch-vcs`
(version from git tags, never hardcoded - `src/medialab_orchestrator/_version.py`
is generated and gitignored), `pydantic-settings`. Same conventions: `X-API-Key`
auth, `{"status": "error", "code": ..., "detail": ...}` error shape, `slowapi`
rate limiting, `X-Request-ID` request logging, health-check reachability.

Deps unique here: stdlib `sqlite3` (job store), `PTN` / `parse-torrent-title`
(season number ONLY - title/year come from TMDB), `httpx` (downstream fan-out),
`medialab-contracts` (shared models, pinned `v0.3.0` as a uv git dependency).

Full workspace engineering standards apply from commit one (root CLAUDE.md
"Engineering standards"): ruff lint+format (`E,F,I,UP,B,SIM,PLR2004`, UP042
ignored to keep `(str, Enum)`), mypy + pydantic plugin gate, pre-commit, CI
running lint -> format-check -> mypy -> tests, Keep-a-Changelog, dependabot +
pip-audit. CI checks out with `fetch-depth: 0` so hatch-vcs resolves the tag.

## Module layout

```
src/medialab_orchestrator/
├── core/config.py   - single AppConfig pydantic-settings instance (config); imported everywhere
├── core/errors.py   - ErrorCode (CommonErrorCode superset + service codes), AppException
└── store/jobs.py    - JobStatus enum, PipelineJob model, JobStore (SQLite job store)
```

`store/jobs.py` is the implemented spine. Bot-facing routers, the asyncio
worker, the webhook, the relay script, and the TV-rename logic are the
remaining MVP work (see "Build status" below).

## Job lifecycle (SQLite `pipeline_job`)

```
DOWNLOAD_SUBMITTED   POST /download accepted, forwarded to torrent-downloader
DOWNLOADING          qBittorrent working (read-through from /transfers, not polled - see decisions)
STOP_SEEDING         webhook received -> torrent-downloader POST /transfers/stop-seeding
RESOLVE_META         GET /transfers/{hash}/info (media_type, host_path, tmdb_id);
                     GET /search/tmdb/{type}/{tmdb_id} -> canonical title + year
RENAME               TV: PTN(name)->season; move to host_path/Title (Year)/Season NN/
                     movie: no move, dest = host_path/<release_name>
SCAN                 medialab-jellyfin POST /library/scan (Media/Updated)
DONE
FAILED               any step error; last_error stored; retryable from last good state
```

Columns: `id` (surrogate uuid PK), `torrent_hash` (nullable, unique when
present, stored lowercase - backfilled once qBittorrent knows it), `seq`
(autoincrement rowid for stable newest-first ordering), `release_name`,
`media_type`, `tmdb_id`, `resolved_title`, `resolved_year`, `source_path`,
`dest_path`, `status`, `last_error`, `attempts`, `created_at`, `updated_at`.
Job is born at download submit, never at search (no draft jobs). The hash is
not known up front for a `.torrent`-URL source, so identity is the surrogate
`id`; the hash is stamped from the downloader's `POST /download` response
(`torrent_hash`) or backfilled by the completion webhook (`%I`). `update_job`
keys by `id`; the webhook resolves by hash then updates by the found job's id.

### Idempotency (required for safe retry)
STOP_SEEDING: stopping an already-stopped torrent is a no-op. RESOLVE_META: pure
reads. RENAME: skip the move if `dest_path` is populated and exists. SCAN:
Jellyfin `Media/Updated` is safe to repeat.

**No per-download REGISTER step.** The Jellyfin library root (`F:\Media\Movies`,
`F:\Media\Shows`) is registered ONCE at setup, not per download - Jellyfin
recursively scans an already-registered root, and adding a sub-path of an
existing root 404s. So the pipeline goes RENAME -> SCAN directly; SCAN notifies
Jellyfin the new path changed. `JellyfinClient.register_path` /
medialab-jellyfin `POST /library/paths` still exist for one-time setup use (the
setup wizard, item 8), just not in the per-download pipeline. (Removed the
REGISTER step + `JobStatus.REGISTER` after a live run 404'd on it, 2026-07-20.)

## Planned endpoints (the gateway surface)

All under `/api/v1`, all require the gateway `X-API-Key` except `/health`.

**Search (stateless proxies to torrent-downloader, no job created):**
- `GET /search/tmdb`, `GET /search/tmdb/{movie|show}/{tmdb_id}`,
  `GET /search/torrents` (`media_type` required; shows accept optional
  `season`/`episode`, validated via `TorrentSearchScope` and proxied through).
  Sole accepted exception to "every gateway endpoint binds a job" - value is one
  bot-facing surface.

**Download (creates a job):**
- `POST /download` - body `{source_url, media_type, tmdb_id}` (`source_url` is a
  magnet or an http `.torrent` URL). Inserts
  `pipeline_job(status=DOWNLOAD_SUBMITTED)` keyed by a surrogate uuid, forwards
  to torrent-downloader `POST /download` (which caches
  `{media_type, host_path, tmdb_id}` vs hash and returns the resolved
  `torrent_hash`), stamps that hash onto the job, returns the job.

**Status / observability:**
- `GET /transfers` - merges torrent-downloader live `/transfers` with job rows.
- `GET /jobs` (filter by `status`), `GET /jobs/{id}`,
  `POST /jobs/{id}/retry` (re-enter worker from last good state; 409 if the job
  has no stamped hash yet).
- `GET /storage` - forwards to torrent-downloader.

**Webhook (post-download entry):**
- `POST /webhooks/torrent-complete` - body `{hash, name}`, called by
  `scripts/notify_complete.py` (qBittorrent completion hook child process).
  Finds the job by hash, advances it into the post-download pipeline, returns
  `202` immediately (never blocks qBittorrent). If no job matches the hash,
  inserts one so the event is still tracked. The relay is **standalone +
  stdlib-only** (only `urllib`, no third-party or package imports), so it runs
  on the host next to qBittorrent today and unchanged inside the qBittorrent
  container later (item 20 = re-point one qB setting). Wiring instructions +
  the Windows Defender write-lock note are in the README.

**Health:**
- `GET /api/v1/health` - public, no auth. Reports reachability of both
  downstream services. The bot's single cross-service health signal.

## Implementation decisions (resolved from the spec's open questions, 2026-06-26)

The spec left three questions for the implementation phase. Resolved:

1. **Webhook auth: keyed.** `POST /webhooks/torrent-complete` requires the
   gateway `X-API-Key` like the rest of `/api/v1`. In the compose network
   localhost is not a trust boundary, so uniform auth over a special-cased
   open endpoint. `notify_complete.py` sends the key from its own env.
2. **DOWNLOADING via read-through, not polling.** The gateway does NOT actively
   poll torrent-downloader `/transfers` to advance DOWNLOAD_SUBMITTED ->
   DOWNLOADING. `GET /transfers` is a live read-through (merge job rows with a
   one-shot downstream read on request); the completion webhook is the event
   that moves a job into the post-download pipeline. Less machinery, no poll loop.
3. **PTN season parsing validated at build.** The season-number parser is
   verified against real release-name samples while writing it. No season
   parseable -> mark FAILED with a clear `last_error`; operator fixes the folder
   and calls `retry`. No silent half-processing.

## Other core decisions (from the spec)

- **TMDB id threaded, no title guessing.** Bot knows the id (user picked the
  search result); it flows bot -> orchestrator -> torrent-downloader (cached vs
  hash) -> back at completion. Canonical `Title (Year)` from TMDB. PTN is
  season-number only.
- **One TMDB key owner.** Title/year resolved via torrent-downloader's
  `/search/tmdb/...`, not TMDB directly. torrent-downloader stays sole TMDB-key
  holder.
- **Shared media volume, not host shell-out.** Host media dir bind-mounted into
  this container (`MEDIA_MOUNT_PATH`); file moves go through the mount.
- **SQLite over Postgres, in-process asyncio worker over Celery/Redis** - the
  lightest durable/queryable store and the lightest worker that fit single-host
  homelab scale. Deliberate restraint; the scale-up path is documented, not built.

## Config (`core/config.py` / `.env`)

`API_KEY` (gateway's own key), `TORRENT_DOWNLOADER_URL` +
`TORRENT_DOWNLOADER_API_KEY`, `MEDIALAB_JELLYFIN_URL` +
`MEDIALAB_JELLYFIN_API_KEY`, `MEDIA_MOUNT_PATH` (in-container mount, default
`/media`), `DB_PATH` (SQLite file, default `./data/orchestrator.db`). Every
field is optional at import time (CI has no `.env`); real values required at
runtime. `scripts/notify_complete.py` reads its own minimal env
(`ORCHESTRATOR_URL` + key) as a qBittorrent child process.

## Testing patterns

- `uv run pytest` always, never `python -m pytest`. pytest style only.
- CI isolation: tests pass with no `.env` and no network. The `store` fixture
  (`tests/conftest.py`) gives a fresh in-memory `JobStore(db_path=":memory:")`
  per test - never a real DB file. Downstream HTTP (torrent-downloader,
  medialab-jellyfin) is mocked at the client boundary. The webhook is exercised
  by posting to the endpoint directly, no live qBittorrent.
- Live-credential / live-service tests are marked `@pytest.mark.integration`
  and skipped in CI (marker registered in `pyproject.toml`).

## Out of scope for MVP (deferred)

- Jellyfin host power-on / wake (WoL / smart-plug) - assume host awake.
- Multi-host shared storage for the media mount - single-host bind mount now.
- `/trending`, `/similar` gateway endpoints - add as passthroughs when
  torrent-downloader's TMDB roadmap ships them.

## Build status (2026-06-26)

Roadmap item 6 (root CLAUDE.md). In progress on branch `feat/orchestrator-mvp`
(root repo). Done: scaffold, standards from commit one, `core/config.py`,
`core/errors.py`, the SQLite job store (`store/jobs.py`) + 16 passing store
tests. Remaining MVP: asyncio worker (FastAPI lifespan), bot-facing routers,
webhook + `scripts/notify_complete.py` relay, downstream `httpx` clients, TV
rename, CI workflow, pre-commit, CHANGELOG, root `docker-compose.yml`, submodule
wiring. The repo is local-only until pushed to GitHub (needs a public repo for
the contracts git-ref dependency to resolve in CI / Docker builds).

## Versioning

Version from git tags via `hatch-vcs` - never hardcoded. Release: merge to main,
tag (`git tag -a vX.Y.Z -m "vX.Y.Z"`), push tag, GitHub Release, update
`CHANGELOG.md` before tagging.
