"""FastAPI application factory for MemeXpert."""

from __future__ import annotations

from fastapi import FastAPI

from memexpert.api.routes.health import router as health_router
from memexpert.api.routes.v1 import router as v1_router


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""

    app = FastAPI(title="MemeXpert API", version="0.1.0")
    app.include_router(health_router)
    app.include_router(v1_router)
    return app


__all__ = ["create_app"]
