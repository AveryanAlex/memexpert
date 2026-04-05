"""Health check routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Shallow service health response."""

    status: Literal["ok"] = "ok"


router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Shallow health check")
async def healthcheck() -> HealthResponse:
    """Return process liveness without probing infrastructure dependencies."""

    return HealthResponse()


__all__ = ["HealthResponse", "router"]
