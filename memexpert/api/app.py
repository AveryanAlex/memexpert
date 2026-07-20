"""FastAPI application factory for MemeXpert."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from memexpert.api.dependencies import (
    AuthHTTPError,
    PipelineHTTPError,
    auth_http_exception_handler,
    pipeline_http_exception_handler,
)
from memexpert.api.routes.health import router as health_router
from memexpert.api.routes.v1 import router as v1_router
from memexpert.api.security import (
    SecurityHTTPError,
    build_security_cors_middleware_kwargs,
    security_http_exception_handler,
    security_http_middleware,
)
from memexpert.core.config import get_settings
from memexpert.core.qdrant import reset_async_qdrant_state

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@asynccontextmanager
async def _app_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Release process-shared provider transports during graceful shutdown."""

    try:
        yield
    finally:
        await reset_async_qdrant_state()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""

    settings = get_settings()
    app = FastAPI(title="MemeXpert API", version="0.1.0", lifespan=_app_lifespan)
    app.add_exception_handler(AuthHTTPError, auth_http_exception_handler)
    app.add_exception_handler(PipelineHTTPError, pipeline_http_exception_handler)
    app.add_exception_handler(SecurityHTTPError, security_http_exception_handler)
    app.middleware("http")(security_http_middleware)
    cors_middleware_kwargs = build_security_cors_middleware_kwargs(settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_middleware_kwargs["allow_origins"],
        allow_origin_regex=cors_middleware_kwargs["allow_origin_regex"],
        allow_credentials=cors_middleware_kwargs["allow_credentials"],
        allow_methods=cors_middleware_kwargs["allow_methods"],
        allow_headers=cors_middleware_kwargs["allow_headers"],
    )
    app.include_router(health_router)
    app.include_router(v1_router)
    return app


__all__ = ["create_app"]
