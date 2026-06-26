---
name: impl-decisions
description: resolved spec open questions - webhook keyed, DOWNLOADING read-through not poll, PTN season-only
metadata:
  type: project
---

The spec left three open questions for the implementation phase. Resolved
2026-06-26 (user approved these leans):

1. **Webhook auth: keyed.** `POST /webhooks/torrent-complete` requires the
   gateway `X-API-Key` like the rest of `/api/v1`. In the compose network
   localhost is not a trust boundary, so uniform auth beats a special-cased open
   endpoint. `scripts/notify_complete.py` sends the key from its own env.
2. **DOWNLOADING via read-through, not polling.** The gateway does NOT poll
   torrent-downloader `/transfers` to advance DOWNLOAD_SUBMITTED -> DOWNLOADING.
   `GET /transfers` is a live read-through (merge job rows with a one-shot
   downstream read on request); the completion webhook is the event that moves a
   job into the post-download pipeline. Less machinery, no poll loop.
3. **PTN season parsing validated at build.** Verify the season-number parser
   against real release-name samples while writing it. No season parseable ->
   mark job FAILED with a clear `last_error`; operator fixes the folder and
   calls `retry`. No silent half-processing.

Other locked decisions: TMDB id threaded end-to-end (no title guessing; PTN is
season-number ONLY, title/year from TMDB via torrent-downloader). One TMDB key
owner (torrent-downloader). Shared media volume bind-mount, no host shell-out.

**How to apply:** These are decided - do not re-litigate when building the
webhook, worker, or rename. See [[source-of-truth]] for the governing spec.
