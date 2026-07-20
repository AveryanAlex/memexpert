# ruff: noqa: TC001
"""Privacy-bounded first-party telemetry endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.routing import APIRoute

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from starlette.responses import Response
    from starlette.types import Message

from memexpert.api.dependencies import AnalyticsServiceDep, MemeSearchServiceDep, OptionalCurrentUserDep
from memexpert.api.routes._meme_interactions import (
    MemeActionAttributionRequest,
    build_meme_interaction_write,
    resolve_meme_interaction_request,
)
from memexpert.models.enums import AnalyticsEventType
from memexpert.schemas.analytics import (
    INTERACTION_BATCH_MAX_BYTES,
    INTERACTION_PROPERTIES_MAX_DEPTH,
    InteractionBatchCreateRequest,
    InteractionBatchRecordedRead,
    PageViewCreateRequest,
    PageViewRecordedRead,
)
from memexpert.services.analytics import InteractionEventIdConflictError, InteractionEventWrite

_INTERACTION_BATCH_JSON_MAX_DEPTH = INTERACTION_PROPERTIES_MAX_DEPTH + 4


def _json_nesting_exceeds_limit(payload: bytes, *, max_depth: int) -> bool:
    """Scan structural JSON bytes without recursively parsing an attacker-controlled tree."""

    depth = 0
    in_string = False
    escaped = False
    for character in payload:
        if in_string:
            if escaped:
                escaped = False
            elif character == ord("\\"):
                escaped = True
            elif character == ord('"'):
                in_string = False
            continue
        if character == ord('"'):
            in_string = True
        elif character in {ord("{"), ord("[")}:
            depth += 1
            if depth > max_depth:
                return True
        elif character in {ord("}"), ord("]")}:
            depth = max(depth - 1, 0)
    return False


class _BoundedAnalyticsRoute(APIRoute):
    """Reject oversized or excessively nested interaction bodies before JSON decoding."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        route_handler = super().get_route_handler()
        if not self.path.endswith("/interactions/batch"):
            return route_handler

        async def bounded_route_handler(request: Request) -> Response:
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail="Interaction batch Content-Length is invalid.",
                    ) from exc
                if declared_size < 0:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail="Interaction batch Content-Length is invalid.",
                    )
                if declared_size > INTERACTION_BATCH_MAX_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=f"Interaction batch requests may contain at most {INTERACTION_BATCH_MAX_BYTES} bytes.",
                    )

            payload = bytearray()
            async for chunk in request.stream():
                if len(payload) + len(chunk) > INTERACTION_BATCH_MAX_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=f"Interaction batch requests may contain at most {INTERACTION_BATCH_MAX_BYTES} bytes.",
                    )
                payload.extend(chunk)
            payload_bytes = bytes(payload)
            if _json_nesting_exceeds_limit(
                payload_bytes,
                max_depth=_INTERACTION_BATCH_JSON_MAX_DEPTH,
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        "Interaction batch JSON exceeds the maximum supported "
                        f"nesting depth of {_INTERACTION_BATCH_JSON_MAX_DEPTH}."
                    ),
                )

            sent = False

            async def receive() -> Message:
                nonlocal sent
                if sent:
                    return {"type": "http.request", "body": b"", "more_body": False}
                sent = True
                return {"type": "http.request", "body": payload_bytes, "more_body": False}

            bounded_request = Request(request.scope, receive=receive)
            return await route_handler(bounded_request)

        return bounded_route_handler


router = APIRouter(prefix="/analytics", tags=["analytics"], route_class=_BoundedAnalyticsRoute)


@router.post(
    "/page-views",
    response_model=PageViewRecordedRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Record a consumer page-view category",
)
async def record_page_view(
    page_view: PageViewCreateRequest,
    current_user: OptionalCurrentUserDep,
    analytics_service: AnalyticsServiceDep,
) -> PageViewRecordedRead:
    """Record a first-party page view without accepting URLs or visitor metadata.

    The writer is deliberately best-effort: the browser should never see a
    product error because analytics storage is temporarily unavailable.
    """

    await analytics_service.record_interaction_event_best_effort(
        InteractionEventWrite(
            event_type=AnalyticsEventType.PAGE_VIEW,
            user_id=current_user.id if current_user else None,
            surface=page_view.surface.value,
        )
    )
    return PageViewRecordedRead()


@router.post(
    "/interactions/batch",
    response_model=InteractionBatchRecordedRead,
    responses={
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": f"Request body exceeds {INTERACTION_BATCH_MAX_BYTES} bytes.",
        },
    },
    status_code=status.HTTP_202_ACCEPTED,
    summary="Record an idempotent batch of attributed meme interactions",
)
async def record_interaction_batch(
    batch: InteractionBatchCreateRequest,
    current_user: OptionalCurrentUserDep,
    analytics_service: AnalyticsServiceDep,
    meme_search_service: MemeSearchServiceDep,
) -> InteractionBatchRecordedRead:
    """Verify signed placements, current access, and atomically persist at most 50 events."""

    meme_ids = tuple(dict.fromkeys(event.meme_id for event in batch.events))
    visible_meme_ids = await meme_search_service.visible_meme_ids_for_interactions(
        meme_ids,
        viewer_user_id=current_user.id if current_user else None,
        include_nsfw=current_user.nsfw_enabled if current_user else False,
    )
    if visible_meme_ids != frozenset(meme_ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meme was not found.")

    writes = []
    for event in batch.events:
        interaction = resolve_meme_interaction_request(
            MemeActionAttributionRequest(
                event_id=event.event_id,
                attribution_token=event.attribution_token,
            ),
            meme_id=event.meme_id,
            current_user=current_user,
        )
        try:
            writes.append(
                build_meme_interaction_write(
                    event.event_type,
                    meme_id=event.meme_id,
                    current_user=current_user,
                    interaction=interaction,
                    default_surface="web_unknown",
                    occurred_at=event.occurred_at,
                    properties=event.properties,
                )
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Interaction event payload is invalid.",
            ) from exc
    try:
        result = await analytics_service.record_interaction_events(tuple(writes))
    except InteractionEventIdConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="event_id is already assigned to another interaction.",
        ) from exc
    return InteractionBatchRecordedRead(recorded=result.recorded, duplicates=result.duplicates)


__all__ = ["router"]
