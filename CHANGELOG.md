# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0] - 2026-09-25

### Fixed

- RENAME renames from qBittorrent's real on-disk root (`content_path`, via
  the relay's new `%F` argument or the transfer list at STOP_SEEDING) instead
  of the display name, which differs from the folder for most releases. A
  missing download folder now fails the job with `SOURCE_NOT_FOUND` instead of
  reporting DONE having moved nothing; a retry after a completed move still
  passes. `last_error` is cleared when a job reaches DONE. RESOLVE_META no
  longer calls the downloader's per-hash info endpoint.

## [0.8.1] - 2026-09-25

### Fixed

- RESOLVE_META no longer fails when torrent-downloader has no cached entry
  for the hash (its cache is wiped by an image rebuild); the job's own
  `media_type` and `tmdb_id` are what the pipeline needs, `source_path` is
  left empty. Seen on the first health-poll tick: five recovered jobs 404'd.

## [0.8.0] - 2026-09-25

### Added

- Health poll (`HEALTH_POLL_INTERVAL_SECONDS`, default 300): resumes errored
  downloads up to `AUTO_RESUME_MAX` times, runs the pipeline for completions
  the webhook missed, retries `FAILED` jobs up to `AUTO_RETRY_MAX` attempts,
  and parks anything past its budget in the new `NEEDS_ATTENTION` status with
  the reason in `last_error`. `POST /jobs/{id}/retry` accepts that status and
  resets both budgets. `GET /health` reports `needs_attention`.
- Job columns `remediations` and `seeding_removed_at`, added to an existing
  database at startup.

### Changed

- STOP_SEEDING removes the job's torrent from qBittorrent (files kept) via
  `DELETE /transfers/{hash}` instead of pausing every seeding torrent, so a
  finished download never errors in qBittorrent after RENAME empties its
  folder. Requires torrent-downloader with the per-hash endpoints.

## [0.7.0] - 2026-09-25

### Changed

- RENAME places every download to Jellyfin's documented layout: movies as
  `Title (Year)/Title (Year).ext` with other videos under `extras/`; shows as
  `Title (Year)/Season NN/Title SNNEMM.ext` per episode file, `Season 00` for
  specials, `SNNEMM-EMM` for multi-episode files, nested multi-season packs
  placed by each file's own season. Subtitles follow their video. Non-media
  files stay behind; the download folder is removed once it holds no video.
  `dest_path` is now the folder Jellyfin scans (series or movie folder).
- `SEASON_UNPARSEABLE` is replaced by `EPISODE_UNPARSEABLE`; any video file
  without a parseable season and episode fails the job before anything moves.

## [0.6.0] - 2026-09-24

### Added

- `POST /api/v1/transfers/stop-seeding`: pauses every seeding torrent via
  torrent-downloader, returns its `{status, message}` with 202. No job involved.

## [0.5.0] - 2026-09-21

### Changed

- Imports `API_PREFIX`, `API_KEY_HEADER`, `HEALTH_PATH` and `MEDIA_TYPE_SUBDIRS`
  from medialab-contracts v0.4.0; the per-client `_PREFIX` and the worker's
  media subdir map are gone. `scripts/notify_complete.py` keeps its literals
  by design (stdlib-only). Wire values unchanged.

### Changed

- CI calls the workspace's shared reusable workflow (`MickMarch/medialab`
  `python-ci.yml`) instead of carrying its own copy of the quality gate.
- Releases publish automatically from the CHANGELOG section matching the
  pushed tag (shared `release.yml`).
- Dependabot updates arrive grouped, one PR per ecosystem.

### Fixed

- `.env.example` pointed `MEDIALAB_JELLYFIN_URL` at port 8000; medialab-jellyfin
  listens on 8001. A fresh copy of the template now works.

## [0.4.2] - 2026-07-20

### Fixed

- Removed the per-download REGISTER pipeline step (and `JobStatus.REGISTER`).
  The Jellyfin library root is registered once at setup, not per download;
  adding a sub-path of an already-registered root makes Jellyfin return 404, so
  every real download failed at REGISTER. The pipeline now goes RENAME -> SCAN
  directly - Jellyfin recursively scans the already-covered path. Found by the
  first real end-to-end run (a completed movie failed `REGISTER: medialab-jellyfin
  returned 500/404`). `register_path` stays available for one-time setup use.

## [0.4.1] - 2026-07-20

### Changed

- The qBittorrent completion relay (`scripts/notify_complete.py`) is now
  standalone and standard-library-only (`urllib` instead of `httpx`, no package
  imports). It can be dropped in as a single file and run by qBittorrent's
  completion command on the host - no install, no venv - and runs unchanged
  inside the qBittorrent container later. Added a test suite covering arg
  handling, the keyed POST, and error paths.

### Docs

- README: how to wire qBittorrent's "Run external program on torrent
  completion" to the relay (the step that makes the post-download pipeline
  actually run), plus a Windows Defender write-lock note.

## [0.4.0] - 2026-07-20

### Changed

- **Jobs are now keyed by a surrogate `id` (uuid), not the torrent hash.** The
  `pipeline_job` primary key is a generated uuid; `torrent_hash` becomes a
  nullable, backfilled column. This decouples job identity from how the torrent
  was sourced, so a `.torrent`-URL download (whose info-hash is not known up
  front) creates a job the same way a magnet does. The hash is stamped from the
  downloader's response (or by the completion webhook).
- `POST /download` body field `magnet_uri` renamed to `source_url` (a magnet or
  an http `.torrent` URL), forwarded to torrent-downloader. The gateway no
  longer parses the hash itself - the downloader resolves it and returns
  `torrent_hash`, which the gateway stamps onto the job.
- `GET /jobs/{id}` and `POST /jobs/{id}/retry` are addressed by the surrogate
  `job_id`, not the hash. The completion webhook still matches by hash
  internally. Retry on a job with no stamped hash yet returns 409.
- `JobView.id` is now a string (uuid); `JobView.torrent_hash` is nullable.

### Migration

- The `pipeline_job` schema changed (uuid PK, nullable hash). Pre-1.0 homelab
  service: an existing `orchestrator.db` is not migrated - delete it and let the
  service recreate the table. In-flight jobs (if any) are lost; re-submit.

## [0.3.0] - 2026-07-02

### Added

- `GET /search/torrents` now forwards TV season/episode targeting to
  torrent-downloader. `media_type` is a required query param; shows accept
  optional `season`/`episode`. The gateway validates the combination via
  `TorrentSearchScope` (422 on movie+season or an orphan episode) before
  proxying. Search-steering only - no job-table change.

### Changed

- `medialab-contracts` pin bumped to v0.3.0 (`TorrentSearchScope`).

## [0.2.0] - 2026-06-29

### Changed

- `POST /download` now resolves the canonical `Title (Year)` from TMDB at submit
  time (the `tmdb_id` is already known), storing `resolved_title`/`resolved_year`
  on the job immediately so `GET /jobs` shows the title from the moment of
  download instead of only the hash. Best-effort: a metadata-lookup failure is
  logged and the download still proceeds (RESOLVE_META backfills later).
- Title/year extraction moved to a shared `services/metadata.py`
  (`extract_title_year` / `resolve_title_year`), used by both the submit path
  and the worker's RESOLVE_META step.

### Fixed

- Title/year extraction read the TMDB fields off the wrong dict level: the
  torrent-downloader detail response wraps the body under `data`, but the worker
  read the top level, so `resolved_title` would have come back empty even after
  the post-download pipeline ran. The shared helper now unwraps `data` and
  degrades to `("", 0)` on a missing/short body.

## [0.1.0] - 2026-06-26

### Added

- Front-door orchestrating gateway scaffold with full engineering standards
  from the first commit (ruff, mypy, pre-commit, CI lint/typecheck/test/audit,
  dependabot), consuming shared models from `medialab-contracts` v0.2.0.
- SQLite `pipeline_job` store (`store/jobs.py`): nine-state `JobStatus` lifecycle,
  `PipelineJob` model, `JobStore` with create/lookup/list/update, lowercase-hash
  normalisation, an update-column whitelist, and restart-resume persistence.
- Async downstream clients (`clients/`): a `DownstreamClient` base mapping any
  transport/HTTP failure to a single `DOWNSTREAM_UNAVAILABLE` error plus a
  reachability probe, and concrete `TorrentDownloaderClient` / `JellyfinClient`.
- Pipeline worker (`services/worker.py`): a forward-retry saga advancing a job
  one idempotent step at a time (stop-seed, resolve-meta, rename, register,
  scan), persisting after each transition and capturing failures as `FAILED`.
- TV-folder rename (`services/rename.py`): PTN season-number parsing only,
  TMDB-sourced `Title (Year)/Season NN/` destination, idempotent move, and a
  clear failure on unparseable/multi-season release names.
- Bot-facing gateway surface: stateless search proxies, `POST /download`
  (creates a job), `GET /transfers` (read-through merge with job rows),
  `GET /jobs` + `GET /jobs/{hash}` + `POST /jobs/{hash}/retry`, `GET /storage`,
  and a public `GET /health` aggregating downstream reachability.
- `POST /webhooks/torrent-complete` entry point (keyed) that records the release
  name, advances the matching job off the request thread, and tracks orphan
  completions.
- `scripts/notify_complete.py`: a dumb qBittorrent completion relay turning the
  hook's `%I`/`%N` args into one webhook POST.
