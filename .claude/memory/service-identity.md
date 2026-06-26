---
name: service-identity
description: orchestrator is a front-door gateway with a SQLite job spine; downstream services are workers
metadata:
  type: project
---

medialab-orchestrator is a **front-door orchestrating gateway**, not a
post-download relay. The Discord bot talks to exactly ONE service - this one. It
brokers the whole lifecycle (search -> download -> stop-seed -> rename ->
register -> scan) and fans out to downstream worker services
(torrent-downloader, medialab-jellyfin) that are never client-facing.

The **SQLite `pipeline_job` table is the spine**: one row per torrent, advanced
one state at a time by an in-process asyncio worker, persisted after each
transition so a restart resumes from the last committed state. That persistence
+ observability (`GET /jobs`) + retry is what makes it an orchestrator, not a
relay.

Restraint is deliberate (portfolio signal): SQLite over Postgres, in-process
asyncio worker over Celery/Redis, no message broker - lightest things that fit
single-host homelab scale. The scale-up path is documented, not built.

**How to apply:** Do not add a gateway endpoint that is a pure forward touching
no job state and adding no gateway value. Search is the sole accepted stateless
exception (kept so the bot has one dependency). Every stateful endpoint binds a
job record.
