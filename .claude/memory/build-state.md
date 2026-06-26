---
name: build-state
description: orchestrator MVP build progress - job store done, worker/endpoints/webhook/rename remaining
metadata:
  type: project
---

MVP build on branch `feat/orchestrator-mvp` (root repo), started 2026-06-26.

**Done:** scaffold with full engineering standards from commit one (ruff, mypy,
contracts v0.2.0 git-pin); `core/config.py` (AppConfig - gateway key, two
downstream URL+keys, `MEDIA_MOUNT_PATH`, `DB_PATH`, all import-optional);
`core/errors.py` (`ErrorCode` = `CommonErrorCode` superset + `JOB_NOT_FOUND`,
`DOWNSTREAM_UNAVAILABLE`, `SEASON_UNPARSEABLE`); the SQLite job store
(`store/jobs.py`: `JobStatus` 9-state enum, `PipelineJob` model, `JobStore`
create/get/list/update + lowercase-hash + update whitelist + persistence) with
16 passing tests over an in-memory DB fixture. ruff + mypy green.

**Remaining MVP:** asyncio worker (FastAPI lifespan, advances jobs one step,
idempotent steps, forward-retry); downstream `httpx` clients (torrent-downloader,
medialab-jellyfin) mocked at boundary in tests; bot-facing routers (search
proxies, `POST /download`, `GET /transfers` merge, `GET/POST /jobs*`,
`GET /storage`, health); `POST /webhooks/torrent-complete` + `scripts/
notify_complete.py` relay; TV-folder rename (`Series (Year)/Season NN/`, PTN
season-only); auth/limiter/middleware/logger cores mirrored from
torrent-downloader; CI workflow, pre-commit, CHANGELOG, dependabot.

**Repo is local-only** - not yet pushed to GitHub. Needs a PUBLIC GitHub repo
(the contracts git-ref dep must resolve unauthenticated in CI/Docker). Submodule
wiring into the root + root `docker-compose.yml` + spec-move into this CLAUDE.md
+ roadmap mark are pending. See [[impl-decisions]], [[source-of-truth]].

**How to apply:** Update or delete this memory as the MVP lands; it tracks
transient progress, not durable design.
