"""Version 1 API namespace."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from memexpert.api.routes.v1.admin import router as admin_router
from memexpert.api.routes.v1.admin_analytics import router as admin_analytics_router
from memexpert.api.routes.v1.admin_recovery import router as admin_recovery_router
from memexpert.api.routes.v1.admin_search_synonyms import router as admin_search_synonyms_router
from memexpert.api.routes.v1.analytics import router as analytics_router
from memexpert.api.routes.v1.auth import router as auth_router
from memexpert.api.routes.v1.collections import router as collections_router
from memexpert.api.routes.v1.crawler import router as crawler_router
from memexpert.api.routes.v1.media import router as media_router
from memexpert.api.routes.v1.memes import router as memes_router
from memexpert.api.routes.v1.pipeline import router as pipeline_router
from memexpert.api.routes.v1.seo import router as seo_router


class VersionNamespaceResponse(BaseModel):
    """Availability response for the versioned API namespace root."""

    version: Literal["v1"] = "v1"
    status: Literal["available"] = "available"


router = APIRouter(prefix="/api/v1", tags=["v1"])
router.include_router(admin_router)
router.include_router(admin_analytics_router)
router.include_router(admin_recovery_router)
router.include_router(admin_search_synonyms_router)
router.include_router(analytics_router)
router.include_router(auth_router)
router.include_router(collections_router)
router.include_router(media_router)
router.include_router(memes_router)
router.include_router(pipeline_router)
router.include_router(crawler_router)
router.include_router(seo_router)


@router.get("/", response_model=VersionNamespaceResponse, summary="Versioned API namespace")
async def api_v1_root() -> VersionNamespaceResponse:
    """Expose the versioned namespace alongside mounted feature routers."""

    return VersionNamespaceResponse()


__all__ = ["VersionNamespaceResponse", "router"]
