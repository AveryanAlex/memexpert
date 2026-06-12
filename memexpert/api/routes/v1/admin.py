# ruff: noqa: TC001,TC003
"""Cookie-authenticated browser-admin routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status

from memexpert.api.dependencies import AdminUserDep, DbSessionDep
from memexpert.models.enums import ChannelSuggestionStatus, ModerationReportStatus
from memexpert.schemas.admin import (
    AdminChannelSuggestionReviewRequest,
    AdminMemeDeleteRequest,
    AdminMemeDestructiveActionRead,
    AdminMemeDetailRead,
    AdminMemeMergeRequest,
    AdminMemeModerationUpdateRequest,
    AdminMemeRead,
    AdminMemeTemplateRead,
    AdminMemeTemplateUpdateRequest,
    AdminModerationDecisionRead,
    AdminModerationReportRead,
    AdminModerationReportResolveRequest,
    AdminSessionRead,
    AdminSourceChannelCreateRequest,
    AdminSourceChannelRead,
)
from memexpert.schemas.user import ChannelSuggestionRead
from memexpert.services.admin import AdminConflictError, AdminNotFoundError, AdminService

router = APIRouter(prefix="/admin", tags=["admin"])


def get_admin_service(session: DbSessionDep) -> AdminService:
    """Build the admin service for the current request session."""

    return AdminService(session=session)


AdminServiceDep = Annotated[AdminService, Depends(get_admin_service)]


def _map_admin_error(exc: AdminNotFoundError | AdminConflictError) -> HTTPException:
    if isinstance(exc, AdminNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/session", response_model=AdminSessionRead, summary="Read current admin session")
async def read_admin_session(admin_user: AdminUserDep) -> AdminSessionRead:
    return AdminSessionRead(user=admin_user)


@router.get(
    "/channel-suggestions",
    response_model=list[ChannelSuggestionRead],
    summary="List user-submitted channel suggestions",
)
async def list_channel_suggestions(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    suggestion_status: Annotated[ChannelSuggestionStatus | None, Query(alias="status")] = None,
) -> list[ChannelSuggestionRead]:
    return await admin_service.list_channel_suggestions(status=suggestion_status)


@router.post(
    "/channel-suggestions/{suggestion_id}/approve",
    response_model=ChannelSuggestionRead,
    summary="Approve a channel suggestion",
)
async def approve_channel_suggestion(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    suggestion_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminChannelSuggestionReviewRequest | None, Body()] = None,
) -> ChannelSuggestionRead:
    try:
        return await admin_service.review_channel_suggestion(
            suggestion_id,
            status=ChannelSuggestionStatus.APPROVED,
            admin_note=request.admin_note if request else None,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/channel-suggestions/{suggestion_id}/reject",
    response_model=ChannelSuggestionRead,
    summary="Reject a channel suggestion",
)
async def reject_channel_suggestion(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    suggestion_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminChannelSuggestionReviewRequest | None, Body()] = None,
) -> ChannelSuggestionRead:
    try:
        return await admin_service.review_channel_suggestion(
            suggestion_id,
            status=ChannelSuggestionStatus.REJECTED,
            admin_note=request.admin_note if request else None,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.get("/source-channels", response_model=list[AdminSourceChannelRead], summary="List source channels")
async def list_source_channels(_admin: AdminUserDep, admin_service: AdminServiceDep) -> list[AdminSourceChannelRead]:
    return await admin_service.list_source_channels()


@router.post(
    "/source-channels",
    response_model=AdminSourceChannelRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a source channel",
)
async def add_source_channel(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    request: Annotated[AdminSourceChannelCreateRequest, Body()],
) -> AdminSourceChannelRead:
    try:
        return await admin_service.add_source_channel(request)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/source-channels/{channel_id}/pause",
    response_model=AdminSourceChannelRead,
    summary="Pause a source channel",
)
async def pause_source_channel(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    channel_id: Annotated[uuid.UUID, Path()],
) -> AdminSourceChannelRead:
    try:
        return await admin_service.set_source_channel_paused(channel_id, is_paused=True)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/source-channels/{channel_id}/resume",
    response_model=AdminSourceChannelRead,
    summary="Resume a source channel",
)
async def resume_source_channel(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    channel_id: Annotated[uuid.UUID, Path()],
) -> AdminSourceChannelRead:
    try:
        return await admin_service.set_source_channel_paused(channel_id, is_paused=False)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.get("/meme-templates", response_model=list[AdminMemeTemplateRead], summary="List meme templates")
async def list_meme_templates(_admin: AdminUserDep, admin_service: AdminServiceDep) -> list[AdminMemeTemplateRead]:
    return await admin_service.list_meme_templates()


@router.patch(
    "/meme-templates/{template_id}",
    response_model=AdminMemeTemplateRead,
    summary="Edit a meme template",
)
async def update_meme_template(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    template_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminMemeTemplateUpdateRequest, Body()],
) -> AdminMemeTemplateRead:
    try:
        return await admin_service.update_meme_template(template_id, request)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.get("/memes", response_model=list[AdminMemeRead], summary="List memes for minimal moderation")
async def list_moderation_memes(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    is_nsfw: Annotated[bool | None, Query()] = None,
    is_public: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AdminMemeRead]:
    return await admin_service.list_moderation_memes(
        is_nsfw=is_nsfw,
        is_public=is_public,
        limit=limit,
        offset=offset,
    )


@router.get("/memes/{meme_id}", response_model=AdminMemeDetailRead, summary="Read admin meme detail")
async def get_admin_meme_detail(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    meme_id: Annotated[uuid.UUID, Path()],
) -> AdminMemeDetailRead:
    try:
        return await admin_service.get_meme_detail(meme_id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.patch("/memes/{meme_id}/moderation", response_model=AdminMemeRead, summary="Override meme admin fields")
async def update_meme_moderation(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    meme_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminMemeModerationUpdateRequest, Body()],
) -> AdminMemeRead:
    try:
        return await admin_service.update_meme_moderation(
            meme_id,
            admin_user_id=admin_user.id,
            request=request,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.delete("/memes/{meme_id}", response_model=AdminMemeDestructiveActionRead, summary="Delete a meme")
async def delete_admin_meme(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    meme_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminMemeDeleteRequest, Body()],
) -> AdminMemeDestructiveActionRead:
    try:
        return await admin_service.delete_meme(
            meme_id,
            admin_user_id=admin_user.id,
            request=request,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post("/memes/{meme_id}/merge", response_model=AdminMemeDestructiveActionRead, summary="Merge a meme")
async def merge_admin_meme(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    meme_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminMemeMergeRequest, Body()],
) -> AdminMemeDestructiveActionRead:
    try:
        return await admin_service.merge_meme(
            meme_id,
            admin_user_id=admin_user.id,
            request=request,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.get(
    "/moderation-reports",
    response_model=list[AdminModerationReportRead],
    summary="List open moderation reports",
)
async def list_moderation_reports(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    report_status: Annotated[ModerationReportStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AdminModerationReportRead]:
    return await admin_service.list_moderation_reports(
        report_status=report_status,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/moderation-reports/{report_id}/resolve",
    response_model=AdminModerationReportRead,
    summary="Resolve a moderation report",
)
async def resolve_moderation_report(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    report_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminModerationReportResolveRequest, Body()],
) -> AdminModerationReportRead:
    try:
        return await admin_service.resolve_moderation_report(
            report_id,
            admin_user_id=admin_user.id,
            request=request,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.get(
    "/moderation-decisions",
    response_model=list[AdminModerationDecisionRead],
    summary="List moderation decision history",
)
async def list_moderation_decisions(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    meme_id: Annotated[uuid.UUID | None, Query()] = None,
    report_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AdminModerationDecisionRead]:
    return await admin_service.list_moderation_decisions(
        meme_id=meme_id,
        report_id=report_id,
        limit=limit,
        offset=offset,
    )


__all__ = ["AdminServiceDep", "get_admin_service", "router"]
