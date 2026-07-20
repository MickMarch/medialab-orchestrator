# medialab-orchestrator

Front-door orchestrating gateway for the medialab media lifecycle. The Discord
bot talks to exactly one service - this one. The orchestrator brokers the whole
lifecycle (search -> download -> stop-seed -> rename -> register -> scan) and
fans out to the downstream worker services (torrent-downloader,
medialab-jellyfin), which are never client-facing.

A SQLite `pipeline_job` table is the system's spine: one row per torrent,
advanced one state at a time by an in-process asyncio worker and persisted after
each transition, so a restart resumes from the last committed state.

## Stack

FastAPI, `uv`, `hatchling` + `hatch-vcs`, `pydantic-settings`, `httpx`, `PTN`
(season-number parsing only), stdlib `sqlite3`. Shared models from
`medialab-contracts`.

## Commands

```bash
uv sync --dev                  # install
uv run medialab-orchestrator   # run API (production)
uv run medialab-orchestrator-dev  # run API (dev, hot-reload)
uv run pytest                  # tests
```

## Environment

Copy `.env.example` to `.env` and populate. All config loads via
`pydantic-settings`. Every field is optional at import time (for CI), but the
service needs real downstream URLs/keys at runtime.

- `API_KEY` - the gateway's own key (the bot sends this in `X-API-Key`)
- `TORRENT_DOWNLOADER_URL`, `TORRENT_DOWNLOADER_API_KEY`
- `MEDIALAB_JELLYFIN_URL`, `MEDIALAB_JELLYFIN_API_KEY`
- `MEDIA_MOUNT_PATH` - in-container path of the mounted host media dir (default
  `/media`), used to compute TV-rename source/dest
- `DB_PATH` - SQLite file (default `./data/orchestrator.db`)

`scripts/notify_complete.py` reads its own minimal env (`ORCHESTRATOR_URL` plus
key) - it runs as a qBittorrent child process, outside this container.

## Wiring the qBittorrent completion hook

The post-download pipeline (stop-seed -> rename -> register -> Jellyfin scan)
only runs when qBittorrent tells the orchestrator a torrent finished. Without
this, jobs sit at `DOWNLOAD_SUBMITTED` forever.

`scripts/notify_complete.py` is the relay. It is **standalone and stdlib-only**
(no third-party or package imports), so it runs anywhere Python is present - the
host next to qBittorrent today, or inside the qBittorrent container after the
containerization work (backlog item 20). Migration is just re-pointing the one
qBittorrent setting at the in-container path; no code changes.

Setup (host qBittorrent):

1. Copy `src/medialab_orchestrator/scripts/notify_complete.py` anywhere on the
   host (e.g. `C:\medialab\notify_complete.py`).
2. Give the relay its env. qBittorrent's completion command inherits the
   environment of the qBittorrent process, so set these as **user/system
   environment variables** (or wrap the call in a `.bat` that exports them):
   - `ORCHESTRATOR_URL=http://localhost:8000` (the published gateway port)
   - `ORCHESTRATOR_API_KEY=<the gateway API_KEY>`
3. qBittorrent -> Tools -> Options -> Downloads -> "Run external program on
   torrent completion", set:
   ```
   python "C:\medialab\notify_complete.py" "%I" "%N"
   ```
   (`%I` = info-hash, `%N` = torrent name). Use the full path to `python` if it
   is not on qBittorrent's PATH.

The relay POSTs `{hash, name}` to `/api/v1/webhooks/torrent-complete` with the
`X-API-Key`; the gateway matches the job by hash (or orphan-inserts) and advances
it off the request thread, returning `202` immediately so qBittorrent is never
blocked. Verify with `GET /api/v1/jobs` - a completed torrent's job should leave
`DOWNLOAD_SUBMITTED` and progress toward `DONE`.

### Windows write-lock note

If a download errors with `Couldn't write to file. Reason: 'Access is denied'`
and flips to upload-only, that is a transient file lock (typically Windows
Defender real-time scanning the file mid-write), not a medialab bug. Add a
Defender exclusion for the media directory (e.g. `F:\Media`) and `qBittorrent.exe`
(Windows Security -> Virus & threat protection -> Manage settings -> Exclusions).
Auto-recovery of errored torrents is backlog item 10.

## Versioning

Version derives from git tags via `hatch-vcs` - never hardcoded.
`src/medialab_orchestrator/_version.py` is generated at build time and
gitignored.
