# ruff: noqa: TC001,TC002,TC003
"""Cookie-authenticated browser-admin routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from pydantic import AwareDatetime

from memexpert.api.dependencies import AdminUserDep, DbSessionDep
from memexpert.models.enums import ChannelSuggestionStatus, ModerationReportStatus, SourcePlatform
from memexpert.schemas.admin import (
    AdminBlockedPerceptualHashActionRead,
    AdminBlockedPerceptualHashAuditRead,
    AdminBlockedPerceptualHashCreateRequest,
    AdminBlockedPerceptualHashDeactivateRequest,
    AdminBlockedPerceptualHashRead,
    AdminBlockedPerceptualHashUpdateRequest,
    AdminChannelSuggestionReviewRequest,
    AdminMemeDeleteRequest,
    AdminMemeDestructiveActionRead,
    AdminMemeDetailRead,
    AdminMemeMergeRequest,
    AdminMemeModerationUpdateRequest,
    AdminMemeRead,
    AdminMemeSeoEditRequest,
    AdminMemeSeoPageRead,
    AdminMemeSeoRegenerateRequest,
    AdminMemeSeoReviewRowRead,
    AdminMemeTemplateActionRead,
    AdminMemeTemplateCreateRequest,
    AdminMemeTemplateDeleteRequest,
    AdminMemeTemplateMergeRequest,
    AdminMemeTemplateRead,
    AdminMemeTemplateUpdateRequest,
    AdminModerationDecisionRead,
    AdminModerationReportRead,
    AdminModerationReportResolveRequest,
    AdminOverviewRead,
    AdminSessionRead,
    AdminSourceChannelAssignRequest,
    AdminSourceChannelBackfillRequest,
    AdminSourceChannelCreateRequest,
    AdminSourceChannelMarkDeadRequest,
    AdminSourceChannelOrphanRequest,
    AdminSourceChannelPostPageRead,
    AdminSourceChannelRead,
    AdminSourceChannelUpdateRequest,
    AdminTelegramChannelFromReferenceRequest,
    AdminTelegramChannelGroupRead,
    AdminTelegramLoginCancelRead,
    AdminTelegramLoginCompleteRead,
    AdminTelegramLoginPasswordRequest,
    AdminTelegramLoginPhoneCodeRequest,
    AdminTelegramLoginPhoneStartRead,
    AdminTelegramLoginPhoneStartRequest,
    AdminTelegramLoginQrCompleteRequest,
    AdminTelegramLoginQrStartRead,
    AdminTelegramLoginQrStartRequest,
    AdminTelegramLoginQrStatusRead,
    AdminTelegramSessionActionRead,
    AdminTelegramSessionCreateRequest,
    AdminTelegramSessionDeleteRequest,
    AdminTelegramSessionRead,
    AdminTelegramSessionUpdateRequest,
    AdminTelegramSessionValidateRead,
    AdminTelegramSessionValidateRequest,
)
from memexpert.schemas.user import ChannelSuggestionRead
from memexpert.services.admin import AdminConflictError, AdminNotFoundError, AdminService
from memexpert.services.admin_telegram_login import AdminTelegramLoginService

router = APIRouter(prefix="/admin", tags=["admin"])


def get_admin_service(session: DbSessionDep) -> AdminService:
    """Build the admin service for the current request session."""

    return AdminService(session=session)


def get_admin_telegram_login_service(session: DbSessionDep) -> AdminTelegramLoginService:
    """Build the admin Telegram login service for the current request session."""

    return AdminTelegramLoginService(session=session)


AdminServiceDep = Annotated[AdminService, Depends(get_admin_service)]
AdminTelegramLoginServiceDep = Annotated[AdminTelegramLoginService, Depends(get_admin_telegram_login_service)]


def _map_admin_error(exc: AdminNotFoundError | AdminConflictError) -> HTTPException:
    if isinstance(exc, AdminNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/session", response_model=AdminSessionRead, summary="Read current admin session")
async def read_admin_session(admin_user: AdminUserDep) -> AdminSessionRead:
    return AdminSessionRead(user=admin_user)


@router.get("/overview", response_model=AdminOverviewRead, summary="Read actionable admin overview counts")
async def get_admin_overview(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
) -> AdminOverviewRead:
    return await admin_service.get_overview()


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


@router.get("/telegram/sessions", response_model=list[AdminTelegramSessionRead], summary="List Telegram sessions")
async def list_telegram_sessions(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
) -> list[AdminTelegramSessionRead]:
    return await admin_service.list_telegram_sessions()


@router.post(
    "/telegram/sessions",
    response_model=AdminTelegramSessionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a DB-backed Telegram session shell",
)
async def create_telegram_session(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    request: Annotated[AdminTelegramSessionCreateRequest, Body()],
) -> AdminTelegramSessionRead:
    try:
        return await admin_service.create_telegram_session(request, admin_user_id=admin_user.id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.patch(
    "/telegram/sessions/{session_id}",
    response_model=AdminTelegramSessionRead,
    summary="Patch Telegram session policy and status",
)
async def update_telegram_session(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    session_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminTelegramSessionUpdateRequest, Body()],
) -> AdminTelegramSessionRead:
    try:
        return await admin_service.update_telegram_session(session_id, request, admin_user_id=admin_user.id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/telegram/login-attempts/qr",
    response_model=AdminTelegramLoginQrStartRead,
    summary="Start a standalone Telegram QR login attempt",
)
async def start_telegram_qr_login_attempt(
    admin_user: AdminUserDep,
    login_service: AdminTelegramLoginServiceDep,
    request: Annotated[AdminTelegramLoginQrStartRequest | None, Body()] = None,
) -> AdminTelegramLoginQrStartRead:
    try:
        return await login_service.start_qr_login(
            request or AdminTelegramLoginQrStartRequest(),
            admin_user_id=admin_user.id,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/telegram/login-attempts/{attempt_id}/qr/complete",
    response_model=AdminTelegramLoginQrStatusRead,
    summary="Poll a standalone Telegram QR login attempt",
)
async def complete_telegram_qr_login_attempt(
    admin_user: AdminUserDep,
    login_service: AdminTelegramLoginServiceDep,
    attempt_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminTelegramLoginQrCompleteRequest | None, Body()] = None,
) -> AdminTelegramLoginQrStatusRead:
    try:
        return await login_service.complete_qr_login(
            attempt_id,
            request or AdminTelegramLoginQrCompleteRequest(),
            admin_user_id=admin_user.id,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/telegram/login-attempts/phone",
    response_model=AdminTelegramLoginPhoneStartRead,
    summary="Start a standalone Telegram phone login attempt",
)
async def start_telegram_phone_login_attempt(
    admin_user: AdminUserDep,
    login_service: AdminTelegramLoginServiceDep,
    request: Annotated[AdminTelegramLoginPhoneStartRequest, Body()],
) -> AdminTelegramLoginPhoneStartRead:
    try:
        return await login_service.start_phone_login(request, admin_user_id=admin_user.id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/telegram/login-attempts/{attempt_id}/phone/code",
    response_model=AdminTelegramLoginCompleteRead,
    summary="Complete a standalone Telegram phone login with a code",
)
async def complete_telegram_phone_code_login_attempt(
    admin_user: AdminUserDep,
    login_service: AdminTelegramLoginServiceDep,
    attempt_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminTelegramLoginPhoneCodeRequest, Body()],
) -> AdminTelegramLoginCompleteRead:
    try:
        return await login_service.complete_phone_code_login(attempt_id, request, admin_user_id=admin_user.id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/telegram/login-attempts/{attempt_id}/password",
    response_model=AdminTelegramLoginCompleteRead,
    summary="Complete a standalone Telegram login with a 2FA password",
)
async def complete_telegram_password_login_attempt(
    admin_user: AdminUserDep,
    login_service: AdminTelegramLoginServiceDep,
    attempt_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminTelegramLoginPasswordRequest, Body()],
) -> AdminTelegramLoginCompleteRead:
    try:
        return await login_service.complete_password_login(attempt_id, request, admin_user_id=admin_user.id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.delete(
    "/telegram/login-attempts/{attempt_id}",
    response_model=AdminTelegramLoginCancelRead,
    summary="Cancel a standalone Telegram login attempt",
)
async def cancel_telegram_login_attempt(
    admin_user: AdminUserDep,
    login_service: AdminTelegramLoginServiceDep,
    attempt_id: Annotated[uuid.UUID, Path()],
) -> AdminTelegramLoginCancelRead:
    try:
        return await login_service.cancel_login_attempt(attempt_id, admin_user_id=admin_user.id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/telegram/sessions/{session_id}/validate",
    response_model=AdminTelegramSessionValidateRead,
    summary="Validate a stored Telegram session without exposing secret material",
)
async def validate_telegram_session(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    session_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminTelegramSessionValidateRequest | None, Body()] = None,
) -> AdminTelegramSessionValidateRead:
    try:
        return await admin_service.validate_telegram_session(
            session_id,
            request or AdminTelegramSessionValidateRequest(),
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.delete(
    "/telegram/sessions/{session_id}",
    response_model=AdminTelegramSessionActionRead,
    summary="Delete a Telegram session and orphan assigned channels",
)
async def delete_telegram_session(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    session_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminTelegramSessionDeleteRequest, Body()],
) -> AdminTelegramSessionActionRead:
    try:
        return await admin_service.delete_telegram_session(session_id, request, admin_user_id=admin_user.id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.get("/source-channels", response_model=list[AdminSourceChannelRead], summary="List source channels")
async def list_source_channels(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    telegram_session_id: Annotated[uuid.UUID | None, Query()] = None,
    orphaned: Annotated[bool | None, Query()] = None,
) -> list[AdminSourceChannelRead]:
    try:
        return await admin_service.list_source_channels(
            telegram_session_id=telegram_session_id,
            orphaned=orphaned,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.get(
    "/source-channels/{channel_id}/posts",
    response_model=AdminSourceChannelPostPageRead,
    summary="List observed source messages with indexing state",
)
async def list_source_channel_posts(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    channel_id: Annotated[uuid.UUID, Path()],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    snapshot_at: Annotated[AwareDatetime | None, Query()] = None,
) -> AdminSourceChannelPostPageRead:
    try:
        return await admin_service.list_source_channel_posts(
            channel_id,
            limit=limit,
            offset=offset,
            snapshot_at=snapshot_at,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/source-channels/{channel_id}/backfill",
    response_model=AdminSourceChannelRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue older Telegram history for a source",
)
async def queue_source_channel_backfill(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    channel_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminSourceChannelBackfillRequest, Body()],
) -> AdminSourceChannelRead:
    try:
        return await admin_service.queue_source_channel_backfill(
            channel_id,
            request,
            admin_user_id=admin_user.id,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.get("/telegram/channels", response_model=list[AdminSourceChannelRead], summary="List Telegram source channels")
async def list_telegram_source_channels(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    telegram_session_id: Annotated[uuid.UUID | None, Query()] = None,
    orphaned: Annotated[bool | None, Query()] = None,
) -> list[AdminSourceChannelRead]:
    try:
        return await admin_service.list_source_channels(
            platform=SourcePlatform.TELEGRAM,
            telegram_session_id=telegram_session_id,
            orphaned=orphaned,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.get(
    "/telegram/channels/grouped",
    response_model=list[AdminTelegramChannelGroupRead],
    summary="List Telegram source channels grouped by session",
)
async def list_telegram_channel_groups(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
) -> list[AdminTelegramChannelGroupRead]:
    return await admin_service.list_telegram_channel_groups()


@router.post(
    "/source-channels",
    response_model=AdminSourceChannelRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a source channel",
)
async def add_source_channel(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    request: Annotated[AdminSourceChannelCreateRequest, Body()],
) -> AdminSourceChannelRead:
    try:
        return await admin_service.add_source_channel(request, admin_user_id=admin_user.id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/telegram/channels",
    response_model=AdminSourceChannelRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a Telegram source channel",
)
async def add_telegram_source_channel(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    request: Annotated[AdminSourceChannelCreateRequest, Body()],
) -> AdminSourceChannelRead:
    if request.platform is not SourcePlatform.TELEGRAM:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The Telegram channel admin route only accepts platform='telegram'.",
        )
    try:
        return await admin_service.add_source_channel(request, admin_user_id=admin_user.id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/telegram/channels/from-reference",
    response_model=AdminSourceChannelRead,
    status_code=status.HTTP_201_CREATED,
    summary="Resolve and add a public Telegram channel",
)
async def add_telegram_source_channel_from_reference(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    request: Annotated[AdminTelegramChannelFromReferenceRequest, Body()],
) -> AdminSourceChannelRead:
    try:
        return await admin_service.add_telegram_channel_from_reference(
            request,
            admin_user_id=admin_user.id,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.patch(
    "/telegram/channels/{channel_id}",
    response_model=AdminSourceChannelRead,
    summary="Patch source-channel crawler controls",
)
async def update_telegram_source_channel(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    channel_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminSourceChannelUpdateRequest, Body()],
) -> AdminSourceChannelRead:
    try:
        return await admin_service.update_source_channel(channel_id, request, admin_user_id=admin_user.id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/telegram/channels/{channel_id}/assign",
    response_model=AdminSourceChannelRead,
    summary="Assign a source channel to a Telegram session",
)
async def assign_telegram_source_channel(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    channel_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminSourceChannelAssignRequest, Body()],
) -> AdminSourceChannelRead:
    try:
        return await admin_service.assign_source_channel(channel_id, request, admin_user_id=admin_user.id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/telegram/channels/{channel_id}/orphan",
    response_model=AdminSourceChannelRead,
    summary="Orphan a source channel and disable crawler controls",
)
async def orphan_telegram_source_channel(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    channel_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminSourceChannelOrphanRequest | None, Body()] = None,
) -> AdminSourceChannelRead:
    try:
        return await admin_service.orphan_source_channel(
            channel_id,
            request or AdminSourceChannelOrphanRequest(),
            admin_user_id=admin_user.id,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/source-channels/{channel_id}/pause",
    response_model=AdminSourceChannelRead,
    summary="Pause a source channel",
)
async def pause_source_channel(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    channel_id: Annotated[uuid.UUID, Path()],
) -> AdminSourceChannelRead:
    try:
        return await admin_service.set_source_channel_paused(
            channel_id,
            is_paused=True,
            admin_user_id=admin_user.id,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/source-channels/{channel_id}/resume",
    response_model=AdminSourceChannelRead,
    summary="Resume a source channel",
)
async def resume_source_channel(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    channel_id: Annotated[uuid.UUID, Path()],
) -> AdminSourceChannelRead:
    try:
        return await admin_service.set_source_channel_paused(
            channel_id,
            is_paused=False,
            admin_user_id=admin_user.id,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/source-channels/{channel_id}/mark-dead",
    response_model=AdminSourceChannelRead,
    summary="Mark a source channel dead without deleting checkpoint state",
)
async def mark_source_channel_dead(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    channel_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminSourceChannelMarkDeadRequest, Body()],
) -> AdminSourceChannelRead:
    try:
        return await admin_service.mark_source_channel_dead(channel_id, request, admin_user_id=admin_user.id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.get("/meme-templates", response_model=list[AdminMemeTemplateRead], summary="List meme templates")
async def list_meme_templates(_admin: AdminUserDep, admin_service: AdminServiceDep) -> list[AdminMemeTemplateRead]:
    return await admin_service.list_meme_templates()


@router.post(
    "/meme-templates",
    response_model=AdminMemeTemplateRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a meme template",
)
async def create_meme_template(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    request: Annotated[AdminMemeTemplateCreateRequest, Body()],
) -> AdminMemeTemplateRead:
    try:
        return await admin_service.create_meme_template(request)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


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


@router.post(
    "/meme-templates/{template_id}/merge",
    response_model=AdminMemeTemplateActionRead,
    summary="Merge a duplicate meme template into a target",
)
async def merge_meme_template(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    template_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminMemeTemplateMergeRequest, Body()],
) -> AdminMemeTemplateActionRead:
    try:
        return await admin_service.merge_meme_template(
            template_id,
            admin_user_id=admin_user.id,
            request=request,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.delete(
    "/meme-templates/{template_id}",
    response_model=AdminMemeTemplateActionRead,
    summary="Delete an unreferenced meme template",
)
async def delete_meme_template(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    template_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminMemeTemplateDeleteRequest, Body()],
) -> AdminMemeTemplateActionRead:
    try:
        return await admin_service.delete_meme_template(template_id, request)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.get(
    "/blocked-perceptual-hashes",
    response_model=list[AdminBlockedPerceptualHashRead],
    summary="List blocked perceptual hashes",
)
async def list_blocked_perceptual_hashes(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    is_active: Annotated[bool | None, Query()] = None,
) -> list[AdminBlockedPerceptualHashRead]:
    return await admin_service.list_blocked_perceptual_hashes(is_active=is_active)


@router.post(
    "/blocked-perceptual-hashes",
    response_model=AdminBlockedPerceptualHashRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a blocked perceptual hash",
)
async def create_blocked_perceptual_hash(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    request: Annotated[AdminBlockedPerceptualHashCreateRequest, Body()],
) -> AdminBlockedPerceptualHashRead:
    try:
        return await admin_service.create_blocked_perceptual_hash(request, admin_user_id=admin_user.id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.patch(
    "/blocked-perceptual-hashes/{blocked_hash_id}",
    response_model=AdminBlockedPerceptualHashRead,
    summary="Update a blocked perceptual hash",
)
async def update_blocked_perceptual_hash(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    blocked_hash_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminBlockedPerceptualHashUpdateRequest, Body()],
) -> AdminBlockedPerceptualHashRead:
    try:
        return await admin_service.update_blocked_perceptual_hash(
            blocked_hash_id,
            request,
            admin_user_id=admin_user.id,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/blocked-perceptual-hashes/{blocked_hash_id}/deactivate",
    response_model=AdminBlockedPerceptualHashActionRead,
    summary="Deactivate a blocked perceptual hash",
)
async def deactivate_blocked_perceptual_hash(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    blocked_hash_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminBlockedPerceptualHashDeactivateRequest | None, Body()] = None,
) -> AdminBlockedPerceptualHashActionRead:
    try:
        return await admin_service.deactivate_blocked_perceptual_hash(
            blocked_hash_id,
            request or AdminBlockedPerceptualHashDeactivateRequest(),
            admin_user_id=admin_user.id,
        )
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.delete(
    "/blocked-perceptual-hashes/{blocked_hash_id}",
    response_model=AdminBlockedPerceptualHashActionRead,
    summary="Delete an unreferenced blocked perceptual hash or deactivate a referenced one",
)
async def delete_blocked_perceptual_hash(
    admin_user: AdminUserDep,
    admin_service: AdminServiceDep,
    blocked_hash_id: Annotated[uuid.UUID, Path()],
) -> AdminBlockedPerceptualHashActionRead:
    try:
        return await admin_service.delete_blocked_perceptual_hash_safe(blocked_hash_id, admin_user_id=admin_user.id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.get(
    "/blocked-perceptual-hashes/{blocked_hash_id}/audit-log",
    response_model=list[AdminBlockedPerceptualHashAuditRead],
    summary="List blocked perceptual hash audit history",
)
async def list_blocked_perceptual_hash_audit(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    blocked_hash_id: Annotated[uuid.UUID, Path()],
) -> list[AdminBlockedPerceptualHashAuditRead]:
    return await admin_service.list_blocked_perceptual_hash_audit(blocked_hash_id)


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


@router.get("/seo-pages", response_model=list[AdminMemeSeoReviewRowRead], summary="List SEO review rows")
async def list_seo_review_rows(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AdminMemeSeoReviewRowRead]:
    return await admin_service.list_seo_review_rows(limit=limit, offset=offset)


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


@router.patch(
    "/memes/{meme_id}/seo-page",
    response_model=AdminMemeSeoPageRead,
    summary="Manually edit or create a meme SEO page",
)
async def edit_admin_meme_seo_page(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    meme_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminMemeSeoEditRequest, Body()],
) -> AdminMemeSeoPageRead:
    try:
        return await admin_service.edit_meme_seo_page(meme_id, request)
    except (AdminNotFoundError, AdminConflictError) as exc:
        raise _map_admin_error(exc) from exc


@router.post(
    "/memes/{meme_id}/seo-page/regenerate",
    response_model=AdminMemeSeoPageRead,
    summary="Regenerate a meme SEO page with exact confirmation",
)
async def regenerate_admin_meme_seo_page(
    _admin: AdminUserDep,
    admin_service: AdminServiceDep,
    meme_id: Annotated[uuid.UUID, Path()],
    request: Annotated[AdminMemeSeoRegenerateRequest, Body()],
) -> AdminMemeSeoPageRead:
    try:
        return await admin_service.regenerate_meme_seo_page(meme_id, request)
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
