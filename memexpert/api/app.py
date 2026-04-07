"""FastAPI application factory for MemeXpert."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from memexpert.api.dependencies import AuthHTTPError, auth_http_exception_handler
from memexpert.api.routes.health import router as health_router
from memexpert.api.routes.v1 import router as v1_router
from memexpert.api.security import (
    SecurityHTTPError,
    build_security_cors_middleware_kwargs,
    security_http_exception_handler,
    security_http_middleware,
)
from memexpert.core.config import get_settings


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""

    settings = get_settings()
    app = FastAPI(title="MemeXpert API", version="0.1.0")
    app.add_exception_handler(AuthHTTPError, auth_http_exception_handler)
    app.add_exception_handler(SecurityHTTPError, security_http_exception_handler)
    app.middleware("http")(security_http_middleware)
    app.add_middleware(CORSMiddleware, **build_security_cors_middleware_kwargs(settings))
    app.include_router(health_router)
    app.include_router(v1_router)
    return app


__all__ = ["create_app"]
