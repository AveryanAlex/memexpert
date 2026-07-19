# ruff: noqa: TC003
"""Operator backfill for durable Telegram post metadata.

The backfill changes post metadata only, apart from normal apply-mode Telegram
session health transitions for flood waits and permanent account failures. It
does not invoke the crawler runtime or ingest service, so fetching historical
post context cannot download media, move crawler checkpoints, alter ingest
status, or enqueue pipeline work. Dry-run performs no database writes.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import or_, select

from memexpert.crawlers.telegram.client import (
    PipelineTelegramClientProtocol,
    PipelineTelegramError,
    PipelineTelegramFloodWaitError,
    PipelineTelegramMalformedMessageError,
    PipelineTelegramProviderUnavailableError,
    PipelineTelegramSessionAuthRequiredError,
    PipelineTelegramSessionBannedError,
    PipelineTelegramSessionNotRunnableError,
    RawTelegramMessage,
)
from memexpert.models.base import utcnow
from memexpert.models.content import SourceChannel, SourceChannelPost, TelegramSession
from memexpert.models.enums import SourcePlatform, TelegramSessionStatus
from memexpert.schemas.telegram_post import TELEGRAM_POST_METADATA_VERSION, TelegramPostMetadata

if TYPE_CHECKING:
    from memexpert.core.config import Settings
    from memexpert.core.database import AsyncSessionFactory


type TelegramMetadataBackfillClientFactory = Callable[
    [TelegramSession],
    PipelineTelegramClientProtocol | Awaitable[PipelineTelegramClientProtocol],
]


@dataclass(frozen=True, slots=True)
class TelegramPostMetadataBackfillResult:
    """Aggregate outcome of one bounded-batch metadata backfill run."""

    dry_run: bool
    channels_inspected: int
    batches_processed: int
    candidates_inspected: int
    captured: int
    missing: int
    transient_failures: int
    permanent_failures: int
    stale_candidates: int
    unassigned_channels: int
    sessions_parked: int
    sessions_quarantined: int
    sessions_skipped: int
    sessions_requiring_attention: int

    @property
    def retry_required(self) -> bool:
        """Return whether uncaptured work should be retried later."""

        return self.transient_failures > 0 or self.sessions_parked > 0 or self.sessions_skipped > 0

    @property
    def operator_attention_required(self) -> bool:
        """Return whether deterministic failures require operator action."""

        return (
            self.permanent_failures > 0
            or self.unassigned_channels > 0
            or self.sessions_requiring_attention > 0
        )


@dataclass(slots=True)
class _MutableBackfillResult:
    channels_inspected: int = 0
    batches_processed: int = 0
    candidates_inspected: int = 0
    captured: int = 0
    missing: int = 0
    transient_failures: int = 0
    permanent_failures: int = 0
    stale_candidates: int = 0
    unassigned_channels: int = 0
    sessions_parked: int = 0
    sessions_quarantined: int = 0
    sessions_skipped: int = 0
    sessions_requiring_attention: int = 0

    def freeze(self, *, dry_run: bool) -> TelegramPostMetadataBackfillResult:
        return TelegramPostMetadataBackfillResult(
            dry_run=dry_run,
            channels_inspected=self.channels_inspected,
            batches_processed=self.batches_processed,
            candidates_inspected=self.candidates_inspected,
            captured=self.captured,
            missing=self.missing,
            transient_failures=self.transient_failures,
            permanent_failures=self.permanent_failures,
            stale_candidates=self.stale_candidates,
            unassigned_channels=self.unassigned_channels,
            sessions_parked=self.sessions_parked,
            sessions_quarantined=self.sessions_quarantined,
            sessions_skipped=self.sessions_skipped,
            sessions_requiring_attention=self.sessions_requiring_attention,
        )


@dataclass(frozen=True, slots=True)
class _ChannelTarget:
    channel_id: uuid.UUID
    platform_id: str
    telegram_session: TelegramSession | None


@dataclass(frozen=True, slots=True)
class _PostCandidate:
    row_id: uuid.UUID
    post_id: str


@dataclass(frozen=True, slots=True)
class _FetchedBatch:
    messages: dict[str, RawTelegramMessage]
    missing_post_ids: frozenset[str]
    transient_post_ids: frozenset[str]
    permanent_post_ids: frozenset[str]


class TelegramPostMetadataBackfiller:
    """Fetch missing post context through each channel's assigned session."""

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        *,
        settings: Settings,
        telegram_client_factory: TelegramMetadataBackfillClientFactory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._telegram_client_factory = telegram_client_factory

    async def run(
        self,
        *,
        dry_run: bool,
        channel_filters: Sequence[str] = (),
        batch_size: int = 100,
    ) -> TelegramPostMetadataBackfillResult:
        """Process every eligible row once, committing each applied batch.

        Rows that remain at metadata version zero because of a transient
        provider failure are intentionally passed by the keyset cursor during
        this run.  A later invocation starts from the beginning and retries
        them.  Explicitly missing Telegram messages are marked deleted and
        excluded from later candidate scans despite remaining at version zero.
        """

        if not 1 <= batch_size <= 100:
            raise ValueError("batch_size must be between 1 and 100 inclusive.")

        targets = await self._load_channel_targets(channel_filters=channel_filters)
        clients: dict[uuid.UUID, PipelineTelegramClientProtocol] = {}
        unavailable_session_ids: set[uuid.UUID] = set()
        counters = _MutableBackfillResult()
        try:
            for target in targets:
                counters.channels_inspected += 1
                session_state = target.telegram_session
                if session_state is None:
                    counters.unassigned_channels += 1
                    continue
                if session_state.id in unavailable_session_ids:
                    continue
                if self._session_is_unavailable(session_state):
                    counters.sessions_skipped += 1
                    unavailable_session_ids.add(session_state.id)
                    if self._session_unavailability_requires_attention(session_state):
                        counters.sessions_requiring_attention += 1
                    continue

                cursor: uuid.UUID | None = None
                while True:
                    batch = await self._load_candidate_batch(
                        channel_id=target.channel_id,
                        after_id=cursor,
                        batch_size=batch_size,
                    )
                    if not batch:
                        break
                    cursor = batch[-1].row_id
                    counters.batches_processed += 1
                    counters.candidates_inspected += len(batch)

                    try:
                        client = clients.get(session_state.id)
                        if client is None:
                            client = await self._create_client(
                                session_state,
                                persist_session_state=not dry_run,
                            )
                            clients[session_state.id] = client
                        raw_results = await client.fetch_messages(
                            channel_id=target.platform_id,
                            post_ids=tuple(candidate.post_id for candidate in batch),
                        )
                    except PipelineTelegramFloodWaitError as exc:
                        counters.transient_failures += len(batch)
                        counters.sessions_parked += 1
                        unavailable_session_ids.add(session_state.id)
                        if not dry_run:
                            await self._park_session(session_id=session_state.id, exc=exc)
                        break
                    except PipelineTelegramProviderUnavailableError:
                        counters.transient_failures += len(batch)
                        continue
                    except PipelineTelegramSessionBannedError as exc:
                        counters.permanent_failures += len(batch)
                        counters.sessions_quarantined += 1
                        counters.sessions_requiring_attention += 1
                        unavailable_session_ids.add(session_state.id)
                        if not dry_run:
                            await self._quarantine_session(session_id=session_state.id, exc=exc)
                        break
                    except PipelineTelegramSessionAuthRequiredError as exc:
                        counters.permanent_failures += len(batch)
                        counters.sessions_requiring_attention += 1
                        unavailable_session_ids.add(session_state.id)
                        if not dry_run:
                            await self._mark_session_auth_required(session_id=session_state.id, exc=exc)
                        break
                    except PipelineTelegramSessionNotRunnableError as exc:
                        counters.permanent_failures += len(batch)
                        counters.sessions_quarantined += 1
                        counters.sessions_requiring_attention += 1
                        unavailable_session_ids.add(session_state.id)
                        if not dry_run:
                            await self._quarantine_session(session_id=session_state.id, exc=exc)
                        break
                    except PipelineTelegramMalformedMessageError:
                        counters.permanent_failures += len(batch)
                        continue
                    except PipelineTelegramError:
                        counters.permanent_failures += len(batch)
                        continue

                    fetched = _normalize_fetched_batch(
                        channel_id=target.platform_id,
                        candidates=batch,
                        raw_results=raw_results,
                    )
                    if dry_run:
                        counters.captured += len(fetched.messages)
                        counters.missing += len(fetched.missing_post_ids)
                        counters.transient_failures += len(fetched.transient_post_ids)
                        counters.permanent_failures += len(fetched.permanent_post_ids)
                        continue

                    applied_captured, applied_missing, stale = await self._apply_batch(
                        candidates=batch,
                        fetched=fetched,
                    )
                    counters.captured += applied_captured
                    counters.missing += applied_missing
                    counters.transient_failures += len(fetched.transient_post_ids)
                    counters.permanent_failures += len(fetched.permanent_post_ids)
                    counters.stale_candidates += stale

                if session_state.id in unavailable_session_ids:
                    continue
        finally:
            for client in clients.values():
                with suppress(Exception):
                    await client.close()

        return counters.freeze(dry_run=dry_run)

    async def _create_client(
        self,
        session_state: TelegramSession,
        *,
        persist_session_state: bool,
    ) -> PipelineTelegramClientProtocol:
        if self._telegram_client_factory is not None:
            return await _maybe_await(self._telegram_client_factory(session_state))
        return self._build_pipeline_client(
            session_state,
            persist_session_state=persist_session_state,
        )

    def _build_pipeline_client(
        self,
        session_state: TelegramSession,
        *,
        persist_session_state: bool,
    ) -> PipelineTelegramClientProtocol:
        """Create the real adapter, whose batched fetch owns rate limiting."""

        from memexpert.crawlers.telegram.telethon_adapter import PipelineTelethonClient

        return PipelineTelethonClient.create(
            settings=self._settings,
            session_name=session_state.name,
            persist_session_state=persist_session_state,
        )

    async def _load_channel_targets(self, *, channel_filters: Sequence[str]) -> tuple[_ChannelTarget, ...]:
        normalized_filters = tuple(
            normalized
            for value in channel_filters
            if (normalized := value.strip().removeprefix("@"))
        )
        statement = (
            select(SourceChannel, TelegramSession)
            .outerjoin(TelegramSession, TelegramSession.id == SourceChannel.telegram_session_id)
            .join(SourceChannelPost, SourceChannelPost.source_channel_id == SourceChannel.id)
            .where(
                SourceChannel.platform == SourcePlatform.TELEGRAM,
                SourceChannelPost.metadata_version < TELEGRAM_POST_METADATA_VERSION,
                SourceChannelPost.is_deleted.is_(False),
            )
        )
        if normalized_filters:
            channel_uuids = tuple(
                parsed
                for value in normalized_filters
                if (parsed := _try_parse_uuid(value)) is not None
            )
            filter_predicates = [
                SourceChannel.platform_id.in_(normalized_filters),
                SourceChannel.username.in_(normalized_filters),
            ]
            if channel_uuids:
                filter_predicates.append(SourceChannel.id.in_(channel_uuids))
            statement = statement.where(or_(*filter_predicates))

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    statement.distinct().order_by(TelegramSession.name.asc(), SourceChannel.id.asc()),
                )
            ).all()
        return tuple(
            _ChannelTarget(
                channel_id=channel.id,
                platform_id=channel.platform_id,
                telegram_session=telegram_session,
            )
            for channel, telegram_session in rows
        )

    async def _load_candidate_batch(
        self,
        *,
        channel_id: uuid.UUID,
        after_id: uuid.UUID | None,
        batch_size: int,
    ) -> tuple[_PostCandidate, ...]:
        statement = select(SourceChannelPost.id, SourceChannelPost.post_id).where(
            SourceChannelPost.source_channel_id == channel_id,
            SourceChannelPost.metadata_version < TELEGRAM_POST_METADATA_VERSION,
            SourceChannelPost.is_deleted.is_(False),
        )
        if after_id is not None:
            statement = statement.where(SourceChannelPost.id > after_id)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    statement.order_by(SourceChannelPost.id.asc()).limit(batch_size),
                )
            ).all()
        return tuple(_PostCandidate(row_id=row_id, post_id=post_id) for row_id, post_id in rows)

    async def _apply_batch(
        self,
        *,
        candidates: tuple[_PostCandidate, ...],
        fetched: _FetchedBatch,
    ) -> tuple[int, int, int]:
        """Apply fetched/missing outcomes and commit exactly this batch."""

        observed_at = utcnow()
        captured = 0
        missing = 0
        stale = 0
        candidate_by_id = {candidate.row_id: candidate for candidate in candidates}
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(SourceChannelPost)
                        .where(SourceChannelPost.id.in_(candidate_by_id))
                        .order_by(SourceChannelPost.id.asc())
                        .with_for_update(),
                    )
                )
                .scalars()
                .all(),
            )
            loaded_ids = {row.id for row in rows}
            stale += len(candidate_by_id.keys() - loaded_ids)
            for row in rows:
                if row.metadata_version >= TELEGRAM_POST_METADATA_VERSION:
                    stale += 1
                    continue
                candidate = candidate_by_id[row.id]
                message = fetched.messages.get(candidate.post_id)
                if message is not None:
                    _capture_metadata(row, metadata=message.telegram_post, observed_at=observed_at)
                    captured += 1
                elif candidate.post_id in fetched.missing_post_ids:
                    row.is_deleted = True
                    row.deletion_observed_at = observed_at
                    missing += 1
            await session.commit()
        return captured, missing, stale

    async def _park_session(
        self,
        *,
        session_id: uuid.UUID,
        exc: PipelineTelegramFloodWaitError,
    ) -> None:
        observed_at = utcnow()
        flood_wait_until = observed_at + timedelta(seconds=max(exc.wait_seconds, 0))
        async with self._session_factory() as session:
            row = await session.scalar(
                select(TelegramSession)
                .where(TelegramSession.id == session_id)
                .with_for_update(of=TelegramSession),
            )
            if row is not None:
                row.status = TelegramSessionStatus.FLOOD_WAIT
                if row.flood_wait_until is None or _as_utc(row.flood_wait_until) < flood_wait_until:
                    row.flood_wait_until = flood_wait_until
                row.last_error_class = type(exc).__name__[:128]
                row.last_error_text = str(exc)[:4000]
            await session.commit()

    async def _quarantine_session(
        self,
        *,
        session_id: uuid.UUID,
        exc: PipelineTelegramError,
    ) -> None:
        observed_at = utcnow()
        async with self._session_factory() as session:
            row = await session.scalar(
                select(TelegramSession)
                .where(TelegramSession.id == session_id)
                .with_for_update(of=TelegramSession),
            )
            if row is not None:
                row.status = TelegramSessionStatus.QUARANTINED
                row.quarantined_at = observed_at
                row.last_error_class = type(exc).__name__[:128]
                row.last_error_text = str(exc)[:4000]
            await session.commit()

    async def _mark_session_auth_required(
        self,
        *,
        session_id: uuid.UUID,
        exc: PipelineTelegramSessionAuthRequiredError,
    ) -> None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(TelegramSession)
                .where(TelegramSession.id == session_id)
                .with_for_update(of=TelegramSession),
            )
            if row is not None:
                row.status = TelegramSessionStatus.AUTH_REQUIRED
                row.live_listener_started_at = None
                row.last_error_class = type(exc).__name__[:128]
                row.last_error_text = str(exc)[:4000]
            await session.commit()

    @staticmethod
    def _session_is_unavailable(session_state: TelegramSession) -> bool:
        if not session_state.enabled or session_state.status is not TelegramSessionStatus.ACTIVE:
            return True
        if not (session_state.encrypted_string_session or "").strip():
            return True
        return bool(
            session_state.flood_wait_until is not None
            and _as_utc(session_state.flood_wait_until) > utcnow()
        )

    @staticmethod
    def _session_unavailability_requires_attention(session_state: TelegramSession) -> bool:
        if not session_state.enabled:
            return True
        if session_state.status is TelegramSessionStatus.FLOOD_WAIT:
            return (
                session_state.flood_wait_until is None
                or _as_utc(session_state.flood_wait_until) <= utcnow()
            )
        if session_state.status is not TelegramSessionStatus.ACTIVE:
            return True
        return not (session_state.encrypted_string_session or "").strip()


def _normalize_fetched_batch(
    *,
    channel_id: str,
    candidates: tuple[_PostCandidate, ...],
    raw_results: dict[str, RawTelegramMessage | None],
) -> _FetchedBatch:
    messages: dict[str, RawTelegramMessage] = {}
    missing: set[str] = set()
    transient: set[str] = set()
    permanent: set[str] = set()
    for candidate in candidates:
        if candidate.post_id not in raw_results:
            transient.add(candidate.post_id)
            continue
        message = raw_results[candidate.post_id]
        if message is None:
            missing.add(candidate.post_id)
            continue
        if message.message_id != candidate.post_id or message.channel_id != channel_id:
            permanent.add(candidate.post_id)
            continue
        messages[candidate.post_id] = message
    return _FetchedBatch(
        messages=messages,
        missing_post_ids=frozenset(missing),
        transient_post_ids=frozenset(transient),
        permanent_post_ids=frozenset(permanent),
    )


def _capture_metadata(
    row: SourceChannelPost,
    *,
    metadata: TelegramPostMetadata,
    observed_at: datetime,
) -> None:
    """Populate first/latest values for a previously uncaptured row."""

    entities = metadata.entity_json()
    row.first_observed_text = metadata.text
    row.latest_text = metadata.text
    row.first_observed_text_entities = entities
    row.latest_text_entities = [dict(entity) for entity in entities]
    row.media_group_id = metadata.media_group_id
    row.reply_to_post_id = metadata.reply_to_post_id
    row.telegram_edited_at = metadata.edited_at
    row.metadata_first_observed_at = observed_at
    row.metadata_last_observed_at = observed_at
    row.metadata_version = metadata.schema_version
    row.is_deleted = False
    row.deletion_observed_at = None


def _try_parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _maybe_await(
    value: PipelineTelegramClientProtocol | Awaitable[PipelineTelegramClientProtocol],
) -> PipelineTelegramClientProtocol:
    if inspect.isawaitable(value):
        return cast("PipelineTelegramClientProtocol", await value)
    return value


__all__ = [
    "TelegramMetadataBackfillClientFactory",
    "TelegramPostMetadataBackfillResult",
    "TelegramPostMetadataBackfiller",
]
