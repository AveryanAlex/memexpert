# ruff: noqa: TC003
"""Dual-target search-sync smoke proof used by the S03 operator surface.

This module lives alongside :mod:`content_pipeline_reporting` because the
reporting file already carries detail enrichment, outcome classification, run
summaries, and Markdown rendering — adding the smoke-proof runner on top
would push it well past the soft-cap size. The smoke proof consults:

1. the canonical :class:`Meme`,
2. the primary :class:`MemeFile`,
3. the durable :class:`EmbeddingCache` row owning the stored vector,
4. the OCR-derived snippet used as a fallback query,
5. the Qdrant similarity client (so the stored vector can round-trip),
6. the Meilisearch sync adapter (so the text index can round-trip),

and assembles a :class:`SmokeProofResult` the route handler and the
``verify_s03_runtime`` harness both return verbatim. Per-target failure
branches never short-circuit the other target — operators must always see
both per-target reasons together.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from memexpert.core.meilisearch import (
    MeilisearchSyncError,
    MeilisearchSyncMalformedResponseError,
    MeilisearchSyncTimeoutError,
)
from memexpert.core.qdrant import (
    QdrantMalformedResponseError,
    QdrantSimilarityError,
    QdrantTimeoutError,
)
from memexpert.models.base import utcnow
from memexpert.models.content import EmbeddingCache, Meme, MemeFile
from memexpert.models.enums import EmbeddingInputType, SyncTargetKind
from memexpert.schemas.content_pipeline import (
    SmokeProofResult,
    SmokeProofTargetResult,
)
from memexpert.services.errors import PipelineIngestError, PipelineItemNotFoundError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.meilisearch import MeilisearchSyncClientProtocol
    from memexpert.core.qdrant import QdrantSimilarityClientProtocol, QdrantSyncClientProtocol

# Smoke-proof reason constants. These are DATA, not exceptions — operators
# filter reports by these strings so they must stay stable across releases.
SMOKE_REASON_POINT_NOT_FOUND = "point_not_found"
SMOKE_REASON_DOCUMENT_NOT_FOUND = "document_not_found"
SMOKE_REASON_PROVIDER_BLOCKED = "provider_blocked"
SMOKE_REASON_TIMEOUT = "timeout"
SMOKE_REASON_MALFORMED_RESPONSE = "malformed_response"
SMOKE_REASON_QUERY_MISS = "query_miss"
SMOKE_REASON_EMBEDDING_MISSING = "embedding_missing"

# Top-K cap used by the smoke proof. Small enough that a stale point will not
# accidentally surface in a rare top-20 result but large enough to tolerate
# near-duplicate embeddings the auto-merge layer has not yet collapsed.
SMOKE_TOP_K = 20


async def run_smoke_proof(
    session: AsyncSession,
    *,
    qdrant_sync_client: QdrantSyncClientProtocol,
    qdrant_similarity_client: QdrantSimilarityClientProtocol,
    meilisearch_sync_client: MeilisearchSyncClientProtocol,
    meme_file_id: uuid.UUID,
    query: str | None,
) -> SmokeProofResult:
    """Prove one pipeline item is truly searchable across BOTH sync targets.

    Per-target failures are reported as ``reason`` strings on the matching
    :class:`SmokeProofTargetResult` — the function never raises when the
    underlying provider is transiently unavailable or the target document is
    simply missing. Only genuine programmer errors (missing durable state,
    unreachable adapters in a way that implies a bug, etc.) propagate so the
    caller surfaces a 5xx instead of a misleading "proof=false" response.
    """

    context = await _load_smoke_context(session, meme_file_id=meme_file_id)
    resolved_query = _resolve_query(query=query, context=context)

    qdrant_result = await _prove_qdrant_target(
        qdrant_sync_client=qdrant_sync_client,
        qdrant_similarity_client=qdrant_similarity_client,
        meme_file_id=meme_file_id,
        vector=context.vector,
    )
    meili_result = await _prove_meilisearch_target(
        meilisearch_sync_client=meilisearch_sync_client,
        meme_file_id=meme_file_id,
        query=resolved_query,
    )

    both_searchable = qdrant_result.searchable and meili_result.searchable
    return SmokeProofResult(
        meme_file_id=meme_file_id,
        query=query,
        both_targets_searchable=both_searchable,
        targets=(qdrant_result, meili_result),
        evaluated_at=utcnow(),
    )


class _SmokeContext:
    """In-memory bundle of the durable state the smoke proof consults.

    A plain class rather than a dataclass because the vector is a
    ``tuple[float, ...]`` that must stay immutable and the smoke runner is
    the only call site; a dataclass would add boilerplate without benefit.
    """

    __slots__ = ("canonical_meme", "meme_file", "ocr_snippet", "vector")

    def __init__(
        self,
        *,
        canonical_meme: Meme,
        meme_file: MemeFile,
        vector: tuple[float, ...],
        ocr_snippet: str | None,
    ) -> None:
        self.canonical_meme = canonical_meme
        self.meme_file = meme_file
        self.vector = vector
        self.ocr_snippet = ocr_snippet


async def _load_smoke_context(
    session: AsyncSession,
    *,
    meme_file_id: uuid.UUID,
) -> _SmokeContext:
    """Load canonical meme + meme_file + stored embedding + OCR snippet for the proof."""

    meme_file = await session.scalar(select(MemeFile).where(MemeFile.id == meme_file_id))
    if meme_file is None:
        raise PipelineItemNotFoundError(f"Pipeline item {meme_file_id} does not exist.")

    canonical_meme = await session.scalar(select(Meme).where(Meme.id == meme_file.meme_id))
    if canonical_meme is None:
        raise PipelineIngestError(
            f"Canonical meme {meme_file.meme_id} is missing when running the smoke proof "
            f"for pipeline item {meme_file_id}.",
        )

    cache_row = await session.scalar(
        select(EmbeddingCache)
        .where(
            EmbeddingCache.source_file_id == meme_file_id,
            EmbeddingCache.input_type == EmbeddingInputType.IMAGE,
        )
        .order_by(EmbeddingCache.created_at.desc())
        .limit(1),
    )
    if cache_row is None:
        raise PipelineIngestError(
            f"Pipeline item {meme_file_id} has no durable embedding cache row; "
            "the smoke proof cannot round-trip Qdrant without the stored vector.",
        )

    from memexpert.core.config import get_settings
    from memexpert.core.voyage import decode_embedding_bytes

    # The stored bytes are Voyage output; their dimension count is fixed by
    # the configured Voyage output_dimensions setting so the same invariant
    # the sync_qdrant consumer enforces also applies here.
    vector = decode_embedding_bytes(
        cache_row.embedding,
        dimensions=get_settings().pipeline_voyage_output_dimensions,
    )

    # The canonical OCR text is the authoritative snippet — it is the same
    # field the sync workers advertised to Meilisearch, so the smoke proof
    # uses it as the fallback query when the operator did not pass one.
    ocr_snippet = canonical_meme.ocr_text

    return _SmokeContext(
        canonical_meme=canonical_meme,
        meme_file=meme_file,
        vector=vector,
        ocr_snippet=ocr_snippet,
    )


def _resolve_query(*, query: str | None, context: _SmokeContext) -> str:
    """Return the Meilisearch text query the smoke proof should run.

    Priority: explicit operator query → canonical OCR snippet → the bare
    meme file id as a last resort. Using the id is a weak fallback but it
    keeps the proof runnable for items whose OCR produced no text, which
    matches how the pipeline classifies them as ``language=none``.
    """

    if query is not None:
        stripped_query = query.strip()
        if stripped_query:
            return stripped_query
    if context.ocr_snippet is not None:
        stripped_snippet = context.ocr_snippet.strip()
        if stripped_snippet:
            return stripped_snippet
    return str(context.meme_file.id)


async def _prove_qdrant_target(
    *,
    qdrant_sync_client: QdrantSyncClientProtocol,
    qdrant_similarity_client: QdrantSimilarityClientProtocol,
    meme_file_id: uuid.UUID,
    vector: tuple[float, ...],
) -> SmokeProofTargetResult:
    """Assemble the Qdrant half of the dual-target smoke proof.

    The id-lookup is the authoritative "point exists" signal. The
    query-by-vector re-query exercises the same path the runtime uses during
    ingest and catches subtle broken states where the point exists but
    scoring has drifted so far that the index is effectively unusable.
    """

    started = time.perf_counter()
    try:
        preview = await qdrant_sync_client.fetch_meme_point(meme_file_id)
    except Exception as exc:
        reason = _classify_qdrant_error(exc)
        if reason is None:
            raise
        latency_ms = (time.perf_counter() - started) * 1000.0
        return SmokeProofTargetResult(
            target=SyncTargetKind.QDRANT,
            searchable=False,
            reason=reason,
            latency_ms=latency_ms,
            matched_by=None,
        )

    if preview is None:
        latency_ms = (time.perf_counter() - started) * 1000.0
        return SmokeProofTargetResult(
            target=SyncTargetKind.QDRANT,
            searchable=False,
            reason=SMOKE_REASON_POINT_NOT_FOUND,
            latency_ms=latency_ms,
            matched_by=None,
        )

    id_lookup_passed = True
    query_match_passed = False
    try:
        # Pass a fresh UUID as ``current_meme_file_id`` so the existing
        # self-match filter does NOT drop our target. The worker runtime
        # uses that filter to avoid merging a file with itself, which is
        # the opposite of what the smoke proof wants: it wants to see the
        # target surface in the top-K matches.
        sentinel_current = uuid.uuid7()
        matches = await qdrant_similarity_client.find_similar_memes(
            vector=vector,
            current_meme_file_id=sentinel_current,
            limit=SMOKE_TOP_K,
        )
    except Exception as exc:
        reason = _classify_qdrant_error(exc)
        if reason is None:
            raise
        latency_ms = (time.perf_counter() - started) * 1000.0
        # Id-lookup passed but the re-query failed — operators still see
        # both facts via the reason string plus ``matched_by="id_lookup"``
        # later in the report. For now we mark the target as NOT searchable
        # because the authoritative proof requires BOTH steps.
        return SmokeProofTargetResult(
            target=SyncTargetKind.QDRANT,
            searchable=False,
            reason=reason,
            latency_ms=latency_ms,
            matched_by="id_lookup",
        )

    matched_ids = {match.meme_file_id for match in matches}
    query_match_passed = meme_file_id in matched_ids
    latency_ms = (time.perf_counter() - started) * 1000.0

    matched_by: Literal["id_lookup", "query_match", "both"] | None = None
    if id_lookup_passed and query_match_passed:
        matched_by = "both"
    elif query_match_passed:
        matched_by = "query_match"
    elif id_lookup_passed:
        matched_by = "id_lookup"

    searchable = id_lookup_passed and query_match_passed
    reason = None if searchable else SMOKE_REASON_QUERY_MISS
    return SmokeProofTargetResult(
        target=SyncTargetKind.QDRANT,
        searchable=searchable,
        reason=reason,
        latency_ms=latency_ms,
        matched_by=matched_by,
    )


async def _prove_meilisearch_target(
    *,
    meilisearch_sync_client: MeilisearchSyncClientProtocol,
    meme_file_id: uuid.UUID,
    query: str,
) -> SmokeProofTargetResult:
    """Assemble the Meilisearch half of the dual-target smoke proof.

    The contract mirrors :func:`_prove_qdrant_target`: the id-lookup is the
    authoritative "document exists" signal and the text search re-query
    catches index-drift cases where the document is indexed but the query
    analyser has effectively hidden it. Both paths have to pass before the
    per-target proof reports ``searchable=True``.
    """

    started = time.perf_counter()
    try:
        preview = await meilisearch_sync_client.fetch_document(meme_file_id)
    except Exception as exc:
        reason = _classify_meili_error(exc)
        if reason is None:
            raise
        latency_ms = (time.perf_counter() - started) * 1000.0
        return SmokeProofTargetResult(
            target=SyncTargetKind.MEILISEARCH,
            searchable=False,
            reason=reason,
            latency_ms=latency_ms,
            matched_by=None,
        )

    if preview is None:
        latency_ms = (time.perf_counter() - started) * 1000.0
        return SmokeProofTargetResult(
            target=SyncTargetKind.MEILISEARCH,
            searchable=False,
            reason=SMOKE_REASON_DOCUMENT_NOT_FOUND,
            latency_ms=latency_ms,
            matched_by=None,
        )

    id_lookup_passed = True
    try:
        hits = await meilisearch_sync_client.search(query, limit=SMOKE_TOP_K)
    except Exception as exc:
        reason = _classify_meili_error(exc)
        if reason is None:
            raise
        latency_ms = (time.perf_counter() - started) * 1000.0
        return SmokeProofTargetResult(
            target=SyncTargetKind.MEILISEARCH,
            searchable=False,
            reason=reason,
            latency_ms=latency_ms,
            matched_by="id_lookup",
        )

    query_match_passed = _hits_contain_meme_file(hits=hits, meme_file_id=meme_file_id)
    latency_ms = (time.perf_counter() - started) * 1000.0

    matched_by: Literal["id_lookup", "query_match", "both"] | None = None
    if id_lookup_passed and query_match_passed:
        matched_by = "both"
    elif query_match_passed:
        matched_by = "query_match"
    elif id_lookup_passed:
        matched_by = "id_lookup"

    searchable = id_lookup_passed and query_match_passed
    reason = None if searchable else SMOKE_REASON_QUERY_MISS
    return SmokeProofTargetResult(
        target=SyncTargetKind.MEILISEARCH,
        searchable=searchable,
        reason=reason,
        latency_ms=latency_ms,
        matched_by=matched_by,
    )


def _hits_contain_meme_file(
    *,
    hits: Sequence[dict[str, Any]],
    meme_file_id: uuid.UUID,
) -> bool:
    """Return ``True`` when the Meilisearch hits list carries the target id.

    Meilisearch documents index the hex form of the meme file id as their
    primary key (see ``PipelineMeilisearchDocument.id``), so the hit's
    ``id`` field is what the smoke proof compares against.
    """

    target_hex = meme_file_id.hex
    target_str = str(meme_file_id)
    for hit in hits:
        raw_id = hit.get("id")
        if not isinstance(raw_id, str):
            continue
        if raw_id in {target_hex, target_str}:
            return True
    return False


def _classify_qdrant_error(exc: BaseException) -> str | None:
    """Map a Qdrant adapter exception onto one of the smoke-proof reason strings.

    Returns ``None`` when the exception is not part of the recognized
    taxonomy — the caller re-raises so genuine bugs surface as 5xx rather
    than false-negative smoke proofs. Timeout detection covers both the
    typed SDK error and ``asyncio.TimeoutError`` so callers that wrap the
    adapter in ``asyncio.wait_for`` still see the translated reason.
    """

    if isinstance(exc, (QdrantTimeoutError, asyncio.TimeoutError, TimeoutError)):
        return SMOKE_REASON_TIMEOUT
    if isinstance(exc, QdrantMalformedResponseError):
        return SMOKE_REASON_MALFORMED_RESPONSE
    if isinstance(exc, QdrantSimilarityError):
        return SMOKE_REASON_PROVIDER_BLOCKED
    # ``QdrantSyncError`` is the sync-side taxonomy; we reuse it verbatim
    # here so fetch_meme_point failures classify consistently with the
    # runtime's sync path.
    from memexpert.core.qdrant import (
        QdrantSyncError,
        QdrantSyncMalformedResponseError,
        QdrantSyncTimeoutError,
    )

    if isinstance(exc, QdrantSyncTimeoutError):
        return SMOKE_REASON_TIMEOUT
    if isinstance(exc, QdrantSyncMalformedResponseError):
        return SMOKE_REASON_MALFORMED_RESPONSE
    if isinstance(exc, QdrantSyncError):
        return SMOKE_REASON_PROVIDER_BLOCKED
    return None


def _classify_meili_error(exc: BaseException) -> str | None:
    """Map a Meilisearch adapter exception onto one of the smoke-proof reason strings."""

    if isinstance(exc, (MeilisearchSyncTimeoutError, asyncio.TimeoutError, TimeoutError)):
        return SMOKE_REASON_TIMEOUT
    if isinstance(exc, MeilisearchSyncMalformedResponseError):
        return SMOKE_REASON_MALFORMED_RESPONSE
    if isinstance(exc, MeilisearchSyncError):
        return SMOKE_REASON_PROVIDER_BLOCKED
    return None


def render_s03_markdown_report(
    summary: object,
) -> str:
    """Render a human-readable Markdown companion to the S03 JSON report.

    Importing :class:`ContentPipelineS03RunSummary` lazily keeps this helper
    reusable from scripts that load the schema after reading the JSON
    artifact back from disk; the concrete type is accepted as ``object`` at
    the call boundary because the schema module re-exports it and the
    dependency direction matters for the reporting / smoke module split.
    """

    from memexpert.schemas.content_pipeline import (
        ContentPipelineS03RunSummary,
    )

    if not isinstance(summary, ContentPipelineS03RunSummary):
        raise TypeError(
            "render_s03_markdown_report requires a ContentPipelineS03RunSummary instance.",
        )

    lines: list[str] = []
    lines.append(f"# Content pipeline S03 search-sync run {summary.run_id}")
    lines.append("")
    lines.append(f"- Started: {summary.started_at.isoformat()}")
    lines.append(f"- Finished: {summary.finished_at.isoformat()}")
    lines.append(f"- Bounded item count: {summary.bounded_item_count}")
    lines.append("")
    lines.append("## Sync counts")
    lines.append("")
    lines.append(f"- qdrant_synced: {summary.qdrant_synced_count}")
    lines.append(f"- meilisearch_synced: {summary.meilisearch_synced_count}")
    lines.append(f"- both_synced: {summary.both_synced_count}")
    lines.append(f"- partially_searchable: {summary.partial_count}")
    lines.append(f"- blocked_by_qdrant: {summary.blocked_by_qdrant_count}")
    lines.append(f"- blocked_by_meili: {summary.blocked_by_meili_count}")
    lines.append(f"- smoke_pass: {summary.smoke_pass_count}")
    lines.append("")
    lines.append("## Stale snapshot ids")
    lines.append("")
    if summary.stale_snapshot_ids:
        for stale_id in summary.stale_snapshot_ids:
            lines.append(f"- `{stale_id}`")
    else:
        lines.append("_(none)_")
    lines.append("")
    lines.append("## Per-item detail")
    lines.append("")
    lines.append(
        "| meme_file_id | outcome | qdrant | meili | smoke | replay |",
    )
    lines.append(
        "|--------------|---------|--------|-------|-------|--------|",
    )
    for report in summary.item_reports:
        qdrant_label = report.qdrant_status.value if report.qdrant_status is not None else "—"
        meili_label = report.meili_status.value if report.meili_status is not None else "—"
        smoke_label = _format_smoke_label(report.smoke_result)
        replay_link = _render_replay_drilldown(report)
        lines.append(
            f"| `{report.meme_file_id}` | {report.outcome} | {qdrant_label} | "
            f"{meili_label} | {smoke_label} | {replay_link} |",
        )
    lines.append("")
    lines.append("## Blocked items (per target)")
    lines.append("")
    blocked_any = False
    for report in summary.item_reports:
        if report.smoke_result is None:
            continue
        for target_result in report.smoke_result.targets:
            if target_result.searchable:
                continue
            blocked_any = True
            replay_link = _render_replay_drilldown_for_target(
                meme_file_id=report.meme_file_id,
                target=target_result.target,
            )
            lines.append(
                f"- `{report.meme_file_id}` {target_result.target.value}: "
                f"{target_result.reason or 'unknown'} — {replay_link}",
            )
    if not blocked_any:
        lines.append("_(none)_")
    lines.append("")
    if summary.errors:
        lines.append("## Errors")
        lines.append("")
        for error_line in summary.errors:
            lines.append(f"- {error_line}")
        lines.append("")
    return "\n".join(lines)


def _format_smoke_label(smoke_result: SmokeProofResult | None) -> str:
    if smoke_result is None:
        return "—"
    return "pass" if smoke_result.both_targets_searchable else "fail"


def _render_replay_drilldown(
    report: object,
) -> str:
    """Render the per-item replay drill-down link shown in the Markdown table."""

    from memexpert.schemas.content_pipeline import ContentPipelineS03RunItemReport

    if not isinstance(report, ContentPipelineS03RunItemReport):
        return "—"
    if report.smoke_result is None or report.smoke_result.both_targets_searchable:
        return "—"
    links: list[str] = []
    for target_result in report.smoke_result.targets:
        if target_result.searchable:
            continue
        links.append(
            _render_replay_drilldown_for_target(
                meme_file_id=report.meme_file_id,
                target=target_result.target,
            ),
        )
    return ", ".join(links) if links else "—"


def _render_replay_drilldown_for_target(
    *,
    meme_file_id: uuid.UUID,
    target: SyncTargetKind,
) -> str:
    """Return the operator replay path for one ``(meme_file_id, target)`` pair."""

    if target is SyncTargetKind.QDRANT:
        return f"[replay qdrant](/api/v1/pipeline/items/{meme_file_id}/sync/qdrant/replay)"
    if target is SyncTargetKind.MEILISEARCH:
        return f"[replay meili](/api/v1/pipeline/items/{meme_file_id}/sync/meili/replay)"
    return "—"


__all__ = [
    "SMOKE_REASON_DOCUMENT_NOT_FOUND",
    "SMOKE_REASON_EMBEDDING_MISSING",
    "SMOKE_REASON_MALFORMED_RESPONSE",
    "SMOKE_REASON_POINT_NOT_FOUND",
    "SMOKE_REASON_PROVIDER_BLOCKED",
    "SMOKE_REASON_QUERY_MISS",
    "SMOKE_REASON_TIMEOUT",
    "SMOKE_TOP_K",
    "render_s03_markdown_report",
    "run_smoke_proof",
]
