"""Focused tests for browser-admin source indexing classification."""

from memexpert.models.enums import (
    ContentPipelineStageStatus,
    PipelineIngestRequestStatus,
    SourceAttachReason,
    SourceChannelPostStatus,
    SyncTargetStatus,
)
from memexpert.services.admin import AdminService


def test_source_post_index_status_requires_both_search_targets() -> None:
    assert (
        AdminService._source_post_index_status(
            post_status=SourceChannelPostStatus.ACCEPTED,
            ingest_status=PipelineIngestRequestStatus.MATERIALIZED,
            source_attach_reason=SourceAttachReason.NEW_FILE,
            pipeline_status=ContentPipelineStageStatus.SUCCEEDED,
            qdrant_status=SyncTargetStatus.SYNCED,
            meilisearch_status=SyncTargetStatus.SYNCED,
        )
        == "indexed"
    )
    assert (
        AdminService._source_post_index_status(
            post_status=SourceChannelPostStatus.ACCEPTED,
            ingest_status=PipelineIngestRequestStatus.MATERIALIZED,
            source_attach_reason=SourceAttachReason.NEW_FILE,
            pipeline_status=ContentPipelineStageStatus.FAILED,
            qdrant_status=SyncTargetStatus.SYNCED,
            meilisearch_status=SyncTargetStatus.FAILED,
        )
        == "partially_indexed"
    )


def test_source_post_index_status_distinguishes_skips_failures_and_processing() -> None:
    assert (
        AdminService._source_post_index_status(
            post_status=SourceChannelPostStatus.UNSUPPORTED,
            ingest_status=None,
            source_attach_reason=None,
            pipeline_status=None,
            qdrant_status=None,
            meilisearch_status=None,
        )
        == "not_indexable"
    )
    assert (
        AdminService._source_post_index_status(
            post_status=SourceChannelPostStatus.ACCEPTED,
            ingest_status=PipelineIngestRequestStatus.FAILED_INVALID_MEDIA,
            source_attach_reason=None,
            pipeline_status=None,
            qdrant_status=None,
            meilisearch_status=None,
        )
        == "failed"
    )
    assert (
        AdminService._source_post_index_status(
            post_status=SourceChannelPostStatus.OBSERVED,
            ingest_status=PipelineIngestRequestStatus.MEDIA_INSPECT_PENDING,
            source_attach_reason=None,
            pipeline_status=ContentPipelineStageStatus.PENDING,
            qdrant_status=SyncTargetStatus.PENDING,
            meilisearch_status=SyncTargetStatus.PENDING,
        )
        == "processing"
    )


def test_source_post_index_status_marks_blocked_sha_duplicate_not_indexable() -> None:
    assert (
        AdminService._source_post_index_status(
            post_status=SourceChannelPostStatus.ACCEPTED,
            ingest_status=PipelineIngestRequestStatus.RESOLVED_SHA_DUPLICATE,
            source_attach_reason=SourceAttachReason.BLOCKED_SHA256_EXISTING_FILE,
            pipeline_status=None,
            qdrant_status=None,
            meilisearch_status=None,
        )
        == "not_indexable"
    )
