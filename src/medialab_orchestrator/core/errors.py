"""Application error codes and base exception for structured error responses."""

from enum import Enum

from medialab_contracts import CommonErrorCode


class ErrorCode(str, Enum):
    """Service error codes: the shared CommonErrorCode set plus service-specific
    codes. Shared values are sourced from CommonErrorCode so a rename there
    surfaces in the superset test."""

    UNAUTHORIZED = CommonErrorCode.UNAUTHORIZED.value
    RATE_LIMITED = CommonErrorCode.RATE_LIMITED.value
    INVALID_INPUT = CommonErrorCode.INVALID_INPUT.value
    INTERNAL_ERROR = CommonErrorCode.INTERNAL_ERROR.value
    PATH_NOT_FOUND = CommonErrorCode.PATH_NOT_FOUND.value
    PERMISSION_DENIED = CommonErrorCode.PERMISSION_DENIED.value
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    WATCHLIST_ITEM_NOT_FOUND = "WATCHLIST_ITEM_NOT_FOUND"
    DOWNSTREAM_UNAVAILABLE = "DOWNSTREAM_UNAVAILABLE"
    EPISODE_UNPARSEABLE = "EPISODE_UNPARSEABLE"
    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    RENAME_INCOMPLETE = "RENAME_INCOMPLETE"
    # POST /jobs/{id}/redo: only a DONE job, or a flagged job that placed
    # nothing, can be redone.
    JOB_NOT_DONE = "JOB_NOT_DONE"
    # POST /jobs/{id}/dismiss: only a FAILED or NEEDS_ATTENTION job.
    JOB_NOT_DISMISSABLE = "JOB_NOT_DISMISSABLE"
    # POST /jobs/{id}/retry: a DISMISSED or DELETED job is closed.
    JOB_NOT_RETRYABLE = "JOB_NOT_RETRYABLE"
    # POST /jobs/{id}/redo: the old job's deletion failed; the replacement
    # row exists and the old job is untouched, so the redo can be retried.
    REDO_DELETION_FAILED = "REDO_DELETION_FAILED"
    # Relayed from torrent-downloader when TMDB is unconfigured or unreachable.
    TMDB_UNAVAILABLE = "TMDB_UNAVAILABLE"
    # Relayed from torrent-downloader when the details page a download points
    # at could not be fetched; the request was valid and a retry may succeed.
    SOURCE_UNREACHABLE = "SOURCE_UNREACHABLE"


class AppException(Exception):
    def __init__(self, status_code: int, code: ErrorCode, detail: str) -> None:
        self.status_code = status_code
        self.code = code
        self.detail = detail
