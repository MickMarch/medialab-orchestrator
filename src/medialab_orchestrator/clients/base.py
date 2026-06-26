"""Shared async HTTP client base for downstream worker services.

Every downstream call goes through one of these. A transport or HTTP error from
a worker surfaces as a single ``AppException(DOWNSTREAM_UNAVAILABLE)`` so the
gateway never leaks a raw httpx error to the bot, and the worker advancing a job
can catch one exception type and mark the job FAILED.
"""

from __future__ import annotations

from typing import Any

import httpx

from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.core.logger import app_logger

_API_KEY_HEADER = "X-API-Key"
_DEFAULT_TIMEOUT_SECONDS = 30.0


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
        self._headers = {_API_KEY_HEADER: api_key} if api_key else {}
        self._timeout = timeout_seconds

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method, url, params=params, json=json, headers=self._headers
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
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

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, *, json: dict[str, Any] | None = None) -> Any:
        return await self.request("POST", path, json=json)

    async def is_reachable(self) -> bool:
        """Probe the downstream ``/api/v1/health`` endpoint for the gateway's
        aggregated health signal. Never raises - returns False on any failure."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(f"{self._base_url}/api/v1/health")
            return response.status_code == httpx.codes.OK
        except httpx.HTTPError:
            return False
