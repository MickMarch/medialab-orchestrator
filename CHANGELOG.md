# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
