"""Shared async HTTP client base for downstream worker services.

Every downstream call goes through one of these. A transport or HTTP error from
a worker surfaces as a single ``AppException(DOWNSTREAM_UNAVAILABLE)`` so the
gateway never leaks a raw httpx error to the bot, and the worker advancing a job
can catch one exception type and mark the job FAILED.
"""

from __future__ import annotations

from typing import Any

import httpx
from medialab_contracts import API_KEY_HEADER, HEALTH_PATH

from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.core.logger import app_logger

_DEFAULT_TIMEOUT_SECONDS = 30.0
_ERROR_CODE_KEY = "code"
_ERROR_DETAIL_KEY = "detail"
_NO_RELAY: frozenset[ErrorCode] = frozenset()


def relayed_error(status_code: int, body: Any, relay: frozenset[ErrorCode]) -> AppException | None:
    """The downstream error re-raised with its own status and code when its
    ``code`` is in ``relay``; ``None`` means map it to DOWNSTREAM_UNAVAILABLE."""
    if not isinstance(body, dict):
        return None
    relayed = {code.value: code for code in relay}.get(str(body.get(_ERROR_CODE_KEY)))
    if relayed is None:
        return None
    return AppException(
        status_code=status_code, code=relayed, detail=str(body.get(_ERROR_DETAIL_KEY, ""))
    )


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


class DownstreamClient:
    """Thin async wrapper over httpx for one downstream service.

    ``base_url`` and ``api_key`` come from config at construction. Callers use
    ``request`` (or the ``get``/``post`` helpers) and get parsed JSON back, or an
    ``AppException`` mapped from any failure.
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._name = name
        self._base_url = base_url.rstrip("/")
        self._headers = {API_KEY_HEADER: api_key} if api_key else {}
        self._timeout = timeout_seconds

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        accept: tuple[int, ...] = (),
        relay: frozenset[ErrorCode] = _NO_RELAY,
    ) -> Any:
        """``accept`` lists non-2xx statuses that mean "already in the wanted
        state" for an idempotent action; they return ``None`` instead of raising.
        ``relay`` lists downstream error codes surfaced as-is (status and code)
        instead of as DOWNSTREAM_UNAVAILABLE."""
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method, url, params=params, json=json, headers=self._headers
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in accept:
                return None
            if relay:
                passthrough = relayed_error(
                    exc.response.status_code, _json_or_none(exc.response), relay
                )
                if passthrough is not None:
                    raise passthrough from exc
            app_logger.warning(
                "%s returned %d for %s %s",
                self._name,
                exc.response.status_code,
                method,
                path,
            )
            raise AppException(
                status_code=502,
                code=ErrorCode.DOWNSTREAM_UNAVAILABLE,
                detail=f"{self._name} returned {exc.response.status_code}.",
            ) from exc
        except httpx.HTTPError as exc:
            app_logger.warning("%s unreachable for %s %s: %s", self._name, method, path, exc)
            raise AppException(
                status_code=502,
                code=ErrorCode.DOWNSTREAM_UNAVAILABLE,
                detail=f"{self._name} is unreachable.",
            ) from exc
        if not response.content:
            return None
        return response.json()

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        relay: frozenset[ErrorCode] = _NO_RELAY,
    ) -> Any:
        return await self.request("GET", path, params=params, relay=relay)

    async def post(self, path: str, *, json: dict[str, Any] | None = None) -> Any:
        return await self.request("POST", path, json=json)

    async def put(self, path: str, *, json: dict[str, Any] | None = None) -> Any:
        return await self.request("PUT", path, json=json, accept=(404, 422))

    async def delete(self, path: str, *, accept: tuple[int, ...] = ()) -> Any:
        return await self.request("DELETE", path, accept=accept)

    async def is_reachable(self) -> bool:
        """Probe the downstream ``/api/v1/health`` endpoint for the gateway's
        aggregated health signal. Never raises - returns False on any failure."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(f"{self._base_url}{HEALTH_PATH}")
            return response.status_code == httpx.codes.OK
        except httpx.HTTPError:
            return False
