"""Version 1 API namespace."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel


class VersionNamespaceResponse(BaseModel):
    """Placeholder response for the versioned API namespace root."""

    version: Literal["v1"] = "v1"
    status: Literal["available"] = "available"


router = APIRouter(prefix="/api/v1", tags=["v1"])


@router.get("/", response_model=VersionNamespaceResponse, summary="Versioned API namespace")
async def api_v1_root() -> VersionNamespaceResponse:
    """Expose the versioned namespace so it appears in OpenAPI before feature routes land."""

    return VersionNamespaceResponse()


__all__ = ["VersionNamespaceResponse", "router"]
