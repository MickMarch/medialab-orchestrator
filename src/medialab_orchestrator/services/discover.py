"""Annotating discover results and wishlist items with wishlist and library state.

The library lookup is best effort: any failure is logged and treated as an
empty library, so the "In Jellyfin" badge never blocks discovery.
"""

from __future__ import annotations

from medialab_contracts import DiscoverResponse, MediaType, WishlistItem

from medialab_orchestrator.clients import JellyfinClient
from medialab_orchestrator.core.logger import app_logger


async def library_tmdb_ids(jellyfin: JellyfinClient, media_type: MediaType) -> set[int]:
    """The TMDB ids in the Jellyfin library for ``media_type``, or empty on failure."""
    try:
        response = await jellyfin.library_tmdb_ids(media_type)
    except Exception as exc:
        # Broad on purpose: the badge is decoration and must never fail the list.
        app_logger.warning("Library lookup for %s failed: %s", media_type.value, exc)
        return set()
    return set(response.tmdb_ids)


def annotate_discover(
    response: DiscoverResponse,
    *,
    media_type: MediaType,
    wishlisted: set[int],
    in_library: set[int],
) -> DiscoverResponse:
    """Set ``on_wishlist`` and ``in_library`` on items of ``media_type`` by TMDB id."""
    items = [
        item.model_copy(
            update={
                "on_wishlist": item.media_type is media_type and item.tmdb_id in wishlisted,
                "in_library": item.media_type is media_type and item.tmdb_id in in_library,
            }
        )
        for item in response.items
    ]
    return response.model_copy(update={"items": items})


async def annotate_wishlist(
    items: list[WishlistItem], jellyfin: JellyfinClient
) -> list[WishlistItem]:
    """Set ``in_library`` with one library lookup per media type present."""
    present = list(dict.fromkeys(item.media_type for item in items))
    libraries = {media_type: await library_tmdb_ids(jellyfin, media_type) for media_type in present}
    return [
        item.model_copy(update={"in_library": item.tmdb_id in libraries[item.media_type]})
        for item in items
    ]
