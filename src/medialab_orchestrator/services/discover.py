"""Annotating discover results, TMDB search results and watchlist items with
watchlist and library state.

The library lookup is best effort: any failure is logged and treated as an
empty library, so the "In Jellyfin" badge never blocks discovery.
"""

from __future__ import annotations

from typing import Any

from medialab_contracts import DiscoverResponse, MediaType, WatchlistItem, WatchlistKind

from medialab_orchestrator.clients import JellyfinClient
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.store import WatchlistStore

TMDB_MEDIA_TYPES: dict[str, MediaType] = {"movie": MediaType.MOVIE, "tv": MediaType.SHOW}
"""TMDB search ``media_type`` wire values mapped to the suite's MediaType."""

SEARCH_RESULTS_KEY = "data"
SEARCH_MEDIA_TYPE_KEY = "media_type"
SEARCH_TMDB_ID_KEY = "tmdb_id"
ON_WATCHLIST_KEY = "on_watchlist"
WATCHLIST_KIND_KEY = "watchlist_kind"
IN_LIBRARY_KEY = "in_library"

WatchlistKinds = dict[int, WatchlistKind]
"""TMDB id to kind for one media type, as ``WatchlistStore.keys`` returns it."""


async def library_tmdb_ids(jellyfin: JellyfinClient, media_type: MediaType) -> set[int]:
    """The TMDB ids in the Jellyfin library for ``media_type``, or empty on failure."""
    try:
        response = await jellyfin.library_tmdb_ids(media_type)
    except Exception as exc:
        # Broad on purpose: the badge is decoration and must never fail the list.
        app_logger.warning("Library lookup for %s failed: %s", media_type.value, exc)
        return set()
    return set(response.tmdb_ids)


def watchlist_flags(kinds: WatchlistKinds, tmdb_id: int) -> dict[str, Any]:
    """``on_watchlist`` and ``watchlist_kind`` for one title."""
    kind = kinds.get(tmdb_id)
    return {ON_WATCHLIST_KEY: kind is not None, WATCHLIST_KIND_KEY: kind}


def annotate_discover(
    response: DiscoverResponse,
    *,
    media_type: MediaType,
    watchlist: WatchlistKinds,
    in_library: set[int],
) -> DiscoverResponse:
    """Set the watchlist flags and ``in_library`` on items of ``media_type`` by TMDB id."""
    items = [
        item.model_copy(
            update={
                **watchlist_flags(watchlist if item.media_type is media_type else {}, item.tmdb_id),
                IN_LIBRARY_KEY: item.media_type is media_type and item.tmdb_id in in_library,
            }
        )
        for item in response.items
    ]
    return response.model_copy(update={"items": items})


async def annotate_watchlist(
    items: list[WatchlistItem], jellyfin: JellyfinClient
) -> list[WatchlistItem]:
    """Set ``in_library`` with one library lookup per media type present."""
    present = list(dict.fromkeys(item.media_type for item in items))
    libraries = {media_type: await library_tmdb_ids(jellyfin, media_type) for media_type in present}
    return [
        item.model_copy(update={IN_LIBRARY_KEY: item.tmdb_id in libraries[item.media_type]})
        for item in items
    ]


def _search_media_type(result: dict[str, Any]) -> MediaType | None:
    return TMDB_MEDIA_TYPES.get(str(result.get(SEARCH_MEDIA_TYPE_KEY)))


async def annotate_search(
    payload: Any, *, watchlist: WatchlistStore, jellyfin: JellyfinClient
) -> Any:
    """Set the watchlist flags and ``in_library`` on each TMDB search result.

    Results keep their own TMDB ``media_type`` (``tv`` stays ``tv``); lookups
    map it to MediaType. One watchlist and one library lookup per media type
    present; an unmapped type is flagged false.
    """
    if not isinstance(payload, dict):
        return payload
    results: list[dict[str, Any]] = payload.get(SEARCH_RESULTS_KEY) or []
    present = [
        media_type
        for media_type in dict.fromkeys(_search_media_type(result) for result in results)
        if media_type is not None
    ]
    kinds = {media_type: watchlist.keys(media_type) for media_type in present}
    libraries = {media_type: await library_tmdb_ids(jellyfin, media_type) for media_type in present}

    def flags(result: dict[str, Any]) -> dict[str, Any]:
        media_type = _search_media_type(result)
        tmdb_id = result.get(SEARCH_TMDB_ID_KEY)
        if media_type is None or not isinstance(tmdb_id, int):
            return {**watchlist_flags({}, 0), IN_LIBRARY_KEY: False}
        kind = kinds[media_type].get(tmdb_id)
        return {
            ON_WATCHLIST_KEY: kind is not None,
            WATCHLIST_KIND_KEY: kind.value if kind is not None else None,
            IN_LIBRARY_KEY: tmdb_id in libraries[media_type],
        }

    return {**payload, SEARCH_RESULTS_KEY: [{**result, **flags(result)} for result in results]}
