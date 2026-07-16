# ruff: noqa: TC001,TC003
"""Cookie-admin routes for versioned Meilisearch synonym management."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status

from memexpert.api.dependencies import AdminUserDep, DbSessionDep
from memexpert.core.config import get_settings
from memexpert.models.enums import SearchSynonymLocale
from memexpert.schemas.search_synonyms import (
    SearchSynonymCatalogRead,
    SearchSynonymDraftUpdateRequest,
    SearchSynonymMutationRequest,
    SearchSynonymPublishRequest,
    SearchSynonymResetRequest,
    SearchSynonymSyncRetryRequest,
    SearchSynonymSyncStateRead,
)
from memexpert.services.admin_search_synonyms import (
    AdminSearchSynonymConflictError,
    AdminSearchSynonymDestructiveChangeError,
    AdminSearchSynonymNotFoundError,
    AdminSearchSynonymPublishValidationError,
    AdminSearchSynonymSeedUnavailableError,
    AdminSearchSynonymService,
)

router = APIRouter(prefix="/admin/search-synonyms", tags=["admin search synonyms"])


def get_admin_search_synonym_service(session: DbSessionDep) -> AdminSearchSynonymService:
    return AdminSearchSynonymService(
        session,
        index_name=get_settings().pipeline_meilisearch_index_name,
    )


AdminSearchSynonymServiceDep = Annotated[
    AdminSearchSynonymService,
    Depends(get_admin_search_synonym_service),
]


def _map_admin_search_synonym_error(
    exc: (
        AdminSearchSynonymNotFoundError
        | AdminSearchSynonymConflictError
        | AdminSearchSynonymPublishValidationError
        | AdminSearchSynonymDestructiveChangeError
        | AdminSearchSynonymSeedUnavailableError
    ),
) -> HTTPException:
    if isinstance(exc, AdminSearchSynonymNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, AdminSearchSynonymPublishValidationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": str(exc),
                "validation": exc.validation.model_dump(mode="json"),
            },
        )
    if isinstance(exc, AdminSearchSynonymDestructiveChangeError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "previous_key_count": exc.previous_key_count,
                "new_key_count": exc.new_key_count,
                "reduction_fraction": exc.reduction_fraction,
            },
        )
    if isinstance(exc, AdminSearchSynonymSeedUnavailableError):
        return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/sync", response_model=SearchSynonymSyncStateRead, summary="Read synonym sync state")
async def read_search_synonym_sync_state(
    _admin: AdminUserDep,
    synonym_service: AdminSearchSynonymServiceDep,
) -> SearchSynonymSyncStateRead:
    try:
        return await synonym_service.get_sync_state()
    except AdminSearchSynonymNotFoundError as exc:
        raise _map_admin_search_synonym_error(exc) from exc


@router.post(
    "/sync/retry",
    response_model=SearchSynonymSyncStateRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request durable synonym reconciliation",
)
async def retry_search_synonym_sync(
    admin_user: AdminUserDep,
    synonym_service: AdminSearchSynonymServiceDep,
    payload: Annotated[SearchSynonymSyncRetryRequest, Body()],
) -> SearchSynonymSyncStateRead:
    try:
        return await synonym_service.retry_sync(
            admin_user_id=admin_user.id,
            request_id=payload.request_id,
            version=payload.version,
            reason=payload.reason,
        )
    except (AdminSearchSynonymNotFoundError, AdminSearchSynonymConflictError) as exc:
        raise _map_admin_search_synonym_error(exc) from exc


@router.get(
    "/{locale}",
    response_model=SearchSynonymCatalogRead,
    summary="Read one locale synonym catalog",
)
async def read_search_synonym_catalog(
    _admin: AdminUserDep,
    synonym_service: AdminSearchSynonymServiceDep,
    locale: Annotated[SearchSynonymLocale, Path()],
) -> SearchSynonymCatalogRead:
    try:
        return await synonym_service.get_catalog(locale)
    except AdminSearchSynonymNotFoundError as exc:
        raise _map_admin_search_synonym_error(exc) from exc


@router.put(
    "/{locale}/draft",
    response_model=SearchSynonymCatalogRead,
    summary="Save and validate one locale synonym draft",
)
async def save_search_synonym_draft(
    admin_user: AdminUserDep,
    synonym_service: AdminSearchSynonymServiceDep,
    locale: Annotated[SearchSynonymLocale, Path()],
    payload: Annotated[SearchSynonymDraftUpdateRequest, Body()],
) -> SearchSynonymCatalogRead:
    try:
        return await synonym_service.save_draft(
            admin_user_id=admin_user.id,
            locale=locale,
            request_id=payload.request_id,
            version=payload.version,
            source_text=payload.source_text,
            reason=payload.reason,
        )
    except (AdminSearchSynonymNotFoundError, AdminSearchSynonymConflictError) as exc:
        raise _map_admin_search_synonym_error(exc) from exc


@router.post(
    "/{locale}/draft/import-seed",
    response_model=SearchSynonymCatalogRead,
    summary="Replace a draft with the bundled research seed",
)
async def import_search_synonym_seed(
    admin_user: AdminUserDep,
    synonym_service: AdminSearchSynonymServiceDep,
    locale: Annotated[SearchSynonymLocale, Path()],
    payload: Annotated[SearchSynonymMutationRequest, Body()],
) -> SearchSynonymCatalogRead:
    try:
        return await synonym_service.import_seed(
            admin_user_id=admin_user.id,
            locale=locale,
            request_id=payload.request_id,
            version=payload.version,
            reason=payload.reason,
        )
    except (
        AdminSearchSynonymNotFoundError,
        AdminSearchSynonymConflictError,
        AdminSearchSynonymSeedUnavailableError,
    ) as exc:
        raise _map_admin_search_synonym_error(exc) from exc


@router.post(
    "/{locale}/draft/publish",
    response_model=SearchSynonymCatalogRead,
    summary="Publish a validated synonym revision",
)
async def publish_search_synonym_draft(
    admin_user: AdminUserDep,
    synonym_service: AdminSearchSynonymServiceDep,
    locale: Annotated[SearchSynonymLocale, Path()],
    payload: Annotated[SearchSynonymPublishRequest, Body()],
) -> SearchSynonymCatalogRead:
    try:
        return await synonym_service.publish_draft(
            admin_user_id=admin_user.id,
            locale=locale,
            request_id=payload.request_id,
            version=payload.version,
            reason=payload.reason,
            confirm_destructive=payload.confirm_destructive,
        )
    except (
        AdminSearchSynonymNotFoundError,
        AdminSearchSynonymConflictError,
        AdminSearchSynonymPublishValidationError,
        AdminSearchSynonymDestructiveChangeError,
    ) as exc:
        raise _map_admin_search_synonym_error(exc) from exc


@router.post(
    "/{locale}/draft/reset",
    response_model=SearchSynonymCatalogRead,
    summary="Restore an immutable revision into the mutable draft",
)
async def reset_search_synonym_draft(
    admin_user: AdminUserDep,
    synonym_service: AdminSearchSynonymServiceDep,
    locale: Annotated[SearchSynonymLocale, Path()],
    payload: Annotated[SearchSynonymResetRequest, Body()],
) -> SearchSynonymCatalogRead:
    try:
        return await synonym_service.reset_draft(
            admin_user_id=admin_user.id,
            locale=locale,
            request_id=payload.request_id,
            version=payload.version,
            reason=payload.reason,
            revision_id=payload.revision_id,
        )
    except (AdminSearchSynonymNotFoundError, AdminSearchSynonymConflictError) as exc:
        raise _map_admin_search_synonym_error(exc) from exc


__all__ = ["router"]
