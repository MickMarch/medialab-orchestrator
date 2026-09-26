# medialab-orchestrator

Front-door orchestrating gateway for the
[medialab](https://github.com/MickMarch/medialab) suite. The Discord bot talks
to exactly one service, this one. It brokers the whole lifecycle
(search -> download -> stop-seed -> resolve metadata -> rename -> Jellyfin
scan) and fans out to the downstream workers, torrent-downloader and
medialab-jellyfin, which are never client-facing.

A SQLite `pipeline_job` table is the system's spine: one row per torrent,
advanced one state at a time by an in-process asyncio worker and persisted
after each transition, so a restart resumes from the last committed state.

## Setup

```bash
uv sync --dev
cp .env.example .env     # then fill in the values
uv run medialab-orchestrator-dev   # dev, hot-reload
uv run medialab-orchestrator       # production
```

`.env.example` documents every variable: the gateway's own `API_KEY`, the two
downstream URL + key pairs, the media mount path, and the SQLite path.
Interactive docs at `/docs`.

The service runs as a container from the workspace `docker-compose.yml`, which
bind-mounts the host media dir and a volume for the SQLite file; see the
[workspace README](../README.md).

## API

All paths under `/api/v1`. Every endpoint except `/health` requires
`X-API-Key: <API_KEY>`.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Public. Reachability of both downstream services plus `needs_attention`, the count of jobs waiting on a human. |
| `GET` | `/search/tmdb?query=` | Proxy to torrent-downloader. No job created. |
| `GET` | `/search/tmdb/{movie\|show}/{tmdb_id}` | Proxy. Show detail carries the season list. |
| `GET` | `/search/torrents?query=&media_type=[&season=&episode=]` | Proxy; scope validated via `TorrentSearchScope`. |
| `POST` | `/download` | Body `{source_url, media_type, tmdb_id}`. Creates a `pipeline_job`, forwards to torrent-downloader, stamps the returned `torrent_hash`. Returns the job (`202`). |
| `GET` | `/transfers` | Live downloader transfers merged with job rows. |
| `GET` | `/jobs[?status=]`, `GET /jobs/{id}` | Pipeline lifecycle view. |
| `GET` | `/jobs/{id}/deletion-plan` | What a delete would remove: torrent, download folder, placed files, Jellyfin path, or a refusal reason. No side effects. |
| `DELETE` | `/jobs/{id}` | Executes that plan; job becomes `DELETED`. `409` with the reason when refused. |
| `POST` | `/jobs/{id}/retry` | Re-enter the worker from the last good state (`FAILED` or `NEEDS_ATTENTION`); resets the automatic retry budgets. `409` if the job has no hash yet. |
| `GET` | `/storage` | Disk usage of the media mount (measured here). |
| `POST` | `/transfers/stop-seeding` | Proxy to torrent-downloader: pause every seeding (completed) torrent, never an in-progress download. `202`. No job involved. |
| `POST` | `/webhooks/torrent-complete` | Body `{hash, name}`, sent by the completion relay. Matches the job by hash (or orphan-inserts), advances it off the request thread, returns `202`. |

Errors: `{"status": "error", "code": "<ErrorCode>", "detail": "..."}`. Every
response includes an `X-Request-ID` UUID.

## Wiring the qBittorrent completion hook

The post-download pipeline runs only when qBittorrent tells the orchestrator a
torrent finished. Without this, jobs sit at `DOWNLOAD_SUBMITTED`.

`src/medialab_orchestrator/scripts/notify_complete.py` is the relay. It is
standalone and stdlib-only, so it runs anywhere Python is present: on the host
next to qBittorrent today, or inside a qBittorrent container later with only
the one qBittorrent setting re-pointed.

1. Copy `notify_complete.py` anywhere on the host (e.g.
   `C:\medialab\notify_complete.py`).
2. Give it its env. qBittorrent's completion command inherits the qBittorrent
   process environment, so set these as user or system environment variables
   (or wrap the call in a `.bat` that sets them; keep that file out of git):
   `ORCHESTRATOR_URL=http://localhost:8000` and
   `ORCHESTRATOR_API_KEY=<the gateway API_KEY>`.
3. qBittorrent -> Tools -> Options -> Downloads -> "Run external program on
   torrent completion":
   ```
   python "C:\medialab\notify_complete.py" "%I" "%N" "%F"
   ```
   (`%I` info-hash, `%N` torrent name, `%F` content path: the real root file
   or folder on disk, which the display name is not. Use the full path to
   `python` if it is not on qBittorrent's PATH.)

Verify with `GET /api/v1/jobs`: a completed torrent's job should leave
`DOWNLOAD_SUBMITTED` and progress to `DONE`.

### Windows write-lock note

If a download errors with `Couldn't write to file. Reason: 'Access is denied'`
and flips to upload-only, that is a transient file lock (typically Windows
Defender scanning the file mid-write), not a medialab bug. Add a Defender
exclusion for the media directory and `qBittorrent.exe`. Automatic recovery of
errored torrents is tracked in
[MickMarch/medialab#20](https://github.com/MickMarch/medialab/issues/20).

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

Standards, workflow and release process: [workspace CLAUDE.md](../CLAUDE.md).
Code-local notes: [CLAUDE.md](CLAUDE.md).
