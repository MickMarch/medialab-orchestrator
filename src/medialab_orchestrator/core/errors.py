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
    DOWNSTREAM_UNAVAILABLE = "DOWNSTREAM_UNAVAILABLE"
    SEASON_UNPARSEABLE = "SEASON_UNPARSEABLE"


class AppException(Exception):
    def __init__(self, status_code: int, code: ErrorCode, detail: str) -> None:
        self.status_code = status_code
        self.code = code
        self.detail = detail
