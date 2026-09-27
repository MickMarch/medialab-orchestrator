"""Response schemas for the watchlist routes that have no shared contract."""

from pydantic import BaseModel


class FollowCheckResponse(BaseModel):
    """Returned from ``POST /watchlist/show/{tmdb_id}/follow/check``."""

    submitted: list[str]
    """Episode codes (``S02E05``) submitted by this check, in air order."""
