"""Normalized failure-reason codes and dead-letter payload type for the pipeline runtime.

These constants mirror (intentionally) the ``PIPELINE_REASON_*`` strings the
service layer writes into ``PipelineStageJournal.normalized_reason``. We keep
the mirror separate from :mod:`memexpert.services.content_pipeline` to avoid a
cyclical worker → service → worker import chain. Any reason string added here
must also be recognised by the service layer's replay allow-list if it should
be treated as retryable.
"""

from __future__ import annotations

PIPELINE_REASON_CLASSIFY_FAILED = "classify_stage_failed"
PIPELINE_REASON_CLASSIFY_MALFORMED = "classify_malformed_result"
PIPELINE_REASON_CLASSIFY_PROVIDER_BLOCKED = "classify_provider_blocked"
PIPELINE_REASON_CLASSIFY_TIMEOUT = "classify_timeout"
PIPELINE_REASON_EMBED_FAILED = "embed_stage_failed"
PIPELINE_REASON_EMBED_MALFORMED_VECTOR = "embed_malformed_vector"
PIPELINE_REASON_EMBED_MERGE_TRANSACTION = "embed_merge_transaction_failed"
PIPELINE_REASON_EMBED_PROVIDER_BLOCKED = "embed_provider_blocked"
PIPELINE_REASON_EMBED_SIMILARITY_BLOCKED = "embed_similarity_blocked"
PIPELINE_REASON_EMBED_SIMILARITY_MALFORMED = "embed_similarity_malformed"
PIPELINE_REASON_EMBED_SIMILARITY_TIMEOUT = "embed_similarity_timeout"
PIPELINE_REASON_EMBED_TIMEOUT = "embed_timeout"
PIPELINE_REASON_FORCED_CLASSIFY_FAILURE = "forced_classify_failure"
PIPELINE_REASON_FORCED_EMBED_FAILURE = "forced_embed_failure"
PIPELINE_REASON_FORCED_SYNC_MEILI_FAILURE = "forced_sync_meili_failure"
PIPELINE_REASON_FORCED_SYNC_QDRANT_FAILURE = "forced_sync_qdrant_failure"
PIPELINE_REASON_FORCED_TRANSCODE_FAILURE = "forced_transcode_failure"
PIPELINE_REASON_MALFORMED_EVENT = "malformed_dispatch_event"
PIPELINE_REASON_OCR_FAILED = "ocr_stage_failed"
PIPELINE_REASON_OCR_PROVIDER_BLOCKED = "ocr_provider_blocked"
PIPELINE_REASON_OCR_TIMEOUT = "ocr_timeout"
PIPELINE_REASON_SYNC_MEILI_CONFLICT = "sync_meili_conflict"
PIPELINE_REASON_SYNC_MEILI_MALFORMED_PAYLOAD = "sync_meili_malformed_payload"
PIPELINE_REASON_SYNC_MEILI_PROVIDER_BLOCKED = "sync_meili_provider_blocked"
PIPELINE_REASON_SYNC_MEILI_TIMEOUT = "sync_meili_timeout"
PIPELINE_REASON_SYNC_QDRANT_CONFLICT = "sync_qdrant_conflict"
PIPELINE_REASON_SYNC_QDRANT_MALFORMED_PAYLOAD = "sync_qdrant_malformed_payload"
PIPELINE_REASON_SYNC_QDRANT_PROVIDER_BLOCKED = "sync_qdrant_provider_blocked"
PIPELINE_REASON_SYNC_QDRANT_TIMEOUT = "sync_qdrant_timeout"
PIPELINE_REASON_TRANSCODE_FAILED = "transcode_stage_failed"
PIPELINE_REASON_TRANSCODE_INVALID_MEDIA = "transcode_invalid_media"
PIPELINE_REASON_TRANSCODE_TIMEOUT = "transcode_timeout"
PIPELINE_REASON_UNSUPPORTED_STAGE = "unsupported_stage"

type DeadLetterPayload = str | bytes | bytearray | int | float | bool | None
