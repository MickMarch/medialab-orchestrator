"""Discord notices for follow submissions: one webhook POST, never raising.

The only httpx use outside ``clients/``: a webhook is not a medialab service
with a key and an error envelope, so the downstream client base does not fit.
"""

from __future__ import annotations

import httpx

from medialab_orchestrator.core.logger import app_logger

_TIMEOUT_SECONDS = 10.0
_CONTENT_KEY = "content"


def follow_notice(title: str, code: str, release_name: str) -> str:
    return f"Following {title}: submitted {code} ({release_name})"


async def post_discord(webhook_url: str, content: str) -> None:
    """Post ``content`` to the channel webhook. A failure is logged and dropped;
    a notice must never fail the submission it reports."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(webhook_url, json={_CONTENT_KEY: content})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        app_logger.warning("Discord notice failed: %s", exc)


def pack_not_found_notice(title: str, season_code: str) -> str:
    return (
        f"Following {title}: no season pack found for {season_code}; "
        "choose how to continue on the Watchlist"
    )
