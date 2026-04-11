"""Stage ordering, routing maps, and normalized reason strings for the ingest service.

The content-pipeline service and its sibling modules (reporting, smoke,
runtime callbacks) all share the same stage graph and the same set of
normalized reason strings. Keeping these in a dedicated constants module
lets every sibling import them without re-declaring and keeps the main
service module free of top-of-file routing-table noise.
"""

from __future__ import annotations

from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    SyncTargetKind,
)
from memexpert.schemas.content_pipeline import ContentPipelineEventType

STAGE_ORDER: dict[ContentPipelineStage, int] = {
    ContentPipelineStage.INGEST: 0,
    ContentPipelineStage.TRANSCODE: 1,
    ContentPipelineStage.OCR: 2,
    ContentPipelineStage.EMBED: 3,
    ContentPipelineStage.CLASSIFY: 4,
    ContentPipelineStage.SYNC_QDRANT: 5,
    ContentPipelineStage.SYNC_MEILI: 6,
}

ACTIVE_STAGE_STATUSES: frozenset[ContentPipelineStageStatus] = frozenset(
    {
        ContentPipelineStageStatus.PENDING,
        ContentPipelineStageStatus.PROCESSING,
        ContentPipelineStageStatus.FAILED,
        ContentPipelineStageStatus.DUPLICATE,
    },
)

MAX_PERCEPTUAL_HASH_LENGTH = 64
DEFAULT_PIPELINE_ITEMS_LIMIT = 50
MAX_PIPELINE_ITEMS_LIMIT = 200
DEFAULT_STUCK_AFTER_SECONDS = 60

PIPELINE_REASON_PUBLISH_FAILED = "publish_failed"
PIPELINE_REASON_REPLAY_REQUESTED = "replay_requested"

# Maximum number of items accepted by the per-target batch replay endpoint.
# Kept small so operators cannot accidentally requeue an entire corpus in one
# call; the S03 runbook documents the repeated-call pattern for larger batches.
SYNC_REPLAY_BATCH_MAX = 10

# Normalized reason used when a per-target sync replay is intentionally queued
# by an operator. Distinct from ``PIPELINE_REASON_REPLAY_REQUESTED`` because
# the sync truth table is independent from the stage journal and the reason
# strings must not collide when operators filter either surface.
PIPELINE_REASON_SYNC_REPLAY_REQUESTED = "sync_replay_requested"

# Mirror of ``pipeline_runtime.constants.PIPELINE_REASON_SYNC_QDRANT_MALFORMED_PAYLOAD``.
# The service layer must never import from ``memexpert.workers`` because the
# worker runtime already depends on the service; duplicating the constant here
# with an explicit doc-comment is the narrowest way to share the one
# terminal-failure reason the Qdrant sync surface knows about.
PIPELINE_REASON_SYNC_QDRANT_MALFORMED_PAYLOAD = "sync_qdrant_malformed_payload"
# Mirror of ``pipeline_runtime.constants.PIPELINE_REASON_SYNC_MEILI_MALFORMED_PAYLOAD`` —
# same rationale as the Qdrant mirror above. The service layer honors the
# runtime's malformed-response classification verbatim so retryability stays
# consistent between the Qdrant and Meilisearch sync paths.
PIPELINE_REASON_SYNC_MEILI_MALFORMED_PAYLOAD = "sync_meili_malformed_payload"

# Default filename + content-type hints used when a ``RawCrawlerPost`` omits
# them. The media processor still validates the actual bytes and rejects
# anything unsupported, but it needs a non-None filename/content_type
# because both ``inspect_upload`` parameters are required. Keeping the
# mapping narrow (only the three crawler media types) is deliberate: if
# the crawler ever sends a media type outside this set, the service
# short-circuits with ``SKIPPED_UNSUPPORTED_MEDIA`` before we get here.
CRAWLER_MEDIA_DEFAULT_FILENAMES: dict[str, str] = {
    "photo": "telegram-photo.jpg",
    "gif": "telegram-animation.mp4",
    "video": "telegram-video.mp4",
}
CRAWLER_MEDIA_DEFAULT_CONTENT_TYPES: dict[str, str] = {
    "photo": "image/jpeg",
    "gif": "video/mp4",
    "video": "video/mp4",
}

DOWNSTREAM_STAGE_EVENT_TYPES: dict[ContentPipelineStage, ContentPipelineEventType] = {
    ContentPipelineStage.TRANSCODE: ContentPipelineEventType.MEME_TRANSCODED,
    ContentPipelineStage.OCR: ContentPipelineEventType.MEME_OCR_DONE,
    ContentPipelineStage.EMBED: ContentPipelineEventType.MEME_EMBEDDED,
    ContentPipelineStage.CLASSIFY: ContentPipelineEventType.MEME_READY,
}

# Single-successor mapping used by every heavy-worker stage EXCEPT classify.
# Classify fans out to BOTH sync targets so it intentionally does not appear
# here — see ``CLASSIFY_FAN_OUT_STAGES`` for the dual-target contract. The
# separation keeps ``_prepare_downstream_dispatches`` honest: anything in
# this dict goes through the single-target commit-then-publish flow, while
# classify uses the publish-then-commit atomic path.
NEXT_STAGE_BY_STAGE: dict[ContentPipelineStage, ContentPipelineStage] = {
    ContentPipelineStage.TRANSCODE: ContentPipelineStage.OCR,
    ContentPipelineStage.OCR: ContentPipelineStage.EMBED,
    ContentPipelineStage.EMBED: ContentPipelineStage.CLASSIFY,
}

# Classify success fans out to BOTH Qdrant and Meilisearch sync stages in a
# single atomic commit. The tuple is ordered so the stage-row + dispatch
# iteration is deterministic across test runs, which matters because the
# runtime publishes the events sequentially and any ordering drift would
# trigger test flakes.
CLASSIFY_FAN_OUT_STAGES: tuple[ContentPipelineStage, ...] = (
    ContentPipelineStage.SYNC_QDRANT,
    ContentPipelineStage.SYNC_MEILI,
)

# Each per-target sync target maps onto exactly one durable stage row so the
# replay surface can resolve the journal row for a given target without
# walking the full journal list in service callers.
SYNC_STAGE_BY_TARGET: dict[SyncTargetKind, ContentPipelineStage] = {
    SyncTargetKind.QDRANT: ContentPipelineStage.SYNC_QDRANT,
    SyncTargetKind.MEILISEARCH: ContentPipelineStage.SYNC_MEILI,
}

# Dispatch event emitted after a successful per-target sync commit. Keeping
# the mapping here lets the service express the Qdrant/Meilisearch success
# flow as one target-parameterized method instead of two nearly-identical
# twins, and it stays close to ``SYNC_STAGE_BY_TARGET`` so adding a new
# target is a single-place edit.
SYNC_SUCCESS_EVENT_TYPE_BY_TARGET: dict[SyncTargetKind, ContentPipelineEventType] = {
    SyncTargetKind.QDRANT: ContentPipelineEventType.MEME_QDRANT_SYNCED,
    SyncTargetKind.MEILISEARCH: ContentPipelineEventType.MEME_MEILI_SYNCED,
}

# Normalized-reason marker that classifies a per-target sync failure as
# non-retryable. The runtime owns the exception-to-reason mapping and the
# service honors it verbatim, so we need a per-target lookup to decide
# retryability without hard-coding Qdrant/Meilisearch literals in the
# service flow.
SYNC_MALFORMED_REASON_BY_TARGET: dict[SyncTargetKind, str] = {
    SyncTargetKind.QDRANT: PIPELINE_REASON_SYNC_QDRANT_MALFORMED_PAYLOAD,
    SyncTargetKind.MEILISEARCH: PIPELINE_REASON_SYNC_MEILI_MALFORMED_PAYLOAD,
}


__all__ = [
    "ACTIVE_STAGE_STATUSES",
    "CLASSIFY_FAN_OUT_STAGES",
    "CRAWLER_MEDIA_DEFAULT_CONTENT_TYPES",
    "CRAWLER_MEDIA_DEFAULT_FILENAMES",
    "DEFAULT_PIPELINE_ITEMS_LIMIT",
    "DEFAULT_STUCK_AFTER_SECONDS",
    "DOWNSTREAM_STAGE_EVENT_TYPES",
    "MAX_PERCEPTUAL_HASH_LENGTH",
    "MAX_PIPELINE_ITEMS_LIMIT",
    "NEXT_STAGE_BY_STAGE",
    "PIPELINE_REASON_PUBLISH_FAILED",
    "PIPELINE_REASON_REPLAY_REQUESTED",
    "PIPELINE_REASON_SYNC_MEILI_MALFORMED_PAYLOAD",
    "PIPELINE_REASON_SYNC_QDRANT_MALFORMED_PAYLOAD",
    "PIPELINE_REASON_SYNC_REPLAY_REQUESTED",
    "STAGE_ORDER",
    "SYNC_MALFORMED_REASON_BY_TARGET",
    "SYNC_REPLAY_BATCH_MAX",
    "SYNC_STAGE_BY_TARGET",
    "SYNC_SUCCESS_EVENT_TYPE_BY_TARGET",
]
