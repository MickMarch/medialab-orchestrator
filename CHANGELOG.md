# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.8.0] - 2026-10-07

### Added

- Credential health: `GET /api/v1/health` carries a `credentials` map merging
  each worker's per-key states with the bot's login report; an unreachable
  worker's keys read `unreachable`. `POST /api/v1/credentials/{name}` lets the
  bot report its Discord login result. On the health poll cadence the
  orchestrator posts one Discord notice per credential entering or leaving
  `invalid`, remembered in `CREDENTIALS_PATH` so a restart does not
  re-announce (MickMarch/medialab#136).

### Changed

- medialab-contracts pin moved to the release carrying the credential models.

## [1.7.0] - 2026-10-05

### Added

- `GET /search/torrents/progress`: proxy to torrent-downloader's progress
  read for a search in flight, same parameters as `/search/torrents`.

### Changed

- medialab-contracts pinned to the tag that carries `TorrentSearchProgress`.

## [1.6.0] - 2026-10-05

### Added

- RENAME: a show video whose file name carries no season and episode takes
  them from the nearest parent folder under the download root that does, then
  from the release name. Only one video may claim a folder-derived code; a
  second claimant, or a code a properly named file already owns, stays an
  extra. The placement is logged at INFO with the name it came from.

## [1.5.2] - 2026-10-04

### Fixed

- `POST /download` and `POST /jobs/{id}/redo` pass torrent-downloader's
  `503 SOURCE_UNREACHABLE` through with its status and code instead of
  collapsing it into `502 DOWNSTREAM_UNAVAILABLE`, so the bot and web can tell
  a retryable source fetch failure from a dead worker.

## [1.5.1] - 2026-10-04

### Fixed

- A show pack with a video that carries no episode number (a bundled movie, a
  featurette) no longer fails the whole job. Episodes are placed, the extra goes
  to `extras/` under the series folder, is listed in `placed_paths` and logged at
  WARNING. A pack where no video parses at all still fails `EPISODE_UNPARSEABLE`.

## [1.5.0] - 2026-10-04

### Added

- `DISMISSED` job status with `POST /jobs/{id}/dismiss` and bulk `POST /jobs/dismiss`:
  close a `FAILED` or `NEEDS_ATTENTION` job a human judged not worth pursuing. The
  error and files stay; the follow submission is marked ignored; the job leaves the
  health poll and the `needs_attention` count.
- `attention_cause` and `dismissed_at` on the job view, so clients can offer the
  action that resolves a flagged job (Redo for a vanished torrent, Retry otherwise).

### Changed

- `POST /jobs/{id}/redo` also accepts a `NEEDS_ATTENTION` job that placed nothing.
- `POST /jobs/{id}/retry` returns `409 JOB_NOT_RETRYABLE` for a `DELETED` or
  `DISMISSED` job.

## [1.4.1] - 2026-10-03

### Fixed

- Re-downloading a title whose earlier job is `DELETED` no longer fails on
  the unique `torrent_hash`: deletion moves the hash into a new
  `deleted_hash` column (existing deleted rows are migrated at startup), so
  the new job is stamped and a late completion webhook is treated as an
  orphan rather than advancing the deleted job. When a live job already owns
  the hash, the new job is marked `FAILED` with the owner's id instead of a
  500 (MickMarch/medialab#119).

## [1.4.0] - 2026-10-03

### Added

- `GET /health` carries `vpn_interface_bound`, torrent-downloader's VPN
  assertion, so the web UI and the bot can warn before a download is
  confirmed. `false` whenever the downloader is unreachable. Enforcement is
  unchanged and stays in torrent-downloader (MickMarch/medialab#113).

### Changed

- `.env.example` points `TORRENT_DOWNLOADER_URL` at `http://gluetun:8001`:
  the downloader runs inside the gluetun VPN namespace in the compose
  layout (MickMarch/medialab#113).

### Deprecated

- `scripts/notify_complete.py`, the host-side completion relay. The compose
  layout uses a `curl` autorun command inside the qBittorrent container
  instead; the README documents both. The relay is removed one release
  after the containerized stack ships.

## [1.3.1] - 2026-10-02

### Fixed

- An orphan job (a completion webhook with no matching job) resolves its
  title and year from the release name instead of asking TMDB for id 0 and
  landing under an empty title (MickMarch/medialab#29).

## [1.3.0] - 2026-10-02

### Added

- The follow poll fetches a complete season as one season pack when nothing
  of it has been fetched yet, including the season the follow starts in; a
  missing pack parks the season as `pack_not_found`, posts a Discord notice,
  and waits for the user's choice through
  `POST /watchlist/show/{tmdb_id}/seasons/{season}/decision` (retry with a
  longer search, retry with fewer seeders, the standard pack again, or
  episode by episode). `GET /watchlist/show/{tmdb_id}/episodes` carries the
  per-season state as `seasons_follow`. Four new runtime settings:
  `follow_pack_minimum_seeders`, `follow_pack_timeout_seconds`,
  `follow_pack_retry_timeout_seconds`, `follow_pack_retry_minimum_seeders`
  (MickMarch/medialab#104).
- medialab-contracts pinned to v1.1.0 for the season follow models.

## [1.2.0] - 2026-10-02

### Added

- `POST /jobs/deletion-plan` and `POST /jobs/delete` take a list of job ids
  and return one plan or one result per id, so a client can confirm and
  delete many downloads at once; each job is handled exactly as the single
  delete is, and a refused or failed job never stops the rest
  (MickMarch/medialab#105).

## [1.1.0] - 2026-09-27

### Added

- The follow poll (MickMarch/medialab#24): `services/follow.py` ticks every
  `follow_poll_interval_seconds` over every unpaused followed show, computes
  the wanted episodes (on or after the start point, aired at least
  `follow_delay_hours` ago, not in the library, not queued, never submitted),
  asks torrent-downloader's pick route for each in air order with
  `follow_minimum_seeders`, and submits the candidate through the
  `POST /download` path, at most `follow_max_submissions_per_tick` per show
  per tick. A downloader error stops that show for the tick; one show
  raising never stops the sweep; `last_checked_at` and `last_submitted` are
  stamped.
- `TorrentDownloaderClient.pick_torrent`: `GET /search/torrents/pick`,
  `None` on `NO_CANDIDATE`.
- Runtime settings `follow_poll_interval_seconds`,
  `follow_max_submissions_per_tick`, `follow_delay_hours` and
  `follow_minimum_seeders`, applied at the next follow tick.
- `DISCORD_NOTIFY_WEBHOOK_URL` (optional): each follow submission posts
  `Following <title>: submitted S02E05 (<release name>)` to the channel
  webhook; a failed notice is logged, never raised.
- Watchlist routes: `GET /watchlist/show/{tmdb_id}/episodes` (the show view
  with `submitted` and `wanted` per episode; `404` unless followed),
  `DELETE /watchlist/show/{tmdb_id}/episodes/{season}/{episode}/submission`
  (Retry, `204`), and `POST /watchlist/show/{tmdb_id}/follow/check` (runs one
  check now and returns `{"submitted": [...]}`).

## [1.0.0] - 2026-09-27

### Added

- Follow routes for the watchlist (MickMarch/medialab#24, storage and routes
  only; the follow poll and auto-submit come separately):
  `PUT /watchlist/show/{tmdb_id}/follow` with a `FollowRequest` body turns a
  saved show into a follow (`404` `WATCHLIST_ITEM_NOT_FOUND` when the show is
  not saved, `422` for a movie), `DELETE .../follow` returns it to saved,
  `POST .../follow/pause` and `/resume` flip `paused`. `GET /watchlist` takes
  `kind=saved|following`; a following item carries `follow` (start,
  resolution, paused, followed_at, last_checked_at, last_submitted).
- `watchlist_item` gains `kind` and the follow columns; a new
  `follow_submission` table records what a follow has submitted per episode.
  `DELETE /jobs/{id}` marks the job's submission `ignored`; a redo repoints it
  at the replacement job.
- Discover, TMDB search and the show header carry `watchlist_kind` beside
  `on_watchlist`.

### Changed

- **Breaking:** the wishlist is the watchlist. `/wishlist` routes are
  `/watchlist` with the same verbs; `on_wishlist` is `on_watchlist`. The
  `wishlist_item` table is renamed `watchlist_item` at startup with rows kept.
- A job reaching `DONE` removes only a saved title; a followed show stays.
- medialab-contracts pinned at v1.0.0.

## [0.20.0] - 2026-09-27

### Added

- `GET /search/tmdb/{media_type}/{tmdb_id}/videos?season=` proxies the
  downloader's trailers and teasers.

### Changed

- medialab-contracts pinned at v0.12.0.

## [0.19.0] - 2026-09-27

### Added

- `POST /jobs/{id}/redo` replaces a `DONE` job with a newly picked torrent in
  one action: body `DownloadRequest`; `409` `JOB_NOT_DONE` unless the job is
  `DONE`, `409` when its deletion plan is refused, `422` when `media_type` or
  `tmdb_id` differ from the job's. The replacement job is created first with
  `redo_of` set and the old job's `season` and `episode`, then the old job is
  deleted through the existing deletion service (`DELETED`), then the download
  is submitted as `POST /download` would. Returns `202` `DownloadResponse`
  with the new job. A failed deletion returns `502` `REDO_DELETION_FAILED`
  with the old job untouched and the replacement kept, so the next redo reuses
  it instead of creating another.
- Job views carry `redo_of` (stored) and `redone_by` (the newest job replacing
  this one, computed on read from one query per listing). The `pipeline_job`
  table gains a nullable `redo_of` column, added to an existing database at
  startup.

### Changed

- The submit path behind `POST /download` (create the row, resolve the title
  best effort, forward the download, stamp the hash) moved to
  `services/download.py` so redo and download share it.

## [0.18.0] - 2026-09-27

### Added

- `GET /shows/{tmdb_id}` returns a `ShowBrowseResponse`: the show's title,
  year, poster and overview from TMDB detail, its seasons and every episode
  (from torrent-downloader's episode listing) flagged `aired` (air date on or
  before today, UTC), `in_library` (best effort: a medialab-jellyfin lookup
  failure leaves it false) and `queued_job_id` (the newest non-terminal job
  whose scope covers the episode; a whole-series job covers every episode, a
  season job its season). Series-level `on_wishlist` and `in_library` as on
  discover. `503` `TMDB_UNAVAILABLE` relayed from the downloader.
- `POST /download` accepts optional `season` and `episode`, the scope the
  torrent was searched with; both are stored on the job and returned on every
  job view. Existing callers without them keep working. The `pipeline_job`
  table gains nullable `season` and `episode` columns, added to an existing
  database at startup.

### Changed

- medialab-contracts dependency bumped to v0.11.0 (`Episode`, `Season`,
  `EpisodeKey`, `EpisodeState`, `SeriesEpisodesResponse`,
  `LibraryEpisodesResponse`, `ShowBrowseResponse`).

## [0.17.0] - 2026-09-27

### Added

- `GET /jobs` and `GET /jobs/{id}` attach live `progress` (`JobProgress`:
  progress, download speed, ETA, qBittorrent state) to jobs in
  `DOWNLOAD_SUBMITTED` or `DOWNLOADING` with a torrent hash, from one
  transfers read per request and only when such a job is listed. qBittorrent's
  unknown ETA becomes `null`. A failed transfers read returns the jobs without
  progress. The same read moves a `DOWNLOAD_SUBMITTED` job to `DOWNLOADING`
  when qBittorrent is actively fetching it, using the health poll's rule.
- `GET /search/tmdb` results carry `on_wishlist` and `in_library` (best
  effort: a library lookup failure leaves it false), with one library lookup
  per media type present. TMDB `tv` results match `show` wishlist and library
  entries and keep their own `media_type` on the wire.

### Changed

- medialab-contracts dependency bumped to v0.10.0 (`JobProgress`,
  `ETA_UNKNOWN_SECONDS`).

## [0.16.1] - 2026-09-27

### Fixed

- Jobs now move from `DOWNLOAD_SUBMITTED` to `DOWNLOADING` when the health
  poll sees qBittorrent actively fetching the torrent.

## [0.16.0] - 2026-09-27

### Added

- `GET /discover/{movie|show}?genre=&page=` proxies torrent-downloader's
  trending and popular-by-genre titles, setting `on_wishlist` from the
  wishlist and `in_library` from medialab-jellyfin (best effort: a library
  lookup failure leaves it false). `GET /discover/{movie|show}/genres`
  proxies the genre list. A TMDB outage returns `503` `TMDB_UNAVAILABLE`.
- Shared wishlist in the SQLite file (`wishlist_item`, created at startup):
  `GET /wishlist?media_type=` newest first with `in_library`, idempotent
  `PUT /wishlist/{media_type}/{tmdb_id}` (`200` with the item; a repeat keeps
  the original `added_at`) and `DELETE /wishlist/{media_type}/{tmdb_id}`
  (`204` even when absent).
- A job reaching `DONE` removes its title from the wishlist; orphan jobs
  (no TMDB id) leave it untouched.

### Changed

- `medialab-contracts` pinned to v0.9.0 for the discover and wishlist models.

## [0.15.0] - 2026-09-26

### Added

- Runtime settings: `GET /settings` aggregates every service, `PUT` and
  `DELETE /settings/{service}/{key}` change one; the orchestrator's own
  health-poll budgets and interval persist in `SETTINGS_PATH` on the data
  volume.

### Changed

- The health poll reads its interval and budgets from config on every tick,
  so a settings change applies without a restart; interval `0` pauses the
  poll instead of disabling it at startup.

## [0.14.0] - 2026-09-26

### Added

- `POST /download` accepts `release_name` (the picked torrent's name) so a job
  is identifiable while it downloads; completion still overwrites it.

## [0.13.0] - 2026-09-26

### Added

- `GET /search/torrents` forwards `alt_query` to torrent-downloader.

## [0.12.0] - 2026-09-26

### Added

- `DELETE /search/cache` proxies torrent-downloader's cache clear so a client
  can drop stale search result sets.

## [0.11.1] - 2026-09-26

### Fixed

- `GET /storage` measures the media mount itself instead of proxying to
  torrent-downloader, which required a `path` it was never given (422 -> 502).

## [0.11.0] - 2026-09-26

### Changed

- RENAME reads the download from the staging directory
  (`<media>/_incoming/<Movies|Shows>`, contracts `STAGING_SUBDIR`) and places it
  into the library root; a download still sitting in the library root (from
  before staging) is found there for one release. The deletion plan names the
  staging folder.

### Fixed

- A legacy `source_path` holding a host root path (jobs from before the
  content_path fix) is ignored instead of being joined onto the media root in
  RENAME and in the deletion plan.

## [0.10.0] - 2026-09-26

### Fixed

- RENAME no longer reports DONE when a locked source file stayed behind
  after its copy landed (a Windows file lock right after completion left the
  film twice, once under the torrent name). A same-size leftover is removed,
  a partial destination is redone, and any video still in the download folder
  fails the job with `RENAME_INCOMPLETE` so the health poll retries it.

### Added

- `GET /jobs/{id}/deletion-plan` and `DELETE /jobs/{id}`: undo a download at
  any stage (torrent and its data via the downloader, the download folder,
  exactly the files RENAME placed, a Jellyfin `Deleted` notice); the job is
  kept as `DELETED`. RENAME records `placed_paths`; jobs that predate it are
  refused for shows (folder named) and fall back to the movie folder for
  movies. `deleted_at` column.

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
