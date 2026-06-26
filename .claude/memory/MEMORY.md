# Memory Index - medialab-orchestrator

Repo-local memory for sessions started inside `medialab-orchestrator/`. The
workspace root (`../`) is the source of truth - read its `CLAUDE.md` and
`medialab-orchestrator-spec.md` first.

- [Source of Truth](source-of-truth.md) - root workspace governs design/roadmap; spec is frozen draft
- [Service Identity](service-identity.md) - front-door gateway, SQLite job spine, downstream workers
- [Implementation Decisions](impl-decisions.md) - webhook keyed, read-through not poll, PTN season-only
- [Build State](build-state.md) - what is done vs remaining on the MVP
