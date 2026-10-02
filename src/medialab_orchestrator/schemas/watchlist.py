"""Response schemas for the watchlist routes that have no shared contract."""

from medialab_contracts import SeasonFollowState, ShowBrowseResponse
from pydantic import BaseModel


class FollowCheckResponse(BaseModel):
    """Returned from ``POST /watchlist/show/{tmdb_id}/follow/check``."""

    submitted: list[str]
    """Episode codes (``S02E05``) submitted by this check, in air order."""


class FollowedShowResponse(ShowBrowseResponse):
    """``GET /watchlist/show/{tmdb_id}/episodes``: the annotated show view plus
    the per-season follow state for every season that has one."""

    seasons_follow: list[SeasonFollowState] = []
