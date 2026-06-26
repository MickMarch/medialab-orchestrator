"""Application entry point: FastAPI app, lifespan context, uvicorn launch helpers."""

import importlib.metadata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from medialab_orchestrator.core.auth import verify_api_key
from medialab_orchestrator.core.config import config
from medialab_orchestrator.core.deps import build_context
from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.core.limiter import limiter
from medialab_orchestrator.core.logger import app_logger
from medialab_orchestrator.core.middleware import RequestLoggingMiddleware
from medialab_orchestrator.routers import gateway, search, system, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the long-lived AppContext (store, clients, worker) at startup."""
    app.state.context = build_context()
    app_logger.info("medialab-orchestrator context ready.")
    yield


app: FastAPI = FastAPI(
    title="medialab-orchestrator API",
    version=importlib.metadata.version("medialab-orchestrator"),
    description=(
        "Front-door orchestrating gateway for the medialab media lifecycle. "
        "The Discord bot talks only to this service; it brokers search, download, "
        "status, and the post-download pipeline, fanning out to torrent-downloader "
        "and medialab-jellyfin. A SQLite job table spans the full lifecycle.\n\n"
        "All endpoints except `/api/v1/health` require an `X-API-Key` header."
    ),
    contact={"name": "Michael Marchand", "url": "https://github.com/MickMarch"},
    openapi_tags=[
        {"name": "System", "description": "Aggregated health."},
        {"name": "Search", "description": "Stateless TMDB / torrent search proxies."},
        {"name": "Gateway", "description": "Download submission, transfers, jobs, storage."},
        {"name": "Webhooks", "description": "qBittorrent completion entry point."},
    ],
    lifespan=lifespan,
)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    retry_after: int = exc.limit.limit.GRANULARITY.seconds  # type: ignore[union-attr]
    response = JSONResponse(
        status_code=429,
        content={
            "status": "error",
            "code": ErrorCode.RATE_LIMITED.value,
            "detail": f"Rate limit exceeded. Retry after {retry_after} seconds.",
        },
    )
    response.headers["Retry-After"] = str(retry_after)
    return response


app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "code": exc.code.value, "detail": exc.detail},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"status": "error", "code": ErrorCode.INVALID_INPUT.value, "detail": str(exc)},
    )


# System router stays public (health, no key). The rest require the gateway key.
app.include_router(system.router, prefix="/api/v1")
app.include_router(search.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])
app.include_router(gateway.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])
app.include_router(webhooks.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])


def custom_openapi() -> dict:
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        contact=app.contact,
        tags=app.openapi_tags,
        routes=app.routes,
    )
    for path, methods in schema.get("paths", {}).items():
        for operation in methods.values():
            if path == "/api/v1/health":
                operation["security"] = []
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi  # type: ignore[method-assign]


def main() -> None:
    """Start the production uvicorn server."""
    app_logger.info("Starting medialab-orchestrator API Server...")
    uvicorn.run("medialab_orchestrator.main:app", host=config.api_host, port=config.api_port)


def dev() -> None:
    """Start the uvicorn server with hot-reload enabled for local development."""
    app_logger.info("Starting medialab-orchestrator API Server in DEV MODE...")
    uvicorn.run(
        "medialab_orchestrator.main:app",
        host=config.api_host,
        port=config.api_port,
        reload=True,
    )


if __name__ == "__main__":
    main()
