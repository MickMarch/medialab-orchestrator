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
| `GET` | `/search/tmdb?query=` | Proxy to torrent-downloader; each result gains `on_watchlist`, `watchlist_kind` and best-effort `in_library` (TMDB `tv` matches `show`). No job created. |
| `GET` | `/search/tmdb/{movie|show}/{tmdb_id}` | Proxy. Show detail carries the season list. |
| `GET` | `/search/tmdb/{movie|show}/{tmdb_id}/videos?season=` | Proxy: YouTube trailers and teasers, official first; `season` narrows a show to one season. |\|show}/{tmdb_id}` | Proxy. Show detail carries the season list. |
| `DELETE` | `/search/cache` | Proxy to torrent-downloader `DELETE /cache`: drops cached search and TMDB result sets. |
| `GET` | `/search/torrents?query=&media_type=[&season=&episode=]` | Proxy; scope validated via `TorrentSearchScope`. |
| `GET` | `/discover/{movie\|show}[?genre=&page=]` | Proxy to torrent-downloader: trending titles, or popular in `genre`. Sets `on_watchlist` and `watchlist_kind` from the watchlist and `in_library` from medialab-jellyfin (best effort, false on failure). `503` `TMDB_UNAVAILABLE` when TMDB is down. No job created. |
| `GET` | `/discover/{movie\|show}/genres` | Proxy: TMDB genre ids and names, usable as `genre`. |
| `GET` | `/shows/{tmdb_id}` | A show's header, seasons and episodes (`ShowBrowseResponse`). Each episode carries `aired`, `in_library` (best effort, false on failure) and `queued_job_id`, the newest non-terminal job whose `season`/`episode` scope covers it. The header carries `on_watchlist` and `watchlist_kind`. `503` `TMDB_UNAVAILABLE` when TMDB is down. No job created. |
| `GET` | `/watchlist[?media_type=&kind=]` | The shared watchlist, newest first, with `in_library`. `kind` is `saved` or `following`; a following item carries `follow` (start, resolution, paused, followed_at, last_checked_at, last_submitted). |
| `PUT` | `/watchlist/{movie\|show}/{tmdb_id}` | Body `{title, year, poster_path, overview}`. Idempotent upsert, `200` with the item; a repeat keeps the original `added_at` and any follow. |
| `DELETE` | `/watchlist/{movie\|show}/{tmdb_id}` | Removes the row, saved or followed. Idempotent, `204` even when absent. A job reaching `DONE` also removes its title when it is only saved, never a follow. |
| `PUT` | `/watchlist/show/{tmdb_id}/follow` | Body `FollowRequest` (`start.mode` `new_only`, `from` with `season` and `episode`, or `beginning`; `resolution`, default `1080p`). The show must already be saved (`404` `WATCHLIST_ITEM_NOT_FOUND`); the UI saves, then follows. Sets `kind = following`, stamps `followed_at`, unpauses; idempotent. `422` for a movie. |
| `DELETE` | `/watchlist/show/{tmdb_id}/follow` | Unfollow: back to `saved`, follow state cleared, row kept. `204` even when absent. |
| `POST` | `/watchlist/show/{tmdb_id}/follow/pause`, `/resume` | Flip `paused`; `200` with the item, `404` unless the show is followed. |
| `POST` | `/watchlist/show/{tmdb_id}/follow/check` | Check now: runs one follow check for this show (paused or not) and returns `{"submitted": ["S02E05", ...]}`. `404` unless followed; `503` `TMDB_UNAVAILABLE` when the show view cannot be fetched. |
| `GET` | `/watchlist/show/{tmdb_id}/episodes` | `GET /shows/{tmdb_id}` plus, per episode, `submitted` (`submitted`, `ignored` or `null`) and `wanted` (the next tick will fetch it), and `seasons_follow`: one `SeasonFollowState` (`season`, `mode`, `attempts`, `last_tried_at`, `job_id`) per season that has one. `404` unless followed. |
| `POST` | `/watchlist/show/{tmdb_id}/seasons/{season}/decision` | Body `{mode}`: `pack_retry_timeout`, `pack_retry_seeders`, `pack` or `episodes`. Only for a season whose `mode` is `pack_not_found` (`409` otherwise); returns the updated state. The next tick, or Check now, runs the choice. |
| `DELETE` | `/watchlist/show/{tmdb_id}/episodes/{season}/{episode}/submission` | Retry: forgets the submission so the next tick may fetch the episode again. `204` even when absent. |
| `POST` | `/download` | Body `{source_url, media_type, tmdb_id}`, optional `release_name`, `season`, `episode` (the searched scope; both absent means the whole title). Creates a `pipeline_job`, forwards to torrent-downloader, stamps the returned `torrent_hash`. Returns the job (`202`). |
| `GET` | `/transfers` | Live downloader transfers merged with job rows. |
| `GET` | `/jobs[?status=]`, `GET /jobs/{id}` | Pipeline lifecycle view. Jobs in `DOWNLOAD_SUBMITTED` or `DOWNLOADING` carry live `progress` (`progress`, `download_speed`, `eta_seconds`, `state`) from one transfers read, or `null` when the read fails; an actively fetching submitted job is moved to `DOWNLOADING` on read. Each job carries `redo_of` (the job it replaces) and `redone_by` (the newest job replacing it, computed on read). |
| `POST` | `/jobs/{id}/redo` | Body as `POST /download` (the newly picked torrent; `media_type` and `tmdb_id` must match the job's, else `422`). Only for a `DONE` job (`409` `JOB_NOT_DONE`) whose deletion plan is not refused (`409`). Creates the replacement job with `redo_of` and the old job's `season`/`episode`, deletes the old job (`DELETED`), then submits the download. Returns the new job (`202`). `502` `REDO_DELETION_FAILED` when the deletion fails: the old job is untouched and the replacement row is reused on the next call. |
| `GET` | `/jobs/{id}/deletion-plan` | What a delete would remove: torrent, download folder, placed files, Jellyfin path, or a refusal reason. No side effects. |
| `DELETE` | `/jobs/{id}` | Executes that plan; job becomes `DELETED`. `409` with the reason when refused. |
| `POST` | `/jobs/deletion-plan` | Body `{job_ids: [...]}` (1 to 100, duplicates collapsed). One `{job, plan}` per id in request order; an unknown id has `job: null` and a plan refused with `no such job`. No side effects. |
| `POST` | `/jobs/delete` | Body as above. Deletes each job exactly as `DELETE /jobs/{id}` does, one after another; a refusal or downstream failure is reported in that id's `error` and the rest still run. Always `200` for a valid body. |
| `POST` | `/jobs/{id}/retry` | Re-enter the worker from the last good state (`FAILED` or `NEEDS_ATTENTION`); resets the automatic retry budgets. `409` if the job has no hash yet. |
| `GET` | `/storage` | Disk usage of the media mount (measured here). |
| `GET` | `/settings` | Every service's runtime settings keyed by service name (`torrent-downloader` relayed, `medialab-orchestrator` local). |
| `PUT` | `/settings/{service}/{key}` | Override one setting (`{"value": ...}`) on the named service; `404` unknown service or key, `422` out of bounds. |
| `DELETE` | `/settings/{service}/{key}` | Drop the override. |
| `POST` | `/transfers/stop-seeding` | Proxy to torrent-downloader: pause every seeding (completed) torrent, never an in-progress download. `202`. No job involved. |
| `POST` | `/webhooks/torrent-complete` | Body `{hash, name}`, sent by the completion relay. Matches the job by hash (or orphan-inserts), advances it off the request thread, returns `202`. |

Errors: `{"status": "error", "code": "<ErrorCode>", "detail": "..."}`. Every
response includes an `X-Request-ID` UUID.

## Runtime settings

Declared in `core/settings.py`, read and overridden through `/settings`
(`medialab-orchestrator` service). Each has a `.env` default of the same name
in upper case; an override persists in `SETTINGS_PATH` and applies at the next
tick of the loop it tunes.

| Key | Range | Default | Tunes |
|---|---|---|---|
| `health_poll_interval_seconds` | 0 to 3600 | 300 | Seconds between health-poll ticks; 0 pauses it. |
| `auto_resume_max` | 0 to 10 | 3 | Resumes of a stalled download before `NEEDS_ATTENTION`. |
| `auto_retry_max` | 0 to 10 | 2 | Retries of a `FAILED` job before `NEEDS_ATTENTION`. |
| `follow_poll_interval_seconds` | 0 to 604800 | 21600 | Seconds between follow-poll ticks; 0 pauses it. |
| `follow_max_submissions_per_tick` | 1 to 20 | 3 | Episodes one followed show may submit per tick. |
| `follow_delay_hours` | 0 to 168 | 12 | Hours after an episode's air date (start of that day, UTC) before a follow fetches it. |
| `follow_minimum_seeders` | 0 to 1000 | 50 | Seeder floor passed to the downloader's automatic pick. |
| `follow_pack_minimum_seeders` | 0 to 1000 | 20 | Seeder floor for a season pack pick. |
| `follow_pack_timeout_seconds` | 5 to 120 | 30 | Search timeout for a season pack pick. |
| `follow_pack_retry_timeout_seconds` | 5 to 120 | 90 | Timeout when the user retries a missing pack with a longer search. |
| `follow_pack_retry_minimum_seeders` | 0 to 1000 | 5 | Seeder floor when the user retries a missing pack with fewer seeders. |

## Follow poll and the Discord notice

`services/follow.py` is the second loop beside the health poll. Every tick it
walks the unpaused followed shows: fetches the show view (`GET /shows/{id}`),
keeps the episodes on or after the start point that aired at least
`follow_delay_hours` ago, are not in the library, not queued and never
submitted, and groups them by season. A complete season (every listed episode
aired at least the delay ago) from which nothing is in the library, queued or
submitted is asked for as one season pack (`pick` with `season` only, the
`follow_pack_*` profile); a found pack is one season job and a submission row
for every episode of the season, so deleting or redoing it covers the whole
season. A missing pack parks the season as `pack_not_found` in `follow_season`
and posts a notice; the user chooses on the Watchlist (see the decision
endpoint) and the next tick runs that one attempt. Every other season is asked
for episode by episode in air order with `follow_minimum_seeders`. Each
candidate is submitted exactly as `POST /download` would. `NO_CANDIDATE` moves
on; a downloader error stops that show until the next tick. When
`DISCORD_NOTIFY_WEBHOOK_URL` is set, every submission posts
`Following <title>: submitted S02E05 (<release name>)` (or `S02` for a pack)
to that channel webhook, and a missing pack posts
`Following <title>: no season pack found for S02; choose how to continue on the Watchlist`.
Unset means no notice; see the workspace [secrets map](../docs/secrets.md).

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
