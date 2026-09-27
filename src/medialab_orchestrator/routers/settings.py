"""Settings router: the suite's runtime settings, keyed by service. The
orchestrator's own are handled locally; torrent-downloader's are relayed."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi import status as fastapi_status
from fastapi.responses import JSONResponse
from medialab_contracts import SettingsResponse, SettingUpdate, SettingView, SuiteSettingsResponse

from medialab_orchestrator.core.deps import AppContext, get_context
from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.core.limiter import RATE_LIMIT_DEFAULT, limiter
from medialab_orchestrator.core.settings import SERVICE_NAME, UnknownSettingError, runtime_settings
from medialab_orchestrator.schemas.errors import ErrorResponse

router = APIRouter(prefix="/settings", tags=["Settings"])

DOWNLOADER_SERVICE_NAME = "torrent-downloader"
_STATUS_SUCCESS = "success"
_ERROR_STATUS_KEY = "status"
_ERROR_DETAIL_KEY = "detail"
_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    404: {"model": ErrorResponse, "description": "Unknown service or setting."},
    422: {"model": ErrorResponse, "description": "Value out of bounds or wrong type."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
    502: {"model": ErrorResponse, "description": "Downstream worker unavailable."},
}


def _not_found(detail: str) -> AppException:
    return AppException(
        status_code=fastapi_status.HTTP_404_NOT_FOUND, code=ErrorCode.INVALID_INPUT, detail=detail
    )


def _relayed(body: Any) -> SettingView | JSONResponse:
    """A downloader reply: its SettingView, or its error passed through with
    the status it chose (404 unknown key, 422 out of bounds)."""
    if isinstance(body, dict) and body.get(_ERROR_STATUS_KEY) == "error":
        detail = str(body.get(_ERROR_DETAIL_KEY, ""))
        status = (
            404 if "Unknown setting" in detail else fastapi_status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        return JSONResponse(
            status_code=status,
            content={"status": "error", "code": ErrorCode.INVALID_INPUT.value, "detail": detail},
        )
    return SettingView.model_validate(body)


@router.get(
    "",
    response_model=SuiteSettingsResponse,
    summary="Every service's runtime settings.",
    responses=_RESPONSES,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def list_settings(
    request: Request, ctx: AppContext = Depends(get_context)
) -> SuiteSettingsResponse:
    downloader = SettingsResponse.model_validate(await ctx.torrent.settings())
    return SuiteSettingsResponse(
        status=_STATUS_SUCCESS,
        services={
            DOWNLOADER_SERVICE_NAME: downloader.settings,
            SERVICE_NAME: runtime_settings.views(),
        },
    )


@router.put(
    "/{service}/{key}",
    response_model=SettingView,
    summary="Override one setting on the named service.",
    responses=_RESPONSES,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def set_setting(
    request: Request,
    service: str,
    key: str,
    payload: SettingUpdate,
    ctx: AppContext = Depends(get_context),
) -> SettingView | JSONResponse:
    if service == DOWNLOADER_SERVICE_NAME:
        return _relayed(await ctx.torrent.set_setting(key, payload.value))
    if service != SERVICE_NAME:
        raise _not_found(f"Unknown service: {service}")
    try:
        return runtime_settings.set(key, payload.value)
    except UnknownSettingError as error:
        raise _not_found(f"Unknown setting: {key}") from error
    except ValueError as error:
        raise AppException(
            status_code=fastapi_status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ErrorCode.INVALID_INPUT,
            detail=str(error),
        ) from error


@router.delete(
    "/{service}/{key}",
    response_model=SettingView,
    summary="Drop the override on the named service.",
    responses=_RESPONSES,
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def reset_setting(
    request: Request, service: str, key: str, ctx: AppContext = Depends(get_context)
) -> SettingView | JSONResponse:
    if service == DOWNLOADER_SERVICE_NAME:
        return _relayed(await ctx.torrent.reset_setting(key))
    if service != SERVICE_NAME:
        raise _not_found(f"Unknown service: {service}")
    try:
        return runtime_settings.reset(key)
    except UnknownSettingError as error:
        raise _not_found(f"Unknown setting: {key}") from error
