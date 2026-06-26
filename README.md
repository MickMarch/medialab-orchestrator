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

## Versioning

Version derives from git tags via `hatch-vcs` - never hardcoded.
`src/medialab_orchestrator/_version.py` is generated at build time and
gitignored.
