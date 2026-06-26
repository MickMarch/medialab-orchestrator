---
name: source-of-truth
description: workspace root governs orchestrator design/roadmap; spec is the frozen design draft
metadata:
  type: project
---

The workspace **root** (`../` from this repo, i.e. `medialab/`) is the source of
truth for this service. Read `medialab/CLAUDE.md` (shared conventions,
engineering standards, roadmap order) and `medialab/medialab-orchestrator-spec.md`
(the frozen design draft, 2026-06-26) before any design decision. When this
repo's own `CLAUDE.md` disagrees with the root, the root wins.

This repo is a submodule pinned in the root like the other three services
(torrent-downloader, medialab-bot, medialab-jellyfin, medialab-contracts). It is
roadmap item 6, built after its prerequisites cleared (medialab-contracts
v0.2.0, torrent-downloader v1.2.0).

**How to apply:** A session started inside this directory must not treat this
`CLAUDE.md` as complete - cross-check the root spec for the full design (job
state machine, idempotency rules, cross-service sequencing).
