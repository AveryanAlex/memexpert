# ruff: noqa: I001
"""Integration tests for ORM metadata registration and public schema contracts."""

from __future__ import annotations

import uuid
import ast
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, Table, UniqueConstraint, inspect as sa_inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import configure_mappers, selectinload

from memexpert.models import metadata, utcnow
from memexpert.models.collection import Collection, CollectionInvite, CollectionMember, CollectionMeme, PinnedMeme
from memexpert.models.content import (
    AdminMemeDestructiveAuditLog,
    BlockedPerceptualHash,
    BlockedPerceptualHashAuditLog,
    EmbeddingCache,
    Meme,
    MemeFile,
    MemeFileOCRResult,
    MemeFileSyncTargetSnapshot,
    MemeMergeLog,
    MemeOfTheDaySelection,
    MemeSeoPage,
    MemeSource,
    MemeSourceEngagementSnapshot,
    MemeTemplate,
    ModerationDecision,
    ModerationReport,
    PipelineIngestRequest,
    PipelineStageJournal,
    RabbitMQOutboxMessage,
    SourceChannel,
    SourceChannelBackfillJob,
    SourceChannelPost,
    TelegramAdminAuditLog,
    TelegramFileIdCache,
    TelegramSession,
    TelegramSessionLoginAttempt,
)
from memexpert.models.enums import (
    AccountDeletionAction,
    AccountStatus,
    AccountType,
    AnalyticsEventType,
    CollectionInviteChannel,
    CollectionInviteStatus,
    CollectionKind,
    CollectionMembershipRole,
    CollectionVisibility,
    ContentKind,
    ContentLanguage,
    MemeVisibilityMode,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    ContentProcessingStatus,
    EmbeddingInputType,
    ModerationAction,
    ModerationReason,
    ModerationReportStatus,
    SearchSynonymLocale,
    SearchSynonymRevisionStatus,
    SearchSynonymSyncStatus,
    SourceChannelBackfillJobStatus,
    SourceChannelPostStatus,
    SourceEngagementCaptureReason,
    SourceEngagementCommentsState,
    SourceEngagementFetchStatus,
    SourceEngagementScheduleLabel,
    SourcePlatform,
    SyncTargetKind,
    SyncTargetStatus,
    TelegramMediaFormat,
    TelegramSessionStatus,
    UserLanguage,
)
from memexpert.models.search_synonyms import (
    SearchSynonymCatalog,
    SearchSynonymRevision,
    SearchSynonymSyncState,
)
from memexpert.models.user import (
    AccountDeletionLog,
    AccountMergeLog,
    AnalyticsEvent,
    ChannelSuggestion,
    InlineUsageEvent,
    LoginEvent,
    TelegramLinkCode,
    User,
)
from memexpert.schemas import (
    CollectionRead,
    ContentPipelineDispatchEvent,
    ContentPipelineEventType,
    ContentPipelineStageJournalRead,
    UserRead,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

EXPECTED_TABLES = {
    "admin_meme_destructive_audit_logs",
    "account_deletion_logs",
    "account_merge_logs",
    "analytics_events",
    "blocked_perceptual_hash_audit_logs",
    "blocked_perceptual_hashes",
    "channel_suggestions",
    "collection_invites",
    "collection_members",
    "collection_memes",
    "collections",
    "dependency_circuit_states",
    "embedding_cache",
    "inline_usage_events",
    "meme_file_ocr_results",
    "meme_file_sync_target_snapshots",
    "login_events",
    "meme_files",
    "meme_merge_logs",
    "meme_of_the_day_selections",
    "meme_seo_pages",
    "meme_source_engagement_snapshots",
    "meme_sources",
    "meme_templates",
    "memes",
    "moderation_decisions",
    "moderation_reports",
    "operational_audit_logs",
    "pinned_memes",
    "pipeline_capacity_states",
    "pipeline_dead_letters",
    "pipeline_ingest_requests",
    "pipeline_stage_attempts",
    "pipeline_stage_journal",
    "rabbitmq_outbox_messages",
    "recovery_job_items",
    "recovery_jobs",
    "runtime_heartbeats",
    "search_synonym_catalogs",
    "search_synonym_revisions",
    "search_synonym_sync_states",
    "source_channels",
    "source_channel_backfill_attempts",
    "source_channel_backfill_jobs",
    "source_channel_posts",
    "telegram_admin_audit_logs",
    "telegram_file_id_cache",
    "telegram_link_codes",
    "telegram_session_login_attempts",
    "telegram_sessions",
    "users",
}
REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_DIRECT_BROKER_PUBLISH_CALLS = {
    ("memexpert/messaging/rabbitmq_outbox.py", "publish_rabbit_message_direct"),
    ("memexpert/workers/pipeline_runtime/runtime.py", "_dead_letter_or_requeue"),
}


class _BrokerPublishCallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.function_stack: list[str] = []
        self.calls: list[tuple[str, int]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        _ = self.function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        _ = self.function_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if _is_direct_broker_publish_call(node):
            self.calls.append((self.function_stack[-1] if self.function_stack else "<module>", node.lineno))
        self.generic_visit(node)


def _is_direct_broker_publish_call(node: ast.Call) -> bool:
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "publish":
        return False
    return _receiver_references_broker(node.func.value)


def _receiver_references_broker(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return "broker" in node.id
    if isinstance(node, ast.Attribute):
        return "broker" in node.attr or _receiver_references_broker(node.value)
    return False


@pytest_asyncio.fixture
async def model_contract_session_factory(
    postgres_async_engine: AsyncEngine,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create a fresh metadata-managed schema for each model-contract test."""

    await _create_metadata_schema(postgres_async_engine)

    try:
        yield postgres_session_factory
    finally:
        await _reset_public_schema(postgres_async_engine)


async def _create_metadata_schema(engine: AsyncEngine) -> None:
    await _reset_public_schema(engine)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)


async def _reset_public_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        _ = await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        _ = await connection.execute(text("CREATE SCHEMA public"))


async def _get_table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return await connection.run_sync(_get_table_names_sync)


def _get_table_names_sync(sync_connection: Connection) -> set[str]:
    return set(sa_inspect(sync_connection).get_table_names())


def _postgresql_where(index_name: str, table_name: str) -> str | None:
    table = metadata.tables[table_name]
    for index in table.indexes:
        if index.name == index_name:
            where_clause = index.dialect_options["postgresql"].get("where")
            return None if where_clause is None else str(where_clause)
    return None


def test_metadata_registers_all_expected_tables_and_relationships() -> None:
    configure_mappers()

    assert set(metadata.tables) == EXPECTED_TABLES
    memes_table = metadata.tables["memes"]
    meme_files_table = metadata.tables["meme_files"]
    motd_table = metadata.tables["meme_of_the_day_selections"]
    pipeline_ingest_requests_table = metadata.tables["pipeline_ingest_requests"]
    pipeline_stage_attempts_table = metadata.tables["pipeline_stage_attempts"]
    synonym_catalogs_table = metadata.tables["search_synonym_catalogs"]
    synonym_revisions_table = metadata.tables["search_synonym_revisions"]
    synonym_sync_states_table = metadata.tables["search_synonym_sync_states"]
    assert "invite_link" not in metadata.tables["collections"].c
    assert metadata.tables["users"].c["active_save_collection_id"].foreign_keys
    assert metadata.tables["collections"].c["owner_id"].foreign_keys
    assert not memes_table.c["primary_file_id"].nullable
    assert "popularity_score" not in memes_table.c
    assert meme_files_table.c["meme_id"].foreign_keys
    assert "author_user_id" not in memes_table.c
    assert memes_table.c["visibility_mode"].nullable is False
    assert "owner_user_id" not in pipeline_ingest_requests_table.c
    assert pipeline_ingest_requests_table.c["uploader_user_id"].foreign_keys
    assert pipeline_ingest_requests_table.c["materialized_meme_id"].foreign_keys
    assert pipeline_ingest_requests_table.c["materialized_meme_file_id"].foreign_keys
    assert pipeline_ingest_requests_table.c["matched_meme_file_id"].foreign_keys
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_meme_files_meme_id_id"
        and [column.name for column in constraint.columns] == ["meme_id", "id"]
        for constraint in meme_files_table.constraints
    )
    assert _postgresql_where("uq_meme_files_sha256_hex_not_null", "meme_files") == ("sha256_hex IS NOT NULL")
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_motd_selected_for_algorithm_version"
        and [column.name for column in constraint.columns] == ["selected_for", "algorithm_version"]
        for constraint in motd_table.constraints
    )
    assert any(
        isinstance(constraint, CheckConstraint) and "candidate_count >= 0" in str(constraint.sqltext)
        for constraint in motd_table.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_pipeline_stage_attempts_file_stage_event_attempt"
        and [column.name for column in constraint.columns] == ["meme_file_id", "stage", "event_id", "attempt_number"]
        for constraint in pipeline_stage_attempts_table.constraints
    )
    assert motd_table.c["meme_id"].nullable
    motd_meme_fk = next(iter(motd_table.c["meme_id"].foreign_keys))
    assert motd_meme_fk.column.table.name == "memes"
    assert motd_meme_fk.column.name == "id"
    assert motd_meme_fk.ondelete == "SET NULL"
    primary_file_fk = next(
        constraint
        for constraint in memes_table.foreign_key_constraints
        if constraint.name == "fk_memes_primary_file_id_meme_files"
    )
    assert [column.name for column in primary_file_fk.columns] == ["id", "primary_file_id"]
    assert [(element.column.table.name, element.column.name) for element in primary_file_fk.elements] == [
        ("meme_files", "meme_id"),
        ("meme_files", "id"),
    ]
    assert primary_file_fk.deferrable is True
    assert primary_file_fk.initially == "DEFERRED"
    assert _postgresql_where("uq_collections_one_favorites_per_owner", "collections") == "kind = 'favorites'"
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_search_synonym_catalogs_locale"
        for constraint in synonym_catalogs_table.constraints
    )
    assert _postgresql_where(
        "uq_search_synonym_revisions_one_draft",
        "search_synonym_revisions",
    ) == "status = 'draft'"
    assert _postgresql_where(
        "uq_search_synonym_revisions_one_published",
        "search_synonym_revisions",
    ) == "status = 'published'"
    assert synonym_revisions_table.c["catalog_id"].foreign_keys
    assert synonym_revisions_table.c["created_by_admin_user_id"].foreign_keys
    assert synonym_revisions_table.c["published_by_admin_user_id"].foreign_keys
    assert synonym_sync_states_table.c["id"].primary_key

    user_relationships = sa_inspect(User).relationships
    meme_relationships = sa_inspect(Meme).relationships
    meme_file_relationships = sa_inspect(MemeFile).relationships
    motd_relationships = sa_inspect(MemeOfTheDaySelection).relationships
    meme_source_relationships = sa_inspect(MemeSource).relationships
    meme_source_columns = sa_inspect(MemeSource).columns
    source_channel_relationships = sa_inspect(SourceChannel).relationships
    telegram_session_relationships = sa_inspect(TelegramSession).relationships
    telegram_login_attempt_table = metadata.tables["telegram_session_login_attempts"]
    pipeline_ingest_request_relationships = sa_inspect(PipelineIngestRequest).relationships
    synonym_catalog_relationships = sa_inspect(SearchSynonymCatalog).relationships
    synonym_revision_relationships = sa_inspect(SearchSynonymRevision).relationships

    assert user_relationships["active_save_collection"].mapper.class_ is Collection
    assert user_relationships["owned_collections"].mapper.class_ is Collection
    assert user_relationships["telegram_link_codes"].mapper.class_ is TelegramLinkCode
    assert meme_relationships["files"].mapper.class_ is MemeFile
    assert meme_relationships["primary_file"].mapper.class_ is MemeFile
    assert "popularity_snapshots" not in meme_relationships
    assert meme_relationships["moderation_reports"].mapper.class_ is ModerationReport
    assert meme_relationships["moderation_decisions"].mapper.class_ is ModerationDecision
    assert meme_relationships["motd_selections"].mapper.class_ is MemeOfTheDaySelection
    assert motd_relationships["meme"].mapper.class_ is Meme
    assert pipeline_ingest_request_relationships["materialized_meme"].mapper.class_ is Meme
    assert pipeline_ingest_request_relationships["materialized_meme_file"].mapper.class_ is MemeFile
    assert pipeline_ingest_request_relationships["matched_meme_file"].mapper.class_ is MemeFile
    assert synonym_catalog_relationships["revisions"].mapper.class_ is SearchSynonymRevision
    assert synonym_revision_relationships["catalog"].mapper.class_ is SearchSynonymCatalog
    assert sa_inspect(SearchSynonymSyncState).primary_key[0].name == "id"
    rabbitmq_outbox_columns = sa_inspect(RabbitMQOutboxMessage).columns
    assert rabbitmq_outbox_columns["aggregate_id"] is not None
    assert rabbitmq_outbox_columns["message_id"] is not None
    assert metadata.tables["admin_meme_destructive_audit_logs"].c["admin_user_id"].foreign_keys
    assert metadata.tables["telegram_admin_audit_logs"].c["admin_user_id"].foreign_keys
    assert metadata.tables["blocked_perceptual_hashes"].c["created_by_admin_user_id"].foreign_keys
    assert metadata.tables["blocked_perceptual_hash_audit_logs"].c["admin_user_id"].foreign_keys
    assert metadata.tables["meme_files"].c["blocked_perceptual_hash_id"].foreign_keys
    assert user_relationships["moderation_reports_submitted"].mapper.class_ is ModerationReport
    assert user_relationships["moderation_reports_resolved"].mapper.class_ is ModerationReport
    assert user_relationships["moderation_decisions"].mapper.class_ is ModerationDecision
    assert meme_file_relationships["pipeline_stage_journal_entries"].mapper.class_ is PipelineStageJournal
    assert meme_file_relationships["ocr_result"].mapper.class_ is MemeFileOCRResult
    assert meme_file_relationships["sync_target_snapshots"].mapper.class_ is MemeFileSyncTargetSnapshot
    assert meme_file_relationships["blocked_perceptual_hash"].mapper.class_ is BlockedPerceptualHash
    assert meme_source_relationships["engagement_snapshots"].mapper.class_ is MemeSourceEngagementSnapshot
    assert source_channel_relationships["telegram_session"].mapper.class_ is TelegramSession
    assert telegram_session_relationships["source_channels"].mapper.class_ is SourceChannel
    assert telegram_session_relationships["login_attempts"].mapper.class_ is TelegramSessionLoginAttempt
    assert metadata.tables["source_channels"].c["telegram_session_id"].foreign_keys
    assert telegram_login_attempt_table.c["telegram_session_id"].nullable
    telegram_session_fk = next(iter(telegram_login_attempt_table.c["telegram_session_id"].foreign_keys))
    assert telegram_session_fk.ondelete == "SET NULL"
    assert telegram_login_attempt_table.c["created_by_admin_user_id"].nullable
    created_by_fk = next(iter(telegram_login_attempt_table.c["created_by_admin_user_id"].foreign_keys))
    assert created_by_fk.column.table.name == "users"
    assert created_by_fk.ondelete == "SET NULL"
    assert {
        "cleanup_status",
        "cleanup_attempts",
        "cleanup_error_class",
        "cleanup_error_text",
        "cleanup_completed_at",
    }.issubset(telegram_login_attempt_table.c.keys())
    assert (
        _postgresql_where(
            "uq_telegram_sessions_account_user_id_not_null",
            "telegram_sessions",
        )
        == "account_user_id IS NOT NULL"
    )
    assert "session_id" not in metadata.tables["source_channels"].c
    assert "views" not in meme_source_columns
    assert "reactions" not in meme_source_columns
    assert sa_inspect(BlockedPerceptualHashAuditLog).columns["blocked_perceptual_hash_id"] is not None
    assert sa_inspect(TelegramAdminAuditLog).columns["telegram_session_id"] is not None


def test_direct_broker_publish_calls_stay_grep_auditable() -> None:
    direct_publish_calls: set[tuple[str, str]] = set()
    for path in sorted((REPO_ROOT / "memexpert").rglob("*.py")):
        relative_path = path.relative_to(REPO_ROOT).as_posix()
        visitor = _BrokerPublishCallVisitor()
        visitor.visit(ast.parse(path.read_text(), filename=str(path)))
        direct_publish_calls.update((relative_path, function_name) for function_name, _ in visitor.calls)

    assert direct_publish_calls == ALLOWED_DIRECT_BROKER_PUBLISH_CALLS


async def test_metadata_creates_full_schema_on_postgres(postgres_async_engine: AsyncEngine) -> None:
    await _create_metadata_schema(postgres_async_engine)

    assert EXPECTED_TABLES.issubset(await _get_table_names(postgres_async_engine))

    await _reset_public_schema(postgres_async_engine)


async def test_user_admin_flag_defaults_false_in_memory_and_when_persisted(
    model_contract_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = User()

    assert user.is_admin is False

    async with model_contract_session_factory() as session:
        session.add(user)
        await session.commit()
        await session.refresh(user)

        assert user.is_admin is False


async def test_moderation_report_and_decision_orm_persist_admin_audit_history(
    model_contract_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with model_contract_session_factory() as session:
        reporter = User(email="reporter@example.com")
        admin = User(email="moderator@example.com", is_admin=True)
        meme_id = uuid.uuid7()
        meme_file_id = uuid.uuid7()
        meme = Meme(
            id=meme_id,
            media_type=ContentKind.IMAGE,
            primary_file_id=meme_file_id,
            is_public=True,
            is_nsfw=False,
        )
        meme_file = MemeFile(
            id=meme_file_id,
            meme_id=meme_id,
            status=ContentProcessingStatus.READY,
            s3_original_key="files/report/original.jpg",
        )
        report = ModerationReport(
            meme=meme,
            reporter_user=reporter,
            reason=ModerationReason.NSFW,
            note="Looks explicit",
        )
        session.add_all([reporter, admin, meme])
        await session.flush()
        session.add(meme_file)
        await session.flush()
        session.add(report)
        await session.flush()

        report.status = ModerationReportStatus.RESOLVED
        report.resolved_by_admin_user = admin
        report.resolved_at = utcnow()
        session.add(
            ModerationDecision(
                meme=meme,
                report=report,
                admin_user=admin,
                action=ModerationAction.MARK_NSFW,
                reason=ModerationReason.NSFW,
                note="Confirmed by admin",
                previous_is_public=True,
                previous_visibility_mode=MemeVisibilityMode.AUTO,
                previous_is_nsfw=False,
                new_is_public=True,
                new_visibility_mode=MemeVisibilityMode.AUTO,
                new_is_nsfw=True,
            ),
        )
        await session.commit()

        persisted = await session.scalar(
            select(ModerationReport)
            .options(selectinload(ModerationReport.decisions), selectinload(ModerationReport.meme))
            .where(ModerationReport.id == report.id)
        )

        assert persisted is not None
        assert persisted.status is ModerationReportStatus.RESOLVED
        assert persisted.reason is ModerationReason.NSFW
        assert persisted.resolved_by_admin_user_id == admin.id
        assert persisted.meme.id == meme.id
        assert len(persisted.decisions) == 1
        assert persisted.decisions[0].action is ModerationAction.MARK_NSFW
        assert persisted.decisions[0].previous_is_nsfw is False
        assert persisted.decisions[0].new_is_nsfw is True


async def test_schema_handles_cycles_multi_invites_and_nullable_content_fields(
    model_contract_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with model_contract_session_factory() as session:
        owner = User(
            status=AccountStatus.ACTIVE,
            email="owner@example.com",
            language=UserLanguage.ANY,
            last_active_at=utcnow(),
        )
        session.add(owner)
        await session.flush()

        favorites = Collection(
            owner=owner,
            title="Favorites",
            kind=CollectionKind.FAVORITES,
            visibility=CollectionVisibility.PRIVATE,
        )
        shared = Collection(
            owner=owner,
            title="Work Reactions",
            description="Shared board",
            kind=CollectionKind.CUSTOM,
            visibility=CollectionVisibility.UNLISTED,
        )
        session.add_all([favorites, shared])
        await session.flush()

        owner.active_save_collection = favorites
        session.add_all(
            [
                CollectionMember(collection=favorites, user=owner, role=CollectionMembershipRole.OWNER),
                CollectionMember(collection=shared, user=owner, role=CollectionMembershipRole.OWNER),
                CollectionInvite(
                    collection=shared,
                    created_by_user=owner,
                    token_hash="a" * 64,
                    role=CollectionMembershipRole.EDITOR,
                    channel=CollectionInviteChannel.DIRECT_LINK,
                    status=CollectionInviteStatus.PENDING,
                    max_uses=10,
                ),
                CollectionInvite(
                    collection=shared,
                    created_by_user=owner,
                    token_hash="b" * 64,
                    role=CollectionMembershipRole.VIEWER,
                    channel=CollectionInviteChannel.EMAIL,
                    label="Email viewers",
                    recipient_email="viewer@example.com",
                    status=CollectionInviteStatus.PENDING,
                ),
            ]
        )

        template = MemeTemplate(
            slug="drake-template",
            name="Drake Hotline Bling",
            description=None,
            is_curated=False,
            base_image_url=None,
            text_regions=None,
        )
        meme_id = uuid.uuid7()
        file_one_id = uuid.uuid7()
        file_two_id = uuid.uuid7()
        meme = Meme(
            id=meme_id,
            media_type=ContentKind.IMAGE,
            primary_file_id=file_one_id,
            language=ContentLanguage.EN,
            is_public=False,
            template=template,
            tags=["reaction", "deadline"],
        )
        file_one = MemeFile(
            id=file_one_id,
            meme_id=meme_id,
            status=ContentProcessingStatus.READY,
            width=1200,
            height=900,
            file_size_bytes=1024,
            mime_type="image/jpeg",
            s3_original_key="files/1/original.jpg",
            perceptual_hash="c" * 16,
            quality_score=1.0,
        )
        file_two = MemeFile(
            id=file_two_id,
            meme_id=meme_id,
            status=ContentProcessingStatus.READY,
            width=1080,
            height=810,
            file_size_bytes=900,
            mime_type="image/png",
            s3_original_key="files/2/original.png",
            quality_score=0.8,
        )
        session.add_all([template, meme])
        await session.flush()
        session.add_all([file_one, file_two])
        await session.flush()
        session.add_all(
            [
                MemeSeoPage(
                    meme=meme,
                    slug="drake-monday-deadline",
                    page_title="Drake Monday Deadline Meme",
                    meta_description="A deadline reaction meme.",
                    alt_text="Drake rejecting then approving",
                    caption=None,
                    body_text=None,
                    tags=["deadline", "reaction"],
                    model_id="gemini/gemini-2.5-flash",
                    prompt_version="seo-v3",
                ),
                MemeSource(
                    file=file_one,
                    platform=SourcePlatform.TELEGRAM,
                    source_id="memes_channel",
                    post_id="12345",
                    is_first_source=True,
                    engagement_snapshots=[
                        MemeSourceEngagementSnapshot(
                            captured_at=utcnow(),
                            capture_reason=SourceEngagementCaptureReason.INGEST_INITIAL,
                            view_count=321,
                            reactions={"like": 10},
                            reaction_count=10,
                            comment_count=None,
                            forward_count=4,
                            comments_state=SourceEngagementCommentsState.UNKNOWN,
                            fetch_status=SourceEngagementFetchStatus.SUCCESS,
                            source_alive=True,
                            raw_metrics={"source": "contract"},
                        )
                    ],
                ),
                TelegramFileIdCache(
                    meme_file=file_one,
                    bot_scope="main-bot",
                    media_format=TelegramMediaFormat.PHOTO,
                    telegram_file_id="AgACAgIAAxkBAAIBQGmock",
                    telegram_file_unique_id="AQADmock",
                ),
                MemeFileOCRResult(
                    meme_file=file_one,
                    engine="paddleocr",
                    fallback_engine="ocr-command",
                    fallback_used=False,
                    low_confidence=False,
                    confidence=0.93,
                    language=ContentLanguage.EN,
                    extracted_text="deadline monday",
                    source_object_key="pipeline/derived/file-one/web.mp4",
                    last_event_id=uuid.uuid7(),
                ),
                EmbeddingCache(
                    input_hash="d" * 64,
                    input_type=EmbeddingInputType.IMAGE,
                    embedding=b"x" * 4096,
                    model_version="voyage-multimodal-3.5",
                    source_file=file_one,
                ),
                PipelineStageJournal(
                    meme_file=file_one,
                    stage=ContentPipelineStage.INGEST,
                    status=ContentPipelineStageStatus.SUCCEEDED,
                    attempt_count=1,
                    last_event_id=uuid.uuid7(),
                    is_retryable=False,
                    started_at=utcnow(),
                    finished_at=utcnow(),
                ),
                PipelineStageJournal(
                    meme_file=file_one,
                    stage=ContentPipelineStage.TRANSCODE,
                    status=ContentPipelineStageStatus.SUCCEEDED,
                    attempt_count=1,
                    last_event_id=uuid.uuid7(),
                    is_retryable=False,
                    started_at=utcnow(),
                    finished_at=utcnow(),
                ),
                PipelineStageJournal(
                    meme_file=file_one,
                    stage=ContentPipelineStage.OCR,
                    status=ContentPipelineStageStatus.SUCCEEDED,
                    attempt_count=1,
                    last_event_id=uuid.uuid7(),
                    is_retryable=False,
                    started_at=utcnow(),
                    finished_at=utcnow(),
                ),
                PipelineStageJournal(
                    meme_file=file_one,
                    stage=ContentPipelineStage.EMBED,
                    status=ContentPipelineStageStatus.SUCCEEDED,
                    attempt_count=1,
                    last_event_id=uuid.uuid7(),
                    is_retryable=False,
                    started_at=utcnow(),
                    finished_at=utcnow(),
                ),
                PipelineStageJournal(
                    meme_file=file_one,
                    stage=ContentPipelineStage.CLASSIFY,
                    status=ContentPipelineStageStatus.SUCCEEDED,
                    attempt_count=1,
                    last_event_id=uuid.uuid7(),
                    is_retryable=False,
                    started_at=utcnow(),
                    finished_at=utcnow(),
                ),
                PipelineStageJournal(
                    meme_file=file_one,
                    stage=ContentPipelineStage.SYNC_QDRANT,
                    status=ContentPipelineStageStatus.PENDING,
                    attempt_count=0,
                    last_event_id=uuid.uuid7(),
                    is_retryable=True,
                ),
                PipelineStageJournal(
                    meme_file=file_one,
                    stage=ContentPipelineStage.SYNC_MEILI,
                    status=ContentPipelineStageStatus.FAILED,
                    attempt_count=2,
                    last_event_id=uuid.uuid7(),
                    normalized_reason="target_sync_failed",
                    last_error_text="sync dispatch failed",
                    is_retryable=True,
                    retry_after=utcnow() + timedelta(minutes=1),
                    started_at=utcnow(),
                ),
                PipelineStageJournal(
                    meme_file=file_two,
                    stage=ContentPipelineStage.INGEST,
                    status=ContentPipelineStageStatus.DUPLICATE,
                    attempt_count=1,
                    last_event_id=uuid.uuid7(),
                    normalized_reason="duplicate_perceptual_hash",
                    is_retryable=False,
                    finished_at=utcnow(),
                ),
                MemeMergeLog(
                    source_meme_id=uuid.uuid7(),
                    source_meme_file_id=file_two.id,
                    target_meme_id=meme.id,
                    target_primary_file_id=file_one.id,
                    similarity_score=0.97,
                    merge_reason="high_similarity_match",
                    details={"moved_file_ids": [str(file_two.id)]},
                ),
                CollectionMeme(collection=favorites, meme=meme, added_by_user=owner),
                CollectionMeme(collection=shared, meme=meme, added_by_user=owner),
                PinnedMeme(user=owner, meme=meme, position=1),
                LoginEvent(
                    user=owner,
                    ip_address="203.0.113.10",
                    user_agent="Firefox on Linux",
                ),
                TelegramLinkCode(
                    guest_user_id=owner.id,
                    code_hash="1" * 64,
                    expires_at=utcnow() + timedelta(minutes=10),
                    redeemed_at=None,
                    redeemed_by_telegram_id=None,
                ),
                AnalyticsEvent(
                    user=owner,
                    event_type=AnalyticsEventType.MEME_VIEW,
                    payload={"meme_id": str(meme.id)},
                ),
                InlineUsageEvent(user=owner, group_hash="feedbeef1234"),
                ChannelSuggestion(
                    user=owner,
                    platform=SourcePlatform.TELEGRAM,
                    channel_url="https://t.me/memes_channel",
                ),
                AccountMergeLog(
                    guest_account_id=owner.id,
                    target_account_id=owner.id,
                    favorites_transferred=1,
                    views_transferred=2,
                    details={"reason": "upgrade"},
                ),
                AccountDeletionLog(
                    user_id=owner.id,
                    action=AccountDeletionAction.CANCELLED,
                    details={"restored": True},
                ),
                AdminMemeDestructiveAuditLog(
                    admin_user_id=owner.id,
                    source_meme_id=meme.id,
                    target_meme_id=None,
                    action="delete",
                    note="Admin removed unsafe test content",
                    affected_snapshot={"meme_files": {"count": 2}},
                ),
                SourceChannel(
                    platform=SourcePlatform.TELEGRAM,
                    platform_id="987654",
                    username="memes_channel",
                    title="Memes Channel",
                    subscriber_count=1500,
                    is_active=True,
                    last_read_post_id="12345",
                ),
            ]
        )
        await session.commit()

        result = await session.execute(
            select(User)
            .options(
                selectinload(User.active_save_collection),
                selectinload(User.owned_collections).selectinload(Collection.invites),
            )
            .where(User.id == owner.id)
        )
        persisted_owner = result.scalar_one()

        assert persisted_owner.active_save_collection is not None
        assert persisted_owner.active_save_collection.kind is CollectionKind.FAVORITES
        assert len(persisted_owner.owned_collections) == 2
        shared_collection = next(
            collection for collection in persisted_owner.owned_collections if collection.kind is CollectionKind.CUSTOM
        )
        assert len(shared_collection.invites) == 2

        meme_result = await session.execute(
            select(Meme)
            .options(selectinload(Meme.files).selectinload(MemeFile.pipeline_stage_journal_entries))
            .where(Meme.id == meme.id)
        )
        persisted_meme = meme_result.scalar_one()
        assert persisted_meme.primary_file_id == file_one.id
        assert len(persisted_meme.files) == 2
        assert (
            len(persisted_meme.files[0].pipeline_stage_journal_entries)
            + len(persisted_meme.files[1].pipeline_stage_journal_entries)
            == 8
        )
        assert {
            entry.stage for meme_file in persisted_meme.files for entry in meme_file.pipeline_stage_journal_entries
        } >= {
            ContentPipelineStage.INGEST,
            ContentPipelineStage.TRANSCODE,
            ContentPipelineStage.OCR,
            ContentPipelineStage.EMBED,
            ContentPipelineStage.CLASSIFY,
            ContentPipelineStage.SYNC_QDRANT,
            ContentPipelineStage.SYNC_MEILI,
        }
        assert any(
            entry.status is ContentPipelineStageStatus.DUPLICATE
            for meme_file in persisted_meme.files
            for entry in meme_file.pipeline_stage_journal_entries
        )

        ocr_result = await session.scalar(
            select(MemeFileOCRResult).where(MemeFileOCRResult.meme_file_id == file_one.id)
        )
        merge_log = await session.scalar(select(MemeMergeLog).where(MemeMergeLog.source_meme_file_id == file_two.id))

        assert ocr_result is not None
        assert ocr_result.engine == "paddleocr"
        assert ocr_result.language is ContentLanguage.EN
        assert merge_log is not None
        assert merge_log.merge_reason == "high_similarity_match"


async def test_constraints_reject_duplicate_provider_ids_and_duplicate_favorites(
    model_contract_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with model_contract_session_factory() as session:
        session.add_all(
            [
                User(google_id="google-subject-1"),
                User(google_id="google-subject-1"),
            ]
        )

        with pytest.raises(IntegrityError):
            await session.commit()

        await session.rollback()

    async with model_contract_session_factory() as session:
        session.add_all(
            [
                TelegramLinkCode(
                    guest_user_id=uuid.uuid7(),
                    code_hash="a" * 64,
                    expires_at=utcnow() + timedelta(minutes=5),
                ),
                TelegramLinkCode(
                    guest_user_id=uuid.uuid7(),
                    code_hash="a" * 64,
                    expires_at=utcnow() + timedelta(minutes=5),
                ),
            ]
        )

        with pytest.raises(IntegrityError):
            await session.commit()

        await session.rollback()

    async with model_contract_session_factory() as session:
        owner = User(email="favorites@example.com")
        session.add(owner)
        await session.flush()
        session.add_all(
            [
                Collection(owner=owner, title="Favorites", kind=CollectionKind.FAVORITES),
                Collection(owner=owner, title="More Favorites", kind=CollectionKind.FAVORITES),
            ]
        )

        with pytest.raises(IntegrityError):
            await session.commit()


async def test_constraints_reject_missing_or_cross_meme_primary_file(
    model_contract_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with model_contract_session_factory() as session:
        meme = Meme(media_type=ContentKind.IMAGE)
        session.add(meme)

        with pytest.raises(IntegrityError):
            await session.flush()

    async with model_contract_session_factory() as session:
        source_meme_id = uuid.uuid7()
        source_file_id = uuid.uuid7()
        invalid_meme_id = uuid.uuid7()
        invalid_file_id = uuid.uuid7()
        session.add_all(
            [
                Meme(id=source_meme_id, media_type=ContentKind.IMAGE, primary_file_id=source_file_id),
                MemeFile(
                    id=source_file_id,
                    meme_id=source_meme_id,
                    status=ContentProcessingStatus.READY,
                    s3_original_key="files/source/original.jpg",
                ),
                Meme(id=invalid_meme_id, media_type=ContentKind.IMAGE, primary_file_id=source_file_id),
                MemeFile(
                    id=invalid_file_id,
                    meme_id=invalid_meme_id,
                    status=ContentProcessingStatus.READY,
                    s3_original_key="files/invalid/original.jpg",
                ),
            ]
        )

        with pytest.raises(IntegrityError):
            await session.commit()


async def test_pipeline_stage_journal_enforces_one_latest_row_per_stage(
    model_contract_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with model_contract_session_factory() as session:
        meme_id = uuid.uuid7()
        meme_file_id = uuid.uuid7()
        meme = Meme(id=meme_id, media_type=ContentKind.IMAGE, primary_file_id=meme_file_id)

        meme_file = MemeFile(
            id=meme_file_id,
            meme_id=meme_id,
            status=ContentProcessingStatus.PENDING,
            s3_original_key="pipeline/originals/file/original.jpg",
        )
        session.add(meme)
        await session.flush()
        session.add(meme_file)
        await session.flush()

        session.add_all(
            [
                PipelineStageJournal(
                    meme_file=meme_file,
                    stage=ContentPipelineStage.TRANSCODE,
                    status=ContentPipelineStageStatus.PENDING,
                    attempt_count=0,
                    last_event_id=uuid.uuid7(),
                    is_retryable=True,
                ),
                PipelineStageJournal(
                    meme_file=meme_file,
                    stage=ContentPipelineStage.TRANSCODE,
                    status=ContentPipelineStageStatus.FAILED,
                    attempt_count=1,
                    last_event_id=uuid.uuid7(),
                    normalized_reason="duplicate_stage_row",
                    is_retryable=False,
                ),
            ]
        )

        with pytest.raises(IntegrityError):
            await session.commit()


def test_public_schemas_validate_from_attributes_and_reject_invalid_enums() -> None:
    now = utcnow()
    user_id = uuid.uuid7()
    collection_id = uuid.uuid7()

    user = User(
        id=user_id,
        status=AccountStatus.ACTIVE,
        email="reader@example.com",
        language=UserLanguage.RU,
        nsfw_enabled=True,
        last_active_at=now,
        token_nonce=0,
        created_at=now,
        updated_at=now,
    )
    default_user = User()
    collection = Collection(
        id=collection_id,
        owner_id=user.id,
        title="Favorites",
        kind=CollectionKind.FAVORITES,
        visibility=CollectionVisibility.PRIVATE,
        created_at=now,
        updated_at=now,
        memberships=[
            CollectionMember(
                collection_id=collection_id,
                user_id=user.id,
                role=CollectionMembershipRole.OWNER,
                joined_at=now,
            )
        ],
        invites=[
            CollectionInvite(
                id=uuid.uuid7(),
                collection_id=collection_id,
                created_by_user_id=user.id,
                token_hash="f" * 64,
                role=CollectionMembershipRole.VIEWER,
                channel=CollectionInviteChannel.DIRECT_LINK,
                status=CollectionInviteStatus.PENDING,
                use_count=0,
                created_at=now,
                updated_at=now,
            )
        ],
    )

    user_payload = UserRead.model_validate(user).model_dump(mode="json")
    default_user_payload = UserRead.model_validate(
        User(
            id=uuid.uuid7(),
            status=AccountStatus.ACTIVE,
            language=UserLanguage.ANY,
            nsfw_enabled=False,
            token_nonce=0,
            created_at=now,
            updated_at=now,
        )
    ).model_dump(mode="json")
    collection_payload = CollectionRead.model_validate(collection).model_dump(mode="json")

    assert user_payload["account_type"] == AccountType.FULL.value
    assert user_payload["is_admin"] is False
    assert default_user.is_admin is False
    assert default_user_payload["is_admin"] is False
    assert user_payload["language"] == UserLanguage.RU.value
    assert collection_payload["kind"] == CollectionKind.FAVORITES.value
    assert collection_payload["memberships"][0]["role"] == CollectionMembershipRole.OWNER.value
    assert collection_payload["invites"][0]["channel"] == CollectionInviteChannel.DIRECT_LINK.value

    with pytest.raises(ValidationError):
        _ = UserRead.model_validate(
            {
                "id": user.id,
                "account_type": "broken",
                "status": AccountStatus.ACTIVE.value,
                "telegram_id": None,
                "google_id": None,
                "email": None,
                "email_verified_at": None,
                "active_save_collection_id": None,
                "nsfw_enabled": False,
                "is_admin": False,
                "language": UserLanguage.ANY.value,
                "last_active_at": None,
                "guest_expires_at": None,
                "deletion_requested_at": None,
                "deletion_due_at": None,
                "deleted_at": None,
                "created_at": now,
                "updated_at": now,
            }
        )


def test_content_pipeline_schemas_reject_invalid_stage_names_event_pairings_and_raw_media_payloads() -> None:
    now = utcnow()
    event_id = uuid.uuid7()
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    journal_entry = PipelineStageJournal(
        id=uuid.uuid7(),
        meme_file_id=meme_file_id,
        stage=ContentPipelineStage.SYNC_MEILI,
        status=ContentPipelineStageStatus.FAILED,
        attempt_count=2,
        last_event_id=event_id,
        normalized_reason="forced_failure",
        last_error_text="stub stage failed",
        is_retryable=True,
        retry_after=now,
        started_at=now,
        finished_at=now,
        created_at=now,
        updated_at=now,
    )

    created_payload = ContentPipelineDispatchEvent.model_validate(
        {
            "event_id": event_id,
            "event_type": ContentPipelineEventType.MEME_CREATED.value,
            "meme_id": meme_id,
            "meme_file_id": meme_file_id,
            "stage": ContentPipelineStage.TRANSCODE.value,
            "source_kind": "manual_upload",
            "original_object_key": "pipeline/originals/file/original.jpeg",
            "attempt": 1,
            "created_at": now,
        }
    ).model_dump(mode="json")
    transcoded_payload = ContentPipelineDispatchEvent.model_validate(
        {
            "event_id": uuid.uuid7(),
            "event_type": ContentPipelineEventType.MEME_TRANSCODED.value,
            "meme_id": meme_id,
            "meme_file_id": meme_file_id,
            "stage": ContentPipelineStage.OCR.value,
            "source_kind": "manual_upload",
            "original_object_key": "pipeline/originals/file/original.jpeg",
            "attempt": 1,
            "created_at": now,
        }
    ).model_dump(mode="json")
    journal_payload = ContentPipelineStageJournalRead.model_validate(journal_entry).model_dump(mode="json")

    assert created_payload["stage"] == ContentPipelineStage.TRANSCODE.value
    assert created_payload["event_type"] == ContentPipelineEventType.MEME_CREATED.value
    assert transcoded_payload["stage"] == ContentPipelineStage.OCR.value
    assert transcoded_payload["event_type"] == ContentPipelineEventType.MEME_TRANSCODED.value
    assert journal_payload["status"] == ContentPipelineStageStatus.FAILED.value
    assert journal_payload["normalized_reason"] == "forced_failure"

    with pytest.raises(ValidationError):
        _ = ContentPipelineDispatchEvent.model_validate(
            {
                "event_id": event_id,
                "event_type": ContentPipelineEventType.MEME_CREATED.value,
                "meme_id": meme_id,
                "meme_file_id": meme_file_id,
                "stage": "unsupported_stage",
                "source_kind": "manual_upload",
                "original_object_key": "pipeline/originals/file/original.jpeg",
                "attempt": 1,
                "created_at": now,
            }
        )

    with pytest.raises(ValidationError):
        _ = ContentPipelineDispatchEvent.model_validate(
            {
                "event_id": event_id,
                "event_type": ContentPipelineEventType.MEME_OCR_DONE.value,
                "meme_id": meme_id,
                "meme_file_id": meme_file_id,
                "stage": ContentPipelineStage.CLASSIFY.value,
                "source_kind": "manual_upload",
                "original_object_key": "pipeline/originals/file/original.jpeg",
                "attempt": 1,
                "created_at": now,
            }
        )

    with pytest.raises(ValidationError):
        _ = ContentPipelineDispatchEvent.model_validate(
            {
                "event_id": event_id,
                "event_type": ContentPipelineEventType.MEME_CREATED.value,
                "meme_id": meme_id,
                "meme_file_id": meme_file_id,
                "stage": ContentPipelineStage.TRANSCODE.value,
                "source_kind": "manual_upload",
                "original_object_key": "pipeline/originals/file/original.jpeg",
                "attempt": 1,
                "created_at": now,
                "raw_media_bytes": "still-not-allowed",
            }
        )


def test_sync_target_enum_values_are_locked_and_stable() -> None:
    # Enum values are wire-visible: the durable snapshot rows store them verbatim
    # and the broker payload schemas rely on them. Locking the tuple order here
    # protects T02/T03/T04 against accidental reordering or renames.
    assert tuple(SyncTargetKind) == (SyncTargetKind.QDRANT, SyncTargetKind.MEILISEARCH)
    assert [member.value for member in SyncTargetKind] == ["qdrant", "meilisearch"]


def test_search_synonym_enum_values_are_locked_and_stable() -> None:
    assert [member.value for member in SearchSynonymLocale] == ["en", "ru"]
    assert [member.value for member in SearchSynonymRevisionStatus] == [
        "draft",
        "published",
        "archived",
    ]
    assert [member.value for member in SearchSynonymSyncStatus] == [
        "idle",
        "pending",
        "syncing",
        "synced",
        "failed",
    ]
    assert tuple(SyncTargetStatus) == (
        SyncTargetStatus.PENDING,
        SyncTargetStatus.PROCESSING,
        SyncTargetStatus.SYNCED,
        SyncTargetStatus.FAILED,
    )
    assert [member.value for member in SyncTargetStatus] == [
        "pending",
        "processing",
        "synced",
        "failed",
    ]


def test_moderation_enum_values_are_locked_and_stable() -> None:
    assert [member.value for member in ModerationReportStatus] == [
        "pending",
        "in_review",
        "resolved",
        "dismissed",
    ]
    assert [member.value for member in ModerationReason] == [
        "copyright",
        "harassment",
        "illegal",
        "nsfw",
        "other",
        "spam",
    ]
    assert [member.value for member in ModerationAction] == [
        "hide",
        "hide_and_mark_nsfw",
        "mark_nsfw",
        "mark_sfw",
        "no_action",
        "template_override",
        "override_flags",
        "publish",
    ]


def test_meme_qdrant_and_meili_events_validate_only_for_their_own_stage() -> None:
    from memexpert.schemas.content_pipeline import _PIPELINE_EVENT_ALLOWED_STAGES

    assert _PIPELINE_EVENT_ALLOWED_STAGES[ContentPipelineEventType.MEME_QDRANT_SYNCED] == frozenset(
        {ContentPipelineStage.SYNC_QDRANT}
    )
    assert _PIPELINE_EVENT_ALLOWED_STAGES[ContentPipelineEventType.MEME_MEILI_SYNCED] == frozenset(
        {ContentPipelineStage.SYNC_MEILI}
    )

    now = utcnow()
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()

    qdrant_payload = ContentPipelineDispatchEvent.model_validate(
        {
            "event_id": uuid.uuid7(),
            "event_type": ContentPipelineEventType.MEME_QDRANT_SYNCED.value,
            "meme_id": meme_id,
            "meme_file_id": meme_file_id,
            "stage": ContentPipelineStage.SYNC_QDRANT.value,
            "source_kind": "manual_upload",
            "original_object_key": "pipeline/originals/file/original.jpeg",
            "attempt": 1,
            "created_at": now,
        }
    )
    meili_payload = ContentPipelineDispatchEvent.model_validate(
        {
            "event_id": uuid.uuid7(),
            "event_type": ContentPipelineEventType.MEME_MEILI_SYNCED.value,
            "meme_id": meme_id,
            "meme_file_id": meme_file_id,
            "stage": ContentPipelineStage.SYNC_MEILI.value,
            "source_kind": "manual_upload",
            "original_object_key": "pipeline/originals/file/original.jpeg",
            "attempt": 1,
            "created_at": now,
        }
    )
    assert qdrant_payload.stage is ContentPipelineStage.SYNC_QDRANT
    assert meili_payload.stage is ContentPipelineStage.SYNC_MEILI

    # Each event must be rejected against any non-matching stage, including the
    # other sync stage — locking the one-event-per-stage contract before T02/T03
    # can rely on it.
    for wrong_stage in (
        ContentPipelineStage.INGEST,
        ContentPipelineStage.TRANSCODE,
        ContentPipelineStage.OCR,
        ContentPipelineStage.EMBED,
        ContentPipelineStage.CLASSIFY,
        ContentPipelineStage.SYNC_MEILI,
    ):
        with pytest.raises(ValidationError):
            _ = ContentPipelineDispatchEvent.model_validate(
                {
                    "event_id": uuid.uuid7(),
                    "event_type": ContentPipelineEventType.MEME_QDRANT_SYNCED.value,
                    "meme_id": meme_id,
                    "meme_file_id": meme_file_id,
                    "stage": wrong_stage.value,
                    "source_kind": "manual_upload",
                    "original_object_key": "pipeline/originals/file/original.jpeg",
                    "attempt": 1,
                    "created_at": now,
                }
            )

    for wrong_stage in (
        ContentPipelineStage.INGEST,
        ContentPipelineStage.TRANSCODE,
        ContentPipelineStage.OCR,
        ContentPipelineStage.EMBED,
        ContentPipelineStage.CLASSIFY,
        ContentPipelineStage.SYNC_QDRANT,
    ):
        with pytest.raises(ValidationError):
            _ = ContentPipelineDispatchEvent.model_validate(
                {
                    "event_id": uuid.uuid7(),
                    "event_type": ContentPipelineEventType.MEME_MEILI_SYNCED.value,
                    "meme_id": meme_id,
                    "meme_file_id": meme_file_id,
                    "stage": wrong_stage.value,
                    "source_kind": "manual_upload",
                    "original_object_key": "pipeline/originals/file/original.jpeg",
                    "attempt": 1,
                    "created_at": now,
                }
            )


def test_item_detail_defaults_sync_targets_to_empty_mapping_and_preserves_s02_fields() -> None:
    from memexpert.schemas.content_pipeline import (
        ContentPipelineClassificationDetail,
        ContentPipelineItemDetail,
        ContentPipelineItemRead,
        ContentPipelineMergeDetail,
    )

    now = utcnow()
    meme_id = uuid.uuid7()
    meme_file_id = uuid.uuid7()
    event_id = uuid.uuid7()

    journal_entry = PipelineStageJournal(
        id=uuid.uuid7(),
        meme_file_id=meme_file_id,
        stage=ContentPipelineStage.TRANSCODE,
        status=ContentPipelineStageStatus.SUCCEEDED,
        attempt_count=1,
        last_event_id=event_id,
        normalized_reason=None,
        last_error_text=None,
        is_retryable=False,
        retry_after=None,
        started_at=now,
        finished_at=now,
        created_at=now,
        updated_at=now,
    )
    base_read = ContentPipelineItemRead(
        meme_id=meme_id,
        meme_file_id=meme_file_id,
        current_stage=ContentPipelineStage.TRANSCODE,
        current_status=ContentPipelineStageStatus.SUCCEEDED,
        original_object_key="pipeline/originals/file/original.png",
        web_video_object_key=None,
        last_event_id=event_id,
        normalized_reason=None,
        last_error_text=None,
        attempt_count=1,
        stages=(ContentPipelineStageJournalRead.model_validate(journal_entry),),
    )

    detail_default = ContentPipelineItemDetail(
        **base_read.model_dump(mode="python"),
        ocr=None,
        merge=ContentPipelineMergeDetail(),
        classification=ContentPipelineClassificationDetail(),
        canonical=None,
        ready_event=None,
    )

    # ``sync_targets`` must default to ``{}`` so pre-S03 serializer clients stay
    # byte-compatible. The base S01 item-read payload must stay a strict subset.
    assert detail_default.sync_targets == {}
    detail_payload = detail_default.model_dump(mode="python")
    base_payload = base_read.model_dump(mode="python")
    for key, value in base_payload.items():
        assert detail_payload[key] == value
    assert detail_payload["sync_targets"] == {}


async def test_sync_target_snapshot_orm_persists_and_enforces_uniqueness(
    model_contract_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with model_contract_session_factory() as session:
        meme_id = uuid.uuid7()
        meme_file_id = uuid.uuid7()
        meme = Meme(id=meme_id, media_type=ContentKind.IMAGE, primary_file_id=meme_file_id)

        meme_file = MemeFile(
            id=meme_file_id,
            meme_id=meme_id,
            status=ContentProcessingStatus.READY,
            s3_original_key="pipeline/originals/sync/original.png",
        )
        session.add(meme)
        await session.flush()
        session.add(meme_file)
        await session.flush()

        snapshot = MemeFileSyncTargetSnapshot(
            meme_file=meme_file,
            sync_target=SyncTargetKind.QDRANT,
            status=SyncTargetStatus.SYNCED,
            last_event_id=uuid.uuid7(),
            normalized_reason=None,
            last_error_text=None,
            last_payload_preview={"point_id": str(meme_file.id)},
            last_success_at=utcnow(),
            last_attempt_at=utcnow(),
            attempt_count=1,
        )
        session.add(snapshot)
        await session.commit()

        persisted_meme_file = await session.scalar(
            select(MemeFile).options(selectinload(MemeFile.sync_target_snapshots)).where(MemeFile.id == meme_file.id)
        )
        assert persisted_meme_file is not None
        assert len(persisted_meme_file.sync_target_snapshots) == 1
        persisted_snapshot = persisted_meme_file.sync_target_snapshots[0]
        assert persisted_snapshot.sync_target is SyncTargetKind.QDRANT
        assert persisted_snapshot.status is SyncTargetStatus.SYNCED
        assert persisted_snapshot.last_payload_preview == {"point_id": str(meme_file.id)}
        assert persisted_snapshot.attempt_count == 1

    async with model_contract_session_factory() as session:
        meme_id = uuid.uuid7()
        meme_file_id = uuid.uuid7()
        meme = Meme(id=meme_id, media_type=ContentKind.IMAGE, primary_file_id=meme_file_id)

        meme_file = MemeFile(
            id=meme_file_id,
            meme_id=meme_id,
            status=ContentProcessingStatus.READY,
            s3_original_key="pipeline/originals/dup/original.png",
        )
        session.add(meme)
        await session.flush()
        session.add(meme_file)
        await session.flush()

        session.add_all(
            [
                MemeFileSyncTargetSnapshot(
                    meme_file=meme_file,
                    sync_target=SyncTargetKind.QDRANT,
                    status=SyncTargetStatus.PENDING,
                    attempt_count=0,
                    last_payload_preview={},
                ),
                MemeFileSyncTargetSnapshot(
                    meme_file=meme_file,
                    sync_target=SyncTargetKind.QDRANT,
                    status=SyncTargetStatus.FAILED,
                    attempt_count=1,
                    last_payload_preview={},
                ),
            ]
        )

        with pytest.raises(IntegrityError):
            await session.commit()


def test_telegram_session_status_values_are_locked_and_stable() -> None:
    # Enum values are wire-visible: the durable session registry row stores them
    # verbatim and the operator surface serializes them into the admin API
    # response payload. Locking the tuple order here protects T02/T03/T04
    # against accidental reordering or renames.
    assert tuple(TelegramSessionStatus) == (
        TelegramSessionStatus.ACTIVE,
        TelegramSessionStatus.AUTH_REQUIRED,
        TelegramSessionStatus.FLOOD_WAIT,
        TelegramSessionStatus.QUARANTINED,
        TelegramSessionStatus.STOPPED,
    )
    assert [member.value for member in TelegramSessionStatus] == [
        "active",
        "auth_required",
        "flood_wait",
        "quarantined",
        "stopped",
    ]


def test_source_engagement_enum_values_are_locked_and_stable() -> None:
    assert [member.value for member in SourceEngagementCaptureReason] == [
        "ingest_initial",
        "scheduled",
        "manual_refresh",
    ]
    assert [member.value for member in SourceEngagementScheduleLabel] == [
        "ingest_initial",
        "plus_1h",
        "plus_3h",
        "plus_12h",
        "plus_1d",
        "plus_3d",
        "plus_7d",
        "plus_1month",
        "monthly",
    ]
    assert [member.value for member in SourceEngagementFetchStatus] == [
        "success",
        "not_found",
        "not_accessible",
        "failed",
    ]
    assert [member.value for member in SourceEngagementCommentsState] == [
        "unknown",
        "enabled",
        "disabled",
        "not_exposed",
    ]


def test_meme_source_exposes_forward_attribution_columns_and_helper() -> None:
    # The forward-chain columns must exist on the ORM model so crawler ingest can
    # populate them; ``is_forwarded`` is the ergonomic helper that keeps the
    # reposter-detection logic out of every caller.
    columns = MemeSource.__table__.c
    assert "published_at" in columns
    assert "forwarded_from_source_id" in columns
    assert "forwarded_from_post_id" in columns
    assert "last_engagement_check_at" in columns
    assert "next_engagement_check_at" in columns
    assert "engagement_check_locked_at" in columns
    assert "engagement_check_lock_owner" in columns
    assert "engagement_check_attempt_count" in columns
    assert "last_engagement_error_code" in columns
    assert columns["engagement_check_attempt_count"].default.arg == 0

    plain = MemeSource(
        file_id=uuid.uuid7(),
        platform=SourcePlatform.TELEGRAM,
        source_id="memes_channel",
        post_id="42",
    )
    assert plain.is_forwarded is False

    reposter = MemeSource(
        file_id=uuid.uuid7(),
        platform=SourcePlatform.TELEGRAM,
        source_id="reposter_channel",
        post_id="100",
        forwarded_from_source_id="original_channel",
        forwarded_from_post_id="7",
    )
    assert reposter.is_forwarded is True


def test_source_engagement_snapshot_model_contract() -> None:
    table = cast("Table", MemeSourceEngagementSnapshot.__table__)

    assert set(table.c.keys()) == {
        "captured_at",
        "capture_reason",
        "comment_count",
        "comments_state",
        "created_at",
        "error_code",
        "fetch_status",
        "forward_count",
        "id",
        "meme_source_id",
        "raw_metrics",
        "reaction_count",
        "reactions",
        "scheduled_for",
        "schedule_label",
        "source_alive",
        "updated_at",
        "view_count",
    }
    assert table.c["meme_source_id"].foreign_keys
    assert table.c["view_count"].nullable
    assert table.c["reactions"].nullable
    assert table.c["reaction_count"].nullable
    assert not table.c["raw_metrics"].nullable

    unique_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique_constraints == {
        "uq_meme_source_engagement_snapshots_source_captured_at": ("meme_source_id", "captured_at"),
        "uq_meme_source_engagement_snapshots_source_schedule": (
            "meme_source_id",
            "scheduled_for",
            "schedule_label",
        ),
    }

    check_constraint_sql = " ".join(
        str(constraint.sqltext).lower() for constraint in table.constraints if isinstance(constraint, CheckConstraint)
    )
    assert "view_count" in check_constraint_sql
    assert "reaction_count" in check_constraint_sql
    assert "comment_count" in check_constraint_sql
    assert "forward_count" in check_constraint_sql
    assert ">= 0" in check_constraint_sql

    index_names = {index.name for index in table.indexes}
    assert "ix_meme_source_engagement_snapshots_source_captured_desc" in index_names
    assert "ix_meme_source_engagement_snapshots_label_status_captured" in index_names


def test_source_channel_exposes_crawler_checkpoint_columns() -> None:
    columns = SourceChannel.__table__.c
    assert "telegram_session_id" in columns
    assert "session_id" not in columns
    assert "catchup_message_limit" in columns
    assert "catchup_enabled" in columns
    assert "live_enabled" in columns
    assert "engagement_enabled" in columns
    assert "is_paused" in columns
    assert "last_fetched_at" in columns
    assert "oldest_observed_post_id" in columns
    assert "history_cursor_post_id" in columns
    assert "initial_catchup_completed" in columns
    assert "history_exhausted" in columns
    # Defaults live on the column descriptors because SQLAlchemy resolves
    # them at flush time, not at construction time. Reading them off the
    # column keeps this a pure unit test that does not need a session.
    assert columns["catchup_message_limit"].default.arg == 5000
    assert columns["catchup_enabled"].default.arg is True
    assert columns["live_enabled"].default.arg is True
    assert columns["engagement_enabled"].default.arg is True
    assert columns["is_paused"].default.arg is False
    assert columns["initial_catchup_completed"].default.arg is False
    assert columns["history_exhausted"].default.arg is False

    index_names = {index.name for index in cast("Table", SourceChannel.__table__).indexes}
    assert "ix_source_channels_telegram_session_id" in index_names
    assert "ix_source_channels_session_live" in index_names
    assert "ix_source_channels_session_engagement" in index_names


def test_source_channel_post_inventory_model_contract() -> None:
    table = cast("Table", SourceChannelPost.__table__)
    assert set(table.c.keys()) == {
        "attempt_count",
        "created_at",
        "id",
        "is_retryable",
        "last_attempt_at",
        "last_error_code",
        "last_error_text",
        "media_type",
        "next_attempt_at",
        "post_id",
        "published_at",
        "quarantined_at",
        "source_channel_id",
        "status",
        "updated_at",
    }
    status_default = table.c["status"].default
    attempt_count_default = table.c["attempt_count"].default
    is_retryable_default = table.c["is_retryable"].default
    assert status_default is not None
    assert attempt_count_default is not None
    assert is_retryable_default is not None
    assert cast("Any", status_default).arg is SourceChannelPostStatus.OBSERVED
    assert cast("Any", attempt_count_default).arg == 0
    assert cast("Any", is_retryable_default).arg is False
    unique_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique_constraints == {
        "uq_source_channel_posts_channel_post": ("source_channel_id", "post_id"),
    }
    assert {index.name for index in table.indexes} == {
        "ix_source_channel_posts_channel_published_at",
        "ix_source_channel_posts_channel_status",
    }


def test_source_channel_backfill_job_model_contract() -> None:
    table = cast("Table", SourceChannelBackfillJob.__table__)
    status_default = table.c["status"].default
    scanned_count_default = table.c["scanned_message_count"].default
    assert status_default is not None
    assert scanned_count_default is not None
    assert cast("Any", status_default).arg is SourceChannelBackfillJobStatus.QUEUED
    assert cast("Any", scanned_count_default).arg == 0
    check_constraint_sql = " ".join(
        str(constraint.sqltext).lower() for constraint in table.constraints if isinstance(constraint, CheckConstraint)
    )
    assert "requested_message_count >= 1" in check_constraint_sql
    assert "requested_message_count <= 50000" in check_constraint_sql
    assert "scanned_message_count >= 0" in check_constraint_sql
    assert {index.name for index in table.indexes} == {
        "ix_source_channel_backfill_jobs_channel_created_id",
        "ix_source_channel_backfill_jobs_status_locked_created",
        "uq_source_channel_backfill_jobs_one_active_per_channel",
    }
    active_index = next(
        index for index in table.indexes if index.name == "uq_source_channel_backfill_jobs_one_active_per_channel"
    )
    assert active_index.unique is True
    predicate = active_index.dialect_options["postgresql"]["where"]
    assert predicate is not None
    assert "queued" in str(predicate)
    assert "running" in str(predicate)


def test_meme_source_unique_platform_source_post_still_holds() -> None:
    # The crawler contract depends on this tuple to enforce idempotency.
    # If T02/T03 ever accidentally drops the constraint, the crawler ingest
    # would lose its duplicate-post guard, so locking this assertion keeps
    # the S04 tests honest about the contract.
    meme_source_table = cast("Table", MemeSource.__table__)
    unique_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in meme_source_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique_constraints == {
        "uq_meme_sources_platform_source_post": ("platform", "source_id", "post_id"),
    }


async def test_telegram_session_orm_persists_projects_safely_and_enforces_uniqueness(
    model_contract_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from memexpert.schemas.content_pipeline import TelegramSessionRead

    async with model_contract_session_factory() as session:
        row = TelegramSession(
            name="primary",
            display_name="Primary Session",
            encrypted_string_session="encrypted-secret-material",
            account_user_id=123456789,
            account_username="meme_admin",
            account_phone_hint="***1234",
            status=TelegramSessionStatus.ACTIVE,
            enabled=True,
            last_heartbeat_at=utcnow(),
        )
        channel = SourceChannel(
            platform=SourcePlatform.TELEGRAM,
            platform_id="session-owned-channel",
            title="Session Owned Channel",
            telegram_session=row,
        )
        session.add(row)
        session.add(channel)
        await session.commit()

        persisted = await session.scalar(
            select(TelegramSession).where(
                TelegramSession.name == "primary",
            )
        )
        assert persisted is not None
        assert persisted.status is TelegramSessionStatus.ACTIVE
        assert persisted.account_user_id == 123456789
        projection = TelegramSessionRead.model_validate(persisted)
        assert projection.name == "primary"
        assert projection.account_username == "meme_admin"
        assert "encrypted_string_session" not in projection.model_dump(mode="python")

        await session.delete(persisted)
        await session.commit()
        await session.refresh(channel)
        orphaned_channel = await session.get(SourceChannel, channel.id)
        assert orphaned_channel is not None
        assert orphaned_channel.telegram_session_id is None

    async with model_contract_session_factory() as session:
        session.add_all(
            [
                TelegramSession(
                    name="duplicate",
                    display_name="Duplicate One",
                    status=TelegramSessionStatus.STOPPED,
                ),
                TelegramSession(
                    name="duplicate",
                    display_name="Duplicate Two",
                    status=TelegramSessionStatus.ACTIVE,
                ),
            ]
        )

        with pytest.raises(IntegrityError):
            await session.commit()
