# ruff: noqa: TC001,TC003
"""Cookie-authenticated failed-work visibility and durable recovery actions."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status

from memexpert.api.dependencies import AdminUserDep, DbSessionDep
from memexpert.models.enums import ContentPipelineStage, RecoveryBucket, RecoveryWorkKind
from memexpert.schemas.admin_recovery import (
    AdminSourceBackfillPageRead,
    RecoveryActionRequest,
    RecoveryBatchCancelRequest,
    RecoveryBatchHandoffRequest,
    RecoveryBatchPreviewRequest,
    RecoveryBatchScheduleRequest,
    RecoveryCandidateRead,
    RecoveryJobItemPageRead,
    RecoveryJobPageRead,
    RecoveryJobRead,
    RecoveryMutationRequest,
    RecoveryRetryFailedPreviewRequest,
    RecoverySummaryRead,
    RecoveryTargetMutationRequest,
    RecoveryWorkPageRead,
    RecoveryWorkRead,
)
from memexpert.services.admin_recovery import (
    AdminRecoveryConflictError,
    AdminRecoveryNotFoundError,
    AdminRecoveryService,
)

router = APIRouter(prefix="/admin", tags=["admin recovery"])


def get_admin_recovery_service(session: DbSessionDep) -> AdminRecoveryService:
    return AdminRecoveryService(session)


AdminRecoveryServiceDep = Annotated[AdminRecoveryService, Depends(get_admin_recovery_service)]


def _map_recovery_error(exc: AdminRecoveryNotFoundError | AdminRecoveryConflictError) -> HTTPException:
    if isinstance(exc, AdminRecoveryNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/recovery/summary", response_model=RecoverySummaryRead, summary="Read failed-work summary")
async def read_recovery_summary(
    _admin: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
) -> RecoverySummaryRead:
    return await recovery_service.get_summary()


@router.get("/recovery/work", response_model=RecoveryWorkPageRead, summary="List failed and stuck work")
async def list_recovery_work(
    _admin: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    bucket: Annotated[RecoveryBucket | None, Query()] = None,
    kind: Annotated[RecoveryWorkKind | None, Query()] = None,
    source_channel_id: Annotated[uuid.UUID | None, Query()] = None,
    stage: Annotated[ContentPipelineStage | None, Query()] = None,
    reason: Annotated[str | None, Query(max_length=128)] = None,
    query: Annotated[str | None, Query(alias="q", max_length=255)] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> RecoveryWorkPageRead:
    try:
        return await recovery_service.list_work(
            bucket=bucket,
            kind=kind,
            source_channel_id=source_channel_id,
            stage=stage,
            reason=reason,
            query=query,
            cursor=cursor,
            limit=limit,
        )
    except AdminRecoveryConflictError as exc:
        raise _map_recovery_error(exc) from exc


@router.get(
    "/recovery/work/{kind}/{work_id}",
    response_model=RecoveryWorkRead,
    summary="Read one failed-work detail",
)
async def read_recovery_work(
    _admin: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    kind: Annotated[RecoveryWorkKind, Path()],
    work_id: Annotated[str, Path(min_length=1, max_length=255)],
) -> RecoveryWorkRead:
    try:
        return await recovery_service.get_work(kind, work_id)
    except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
        raise _map_recovery_error(exc) from exc


@router.get(
    "/recovery/work/{kind}/{work_id}/candidate",
    response_model=RecoveryCandidateRead,
    summary="Read replay and repair actions for canonical work",
)
async def read_recovery_candidate(
    _admin: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    kind: Annotated[RecoveryWorkKind, Path()],
    work_id: Annotated[str, Path(min_length=1, max_length=255)],
) -> RecoveryCandidateRead:
    try:
        return await recovery_service.get_candidate(kind, work_id)
    except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
        raise _map_recovery_error(exc) from exc


@router.post(
    "/recovery/work/{kind}/{work_id}/actions",
    response_model=RecoveryJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Schedule one audited Replay & Repair action",
)
async def act_on_recovery_work(
    admin_user: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    kind: Annotated[RecoveryWorkKind, Path()],
    work_id: Annotated[str, Path(min_length=1, max_length=255)],
    payload: Annotated[RecoveryActionRequest, Body()],
) -> RecoveryJobRead:
    try:
        return await recovery_service.perform_action(
            admin_user_id=admin_user.id,
            kind=kind,
            work_id=work_id,
            payload=payload,
        )
    except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
        raise _map_recovery_error(exc) from exc


@router.post(
    "/recovery/work/{kind}/{work_id}/retry",
    response_model=RecoveryJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Schedule one audited recovery action",
)
async def retry_recovery_work(
    admin_user: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    kind: Annotated[RecoveryWorkKind, Path()],
    work_id: Annotated[str, Path(min_length=1, max_length=255)],
    payload: Annotated[RecoveryMutationRequest, Body()],
) -> RecoveryJobRead:
    try:
        return await recovery_service.retry_work(
            admin_user_id=admin_user.id,
            kind=kind,
            work_id=work_id,
            payload=payload,
        )
    except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
        raise _map_recovery_error(exc) from exc


@router.get(
    "/recovery/batches",
    response_model=RecoveryJobPageRead,
    summary="List Replay & Repair job history",
)
async def list_recovery_batches(
    _admin: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    job_status: Annotated[str | None, Query(alias="status", max_length=64)] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> RecoveryJobPageRead:
    try:
        return await recovery_service.list_jobs(status=job_status, cursor=cursor, limit=limit)
    except AdminRecoveryConflictError as exc:
        raise _map_recovery_error(exc) from exc


@router.post(
    "/recovery/batches/preview",
    response_model=RecoveryJobRead,
    status_code=status.HTTP_201_CREATED,
    summary="Preview explicit or uncapped query-selected recovery work",
)
async def preview_recovery_batch(
    admin_user: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    payload: Annotated[RecoveryBatchPreviewRequest, Body()],
) -> RecoveryJobRead:
    try:
        return await recovery_service.preview_batch(admin_user_id=admin_user.id, payload=payload)
    except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
        raise _map_recovery_error(exc) from exc


@router.post(
    "/recovery/batches/{job_id}/schedule",
    response_model=RecoveryJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Schedule a previewed recovery batch",
)
async def schedule_recovery_batch(
    admin_user: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    job_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[RecoveryBatchScheduleRequest, Body()],
) -> RecoveryJobRead:
    try:
        return await recovery_service.schedule_batch(
            admin_user_id=admin_user.id,
            job_id=job_id,
            version=payload.version,
            reason=payload.reason,
        )
    except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
        raise _map_recovery_error(exc) from exc


@router.get(
    "/recovery/batches/{job_id}",
    response_model=RecoveryJobRead,
    summary="Read recovery batch progress",
)
async def read_recovery_batch(
    admin_user: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    job_id: Annotated[uuid.UUID, Path()],
) -> RecoveryJobRead:
    try:
        return await recovery_service.get_job(admin_user_id=admin_user.id, job_id=job_id)
    except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
        raise _map_recovery_error(exc) from exc


@router.get(
    "/recovery/batches/{job_id}/items",
    response_model=RecoveryJobItemPageRead,
    summary="List paginated Replay & Repair job items",
)
async def list_recovery_batch_items(
    _admin: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    job_id: Annotated[uuid.UUID, Path()],
    item_status: Annotated[str | None, Query(alias="status", max_length=64)] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    failed_first: Annotated[bool, Query()] = True,
) -> RecoveryJobItemPageRead:
    try:
        return await recovery_service.list_job_items(
            job_id=job_id,
            status=item_status,
            cursor=cursor,
            limit=limit,
            failed_first=failed_first,
        )
    except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
        raise _map_recovery_error(exc) from exc


@router.post(
    "/recovery/batches/{job_id}/cancel",
    response_model=RecoveryJobRead,
    summary="Cancel undispatched recovery batch items",
)
async def cancel_recovery_batch(
    admin_user: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    job_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[RecoveryBatchCancelRequest, Body()],
) -> RecoveryJobRead:
    try:
        return await recovery_service.cancel_batch(
            admin_user_id=admin_user.id,
            job_id=job_id,
            version=payload.version,
            reason=payload.reason,
        )
    except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
        raise _map_recovery_error(exc) from exc


@router.post(
    "/recovery/batches/{job_id}/handoff",
    response_model=RecoveryJobRead,
    summary="Assign an operational job while retaining its requester",
)
async def handoff_recovery_batch(
    admin_user: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    job_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[RecoveryBatchHandoffRequest, Body()],
) -> RecoveryJobRead:
    try:
        return await recovery_service.handoff_job(
            admin_user_id=admin_user.id,
            job_id=job_id,
            assigned_admin_user_id=payload.assigned_admin_user_id,
            version=payload.version,
            reason=payload.reason,
        )
    except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
        raise _map_recovery_error(exc) from exc


@router.post(
    "/recovery/batches/{job_id}/retry-failed-preview",
    response_model=RecoveryJobRead,
    status_code=status.HTTP_201_CREATED,
    summary="Preview a retry of failed Replay & Repair steps",
)
async def preview_failed_recovery_items(
    admin_user: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    job_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[RecoveryRetryFailedPreviewRequest, Body()],
) -> RecoveryJobRead:
    try:
        return await recovery_service.preview_failed_items(
            admin_user_id=admin_user.id,
            job_id=job_id,
            payload=payload,
        )
    except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
        raise _map_recovery_error(exc) from exc


@router.get(
    "/source-channels/{source_channel_id}/backfills",
    response_model=AdminSourceBackfillPageRead,
    summary="List source backfill history",
)
async def list_source_backfills(
    _admin: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    source_channel_id: Annotated[uuid.UUID, Path()],
) -> AdminSourceBackfillPageRead:
    return await recovery_service.list_backfills(source_channel_id)


@router.post(
    "/source-channels/{source_channel_id}/backfills/{job_id}/resume",
    response_model=RecoveryJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Resume a failed source backfill",
)
async def resume_source_backfill(
    admin_user: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    source_channel_id: Annotated[uuid.UUID, Path()],
    job_id: Annotated[uuid.UUID, Path()],
    payload: Annotated[RecoveryTargetMutationRequest, Body()],
) -> RecoveryJobRead:
    try:
        return await recovery_service.resume_backfill(
            admin_user_id=admin_user.id,
            source_channel_id=source_channel_id,
            job_id=job_id,
            request_id=payload.request_id,
            version=payload.version,
            reason=payload.reason,
        )
    except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
        raise _map_recovery_error(exc) from exc


@router.post(
    "/source-channels/{source_channel_id}/posts/{post_id}/replay",
    response_model=RecoveryJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue asynchronous source-post replay",
)
async def replay_source_post(
    admin_user: AdminUserDep,
    recovery_service: AdminRecoveryServiceDep,
    source_channel_id: Annotated[uuid.UUID, Path()],
    post_id: Annotated[str, Path(min_length=1, max_length=255)],
    payload: Annotated[RecoveryTargetMutationRequest, Body()],
) -> RecoveryJobRead:
    try:
        return await recovery_service.replay_source_post(
            admin_user_id=admin_user.id,
            source_channel_id=source_channel_id,
            post_id=post_id,
            request_id=payload.request_id,
            version=payload.version,
            reason=payload.reason,
        )
    except (AdminRecoveryNotFoundError, AdminRecoveryConflictError) as exc:
        raise _map_recovery_error(exc) from exc


__all__ = ["AdminRecoveryServiceDep", "get_admin_recovery_service", "router"]
