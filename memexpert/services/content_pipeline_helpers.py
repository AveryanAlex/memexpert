# ruff: noqa: TC001,TC003
"""Pure helper functions and the stage-journal snapshot dataclass.

Split out of ``content_pipeline.py`` so every caller of a stateless
helper (trimmers, filename normalizers, stage-journal ordering, replay
reservation) can import the helper directly without pulling in the
full 2700-line service class. Every function below either operates on
an argument or on a stateless container and never touches
``ContentPipelineService`` state.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from memexpert.models.base import utcnow
from memexpert.models.enums import (
    ContentPipelineStageStatus,
    SyncTargetKind,
)
from memexpert.schemas.content_pipeline import (
    MAX_PIPELINE_ERROR_LENGTH,
    MAX_PIPELINE_REASON_LENGTH,
    ContentPipelineDispatchEvent,
    ContentPipelineItemFilter,
    ContentPipelineSyncTargetPreview,
)
from memexpert.services.content_pipeline_constants import (
    ACTIVE_STAGE_STATUSES,
    PIPELINE_REASON_REPLAY_REQUESTED,
    STAGE_ORDER,
)
from memexpert.services.errors import (
    PipelineIngestError,
    PipelinePayloadValidationError,
    PipelineUnsupportedMediaTypeError,
)

if TYPE_CHECKING:
    from memexpert.models.content import MemeFile, PipelineStageJournal


@dataclass(frozen=True, slots=True)
class StageJournalSnapshot:
    """Durable stage-journal state captured before replay reserves a new attempt."""

    attempt_count: int
    finished_at: datetime | None
    is_retryable: bool
    last_error_text: str | None
    last_event_id: uuid.UUID | None
    normalized_reason: str | None
    retry_after: datetime | None
    started_at: datetime | None
    status: ContentPipelineStageStatus


def trim_reason(normalized_reason: str) -> str:
    """Return ``normalized_reason`` trimmed and capped to the durable column width."""

    return normalized_reason.strip()[:MAX_PIPELINE_REASON_LENGTH]


def trim_error_text(last_error_text: str) -> str:
    """Return ``last_error_text`` trimmed and capped to the durable column width."""

    return last_error_text.strip()[:MAX_PIPELINE_ERROR_LENGTH]


def translate_media_processing_error(exc: Exception) -> PipelinePayloadValidationError:
    """Translate a ``PipelineMediaProcessor`` exception to a service-layer payload error."""

    from memexpert.core.media import MediaProcessingError, MediaTimeoutError, MediaValidationError

    if isinstance(exc, MediaTimeoutError):
        return PipelinePayloadValidationError(str(exc))
    if isinstance(exc, MediaValidationError):
        return PipelineUnsupportedMediaTypeError(str(exc))
    if isinstance(exc, MediaProcessingError):
        return PipelinePayloadValidationError(str(exc))
    return PipelinePayloadValidationError(str(exc))


def normalize_filename(filename: str | None) -> str:
    """Return a non-empty POSIX-safe filename or raise a validation error."""

    if filename is None:
        raise PipelinePayloadValidationError("Uploaded file must include a filename.")

    normalized_filename = PurePosixPath(filename).name.strip()
    if not normalized_filename:
        raise PipelinePayloadValidationError("Uploaded file must include a filename.")
    return normalized_filename


def normalize_content_type(content_type: str | None) -> str:
    """Return the lowercased content type or raise if missing/blank."""

    if content_type is None:
        raise PipelineUnsupportedMediaTypeError("Uploaded file must include a media type.")

    normalized_content_type = content_type.strip().lower()
    if not normalized_content_type:
        raise PipelineUnsupportedMediaTypeError("Uploaded file must include a media type.")
    return normalized_content_type


def normalize_extension(filename: str) -> str:
    """Return the lowercased file extension (no dot) or raise if missing."""

    suffix = PurePosixPath(filename).suffix.lstrip(".").lower()
    if not suffix:
        raise PipelineUnsupportedMediaTypeError("Uploaded filename must include an extension.")
    return suffix


def compare_telegram_post_ids(candidate: str, current: str | None) -> int:
    """Return 1 if ``candidate`` is strictly after ``current``, 0 if equal, -1 if before.

    Telegram channel message ids are integers that only ever grow, so the
    normal path parses both as ints. When the current checkpoint is absent
    the candidate always wins. The helper falls back to string comparison
    only when either value cannot be parsed as an int, which keeps the
    crawler resilient if a caller ever hands the service an opaque
    identifier (e.g. during tests).
    """

    if current is None:
        return 1
    try:
        candidate_int = int(candidate)
        current_int = int(current)
    except ValueError:
        if candidate == current:
            return 0
        return 1 if candidate > current else -1
    if candidate_int == current_int:
        return 0
    return 1 if candidate_int > current_int else -1


def build_sync_preview_model(
    payload_preview: Mapping[str, object],
    *,
    target: SyncTargetKind,
) -> ContentPipelineSyncTargetPreview:
    """Wrap a raw sync-target preview dict into the typed projection record.

    Shared by both the Qdrant and Meilisearch sync paths so every stored
    preview row goes through the same timestamp-stamping and shape contract
    before it is persisted. The runtime captures the raw dict from the
    post-upsert retrieve response (or from the upsert payload itself when
    retrieve is skipped); this helper stamps the typed target + timestamp so
    the schema contract is enforced at the persistence boundary.
    """

    return ContentPipelineSyncTargetPreview(
        target=target,
        preview_fields=dict(payload_preview),
        preview_fetched_at=utcnow(),
    )


def sorted_stage_entries(meme_file: MemeFile) -> tuple[PipelineStageJournal, ...]:
    """Return the meme file's stage journal entries ordered by stage progression."""

    return tuple(
        sorted(
            meme_file.pipeline_stage_journal_entries,
            key=lambda entry: STAGE_ORDER[entry.stage],
        )
    )


def resolve_current_stage(
    stage_entries: tuple[PipelineStageJournal, ...],
) -> PipelineStageJournal:
    """Return the active stage if any, falling back to the last row when all are done."""

    for stage_entry in stage_entries:
        if stage_entry.status in ACTIVE_STAGE_STATUSES:
            return stage_entry
    return stage_entries[-1]


def is_replay_reserved(stage_entry: PipelineStageJournal) -> bool:
    """Return ``True`` when the stage row was freshly reserved for operator replay."""

    return (
        stage_entry.status in {ContentPipelineStageStatus.PENDING, ContentPipelineStageStatus.PROCESSING}
        and stage_entry.normalized_reason == PIPELINE_REASON_REPLAY_REQUESTED
        and stage_entry.last_event_id is not None
    )


def snapshot_stage(stage_entry: PipelineStageJournal) -> StageJournalSnapshot:
    """Capture the current durable state so replay can restore it on publish failure."""

    return StageJournalSnapshot(
        status=stage_entry.status,
        attempt_count=stage_entry.attempt_count,
        last_event_id=stage_entry.last_event_id,
        normalized_reason=stage_entry.normalized_reason,
        last_error_text=stage_entry.last_error_text,
        is_retryable=stage_entry.is_retryable,
        retry_after=stage_entry.retry_after,
        started_at=stage_entry.started_at,
        finished_at=stage_entry.finished_at,
    )


def reserve_replay(
    stage_entry: PipelineStageJournal,
    replay_event: ContentPipelineDispatchEvent,
) -> None:
    """Mutate ``stage_entry`` in place to mark it reserved for an operator replay."""

    stage_entry.status = ContentPipelineStageStatus.PENDING
    stage_entry.attempt_count = replay_event.attempt
    stage_entry.last_event_id = replay_event.event_id
    stage_entry.normalized_reason = PIPELINE_REASON_REPLAY_REQUESTED
    stage_entry.last_error_text = None
    stage_entry.is_retryable = True
    stage_entry.retry_after = None
    stage_entry.started_at = None
    stage_entry.finished_at = None


def ensure_stage_attempt_is_current(
    stage_entry: PipelineStageJournal,
    *,
    attempt: int,
) -> None:
    """Raise when a stage transition arrives with a stale attempt counter."""

    if attempt < stage_entry.attempt_count:
        raise PipelineIngestError(
            "Received a stale stage transition for "
            f"{stage_entry.stage.value}: attempt {attempt} is behind durable "
            f"attempt {stage_entry.attempt_count}.",
        )


def matches_list_filter(
    current_entry: PipelineStageJournal,
    *,
    filter_by: ContentPipelineItemFilter,
    stale_before: datetime,
) -> bool:
    """Return ``True`` when the stage row satisfies the operator list filter."""

    if filter_by is ContentPipelineItemFilter.ALL:
        return True
    if filter_by is ContentPipelineItemFilter.DUPLICATE:
        return current_entry.status is ContentPipelineStageStatus.DUPLICATE
    if filter_by is ContentPipelineItemFilter.FAILED:
        return current_entry.status is ContentPipelineStageStatus.FAILED
    if filter_by is ContentPipelineItemFilter.STUCK:
        if current_entry.status not in {
            ContentPipelineStageStatus.PENDING,
            ContentPipelineStageStatus.PROCESSING,
        }:
            return False
        if current_entry.retry_after is not None:
            return current_entry.retry_after <= utcnow()
        return current_entry.updated_at <= stale_before
    return False


__all__ = [
    "StageJournalSnapshot",
    "build_sync_preview_model",
    "compare_telegram_post_ids",
    "ensure_stage_attempt_is_current",
    "is_replay_reserved",
    "matches_list_filter",
    "normalize_content_type",
    "normalize_extension",
    "normalize_filename",
    "reserve_replay",
    "resolve_current_stage",
    "snapshot_stage",
    "sorted_stage_entries",
    "translate_media_processing_error",
    "trim_error_text",
    "trim_reason",
]
