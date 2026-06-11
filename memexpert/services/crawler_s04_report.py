# ruff: noqa: TC003
"""Pure aggregation + rendering helpers for the T04 freshness SLO proof harness.

This module is deliberately split out of
:mod:`memexpert.services.crawler_freshness` so the latter stays focused on
the DB-driven snapshot aggregation the operator ``/api/v1/crawler/freshness``
route consumes. The T04 helpers here are pure Python — they accept an
already-built :class:`CrawlerFreshnessSnapshot` plus run metadata and
produce a :class:`CrawlerS04RunSummary` plus a human-readable Markdown
report. Tests and the ``scripts/verify_s04_runtime.py`` harness share the
exact same code path so dry-run and live mode agree on the verdict.

Design notes:

* The "fixture channel list" passed to :func:`summarize_s04_run` is the
  set of channels the harness expected to observe. Any channel present
  in the fixture but missing from the snapshot lands in
  :attr:`CrawlerS04RunSummary.stalled_channels`. The harness uses this
  list to call out silently-dead channels that would otherwise hide
  behind a passing global SLO.
* The per-item ``slo_bucket`` tag is pre-computed from the global SLO
  thresholds carried on the snapshot. ``incomplete`` means the item's
  sync chain never reached both targets (``freshness_seconds=None``);
  ``breached_p95`` dominates ``breached_p50`` so one bucket per item is
  enough for the Markdown table.
* The exit-code helper treats an empty bounded corpus as a setup error
  (``bounded_item_count == 0``): the harness explicitly asks the operator
  to name an expected corpus size, so "I asked for nothing" never wins
  an exit 0. The dry-run tests cover this via the canned scenarios.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Literal

from memexpert.schemas.crawler import (
    CrawlerFreshnessSnapshot,
    CrawlerS04PerChannelSummary,
    CrawlerS04RunItemReport,
    CrawlerS04RunSummary,
)

_Mode = Literal["live", "catch_up_only", "dry_run"]
_Bucket = Literal["pass", "breached_p50", "breached_p95", "incomplete"]


def summarize_s04_run(
    snapshot: CrawlerFreshnessSnapshot,
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    mode: _Mode,
    api_base_url: str | None,
    channel_fixture_path: str | None,
    bounded_item_count: int,
    expected_channel_titles: Sequence[str] = (),
    errors: Sequence[str] = (),
) -> CrawlerS04RunSummary:
    """Reduce a freshness snapshot into the T04 machine-readable run summary.

    The caller supplies the ``bounded_item_count`` it expected to observe
    (the CLI's ``--candidate-limit``). When the snapshot carries fewer
    successfully-synced items than that, :func:`_exit_code_from_summary`
    will still mark the run as a failure — an under-sampled run cannot
    prove the SLO holds regardless of whether the observed percentiles
    happen to be below the threshold.

    ``expected_channel_titles`` is the ordered list of channel titles the
    harness was asked to observe. Any expected title that is absent from
    the snapshot's ``per_channel`` breakdown OR whose ``item_count`` is
    zero is reported in :attr:`stalled_channels` so the operator runbook
    can surface it loudly.
    """

    if bounded_item_count < 0:
        raise ValueError("bounded_item_count must be non-negative.")

    item_reports = _build_item_reports(snapshot)
    per_channel_rollups = _build_per_channel_rollups(snapshot)
    stalled_channels = _compute_stalled_channels(
        snapshot=snapshot,
        expected_channel_titles=expected_channel_titles,
    )
    observed_item_count = sum(
        1 for report in item_reports if report.freshness_seconds is not None
    )

    return CrawlerS04RunSummary(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        mode=mode,
        api_base_url=api_base_url,
        channel_fixture_path=channel_fixture_path,
        bounded_item_count=bounded_item_count,
        observed_item_count=observed_item_count,
        p50_seconds=snapshot.p50_seconds,
        p95_seconds=snapshot.p95_seconds,
        slo_p50_seconds=snapshot.slo_p50_seconds,
        slo_p95_seconds=snapshot.slo_p95_seconds,
        slo_p50_pass=snapshot.slo_p50_pass,
        slo_p95_pass=snapshot.slo_p95_pass,
        per_channel=per_channel_rollups,
        item_reports=item_reports,
        errors=tuple(errors),
        stalled_channels=stalled_channels,
    )


def _build_item_reports(
    snapshot: CrawlerFreshnessSnapshot,
) -> tuple[CrawlerS04RunItemReport, ...]:
    """Project snapshot sample items onto T04's item-report schema.

    Channel titles are resolved via the per-channel breakdown instead of
    a join: the snapshot already carries the breakdown and doing a second
    lookup per item is cheaper than threading a second dict through the
    call. If a sample item references a channel that does not appear in
    the breakdown (which would be a bug in the snapshot) we fall back to
    the raw id so the report stays renderable.
    """

    titles_by_channel = {
        row.source_channel_id: row.channel_title for row in snapshot.per_channel
    }
    reports: list[CrawlerS04RunItemReport] = []
    for sample in snapshot.sample_items:
        bucket = _bucket_for_item(
            freshness_seconds=sample.freshness_seconds,
            slo_p50_seconds=snapshot.slo_p50_seconds,
            slo_p95_seconds=snapshot.slo_p95_seconds,
        )
        reports.append(
            CrawlerS04RunItemReport(
                meme_file_id=sample.meme_file_id,
                source_channel_id=sample.source_channel_id,
                channel_title=titles_by_channel.get(
                    sample.source_channel_id,
                    str(sample.source_channel_id),
                ),
                published_at=sample.published_at,
                both_synced_at=sample.both_synced_at,
                freshness_seconds=sample.freshness_seconds,
                slo_bucket=bucket,
                pipeline_stage=sample.pipeline_stage,
                pipeline_status=sample.pipeline_status,
                failure_reason=sample.failure_reason,
                failure_text=sample.failure_text,
                qdrant_status=sample.qdrant_status,
                qdrant_reason=sample.qdrant_reason,
                qdrant_error=sample.qdrant_error,
                meili_status=sample.meili_status,
                meili_reason=sample.meili_reason,
                meili_error=sample.meili_error,
                searchability=sample.searchability,
            )
        )
    return tuple(reports)


def _bucket_for_item(
    *,
    freshness_seconds: float | None,
    slo_p50_seconds: float,
    slo_p95_seconds: float,
) -> _Bucket:
    """Map one item's freshness value onto the four report buckets.

    The ordering matters: we check ``p95`` first because it is the
    stricter threshold and dominates the Markdown colour signal. An item
    whose freshness is ``None`` never contributes to the SLO and shows up
    under ``incomplete`` — the harness treats it as a known gap, not a
    synthetic breach.
    """

    if freshness_seconds is None:
        return "incomplete"
    if freshness_seconds >= slo_p95_seconds:
        return "breached_p95"
    if freshness_seconds >= slo_p50_seconds:
        return "breached_p50"
    return "pass"


def _build_per_channel_rollups(
    snapshot: CrawlerFreshnessSnapshot,
) -> tuple[CrawlerS04PerChannelSummary, ...]:
    """Lift the snapshot's per-channel projection into T04's roll-up shape.

    The snapshot already computed per-channel percentiles; this helper
    only re-evaluates the SLO pass flags at the per-channel grain so the
    Markdown table can surface the single misbehaving channel without
    the operator re-deriving it. An empty channel (no items at all)
    collapses to ``(None, None, pass, pass)`` consistent with the
    "no data means pass" convention T03 locked in.
    """

    rollups: list[CrawlerS04PerChannelSummary] = []
    for entry in snapshot.per_channel:
        slo_p50_pass = entry.p50_seconds is None or entry.p50_seconds < snapshot.slo_p50_seconds
        slo_p95_pass = entry.p95_seconds is None or entry.p95_seconds < snapshot.slo_p95_seconds
        rollups.append(
            CrawlerS04PerChannelSummary(
                source_channel_id=entry.source_channel_id,
                channel_title=entry.channel_title,
                item_count=entry.item_count,
                p50_seconds=entry.p50_seconds,
                p95_seconds=entry.p95_seconds,
                slo_p50_pass=slo_p50_pass,
                slo_p95_pass=slo_p95_pass,
                most_recent_item_at=entry.most_recent_item_at,
                most_recent_freshness_seconds=entry.most_recent_freshness_seconds,
            )
        )
    return tuple(rollups)


def _compute_stalled_channels(
    *,
    snapshot: CrawlerFreshnessSnapshot,
    expected_channel_titles: Sequence[str],
) -> tuple[str, ...]:
    """Return the expected channel titles with no observed items in the snapshot.

    A channel is stalled either when it is absent from the snapshot
    entirely OR when it appears but carries ``item_count=0``. The second
    case is rare but can happen when the freshness query observed a
    channel row earlier and T04 polled a later empty window.
    """

    if not expected_channel_titles:
        return ()
    observed_titles_with_items: set[str] = {
        row.channel_title for row in snapshot.per_channel if row.item_count > 0
    }
    return tuple(
        title for title in expected_channel_titles if title not in observed_titles_with_items
    )


def _exit_code_from_summary(summary: CrawlerS04RunSummary) -> int:
    """Return the harness exit code for a fully-populated summary.

    The canonical rule is locked in the task text: a passing run requires
    ``slo_p50_pass AND slo_p95_pass AND observed_item_count >=
    bounded_item_count``. Everything else is a runtime failure (exit 2).
    Setup failures (missing fixture, API unreachable, etc.) are reported
    separately by the harness before it ever calls this helper — they
    translate to exit 1 and never construct a summary through this path.
    """

    if not summary.slo_p50_pass:
        return 2
    if not summary.slo_p95_pass:
        return 2
    if summary.observed_item_count < summary.bounded_item_count:
        return 2
    return 0


def render_s04_markdown_report(summary: CrawlerS04RunSummary) -> str:
    """Render the operator-facing Markdown companion to the JSON report.

    The sections mirror the S02/S03 runbooks so operators only learn one
    shape. Every per-item row carries a drill-down link into the enriched
    pipeline inspect surface (``/api/v1/pipeline/items/<id>/detail``)
    because T03's operator crawler surface does not have a per-item
    detail route of its own — the pipeline surface already exposes every
    stage + sync snapshot needed to chase a breached SLO.
    """

    lines: list[str] = []
    lines.append(f"# Crawler S04 freshness run {summary.run_id}")
    lines.append("")
    lines.extend(_render_overview(summary))
    lines.extend(_render_freshness_section(summary))
    lines.extend(_render_per_channel_section(summary))
    lines.extend(_render_stalled_channels_section(summary))
    lines.extend(_render_sample_items_section(summary))
    lines.extend(_render_errors_section(summary))
    return "\n".join(lines)


def _render_overview(summary: CrawlerS04RunSummary) -> list[str]:
    lines = ["## Overview", ""]
    lines.append(f"- Mode: `{summary.mode}`")
    lines.append(f"- API base: `{summary.api_base_url or '—'}`")
    lines.append(f"- Channel fixture: `{summary.channel_fixture_path or '—'}`")
    lines.append(f"- Started: {summary.started_at.isoformat()}")
    lines.append(f"- Finished: {summary.finished_at.isoformat()}")
    lines.append(f"- Bounded item count (expected): {summary.bounded_item_count}")
    lines.append(f"- Observed item count (synced both targets): {summary.observed_item_count}")
    verdict = _overall_verdict_label(summary)
    lines.append(f"- SLO verdict: **{verdict}**")
    lines.append("")
    return lines


def _render_freshness_section(summary: CrawlerS04RunSummary) -> list[str]:
    lines = ["## Freshness", ""]
    lines.append("| metric | observed | threshold | pass |")
    lines.append("|--------|----------|-----------|------|")
    lines.append(
        f"| p50 | {_format_seconds(summary.p50_seconds)} | "
        f"{_format_seconds(summary.slo_p50_seconds)} | "
        f"{_format_bool(summary.slo_p50_pass)} |"
    )
    lines.append(
        f"| p95 | {_format_seconds(summary.p95_seconds)} | "
        f"{_format_seconds(summary.slo_p95_seconds)} | "
        f"{_format_bool(summary.slo_p95_pass)} |"
    )
    lines.append("")
    return lines


def _render_per_channel_section(summary: CrawlerS04RunSummary) -> list[str]:
    lines = ["## Per-channel breakdown", ""]
    if not summary.per_channel:
        lines.append("_(no channels observed)_")
        lines.append("")
        return lines
    lines.append("| channel | items | p50 | p95 | slo_p50 | slo_p95 | most recent freshness |")
    lines.append("|---------|-------|-----|-----|---------|---------|-----------------------|")
    for entry in summary.per_channel:
        lines.append(
            f"| {entry.channel_title} | {entry.item_count} | "
            f"{_format_seconds(entry.p50_seconds)} | "
            f"{_format_seconds(entry.p95_seconds)} | "
            f"{_format_bool(entry.slo_p50_pass)} | "
            f"{_format_bool(entry.slo_p95_pass)} | "
            f"{_format_seconds(entry.most_recent_freshness_seconds)} |"
        )
    lines.append("")
    return lines


def _render_stalled_channels_section(summary: CrawlerS04RunSummary) -> list[str]:
    lines = ["## Stalled channels", ""]
    if not summary.stalled_channels:
        lines.append("_(none — every expected channel produced at least one item)_")
        lines.append("")
        return lines
    for title in summary.stalled_channels:
        lines.append(f"- `{title}` — check session flood-wait / ban state via `GET /api/v1/crawler/sessions`.")
    lines.append("")
    return lines


def _render_sample_items_section(summary: CrawlerS04RunSummary) -> list[str]:
    lines = ["## Sample items", ""]
    if not summary.item_reports:
        lines.append("_(no sample items observed)_")
        lines.append("")
        return lines
    lines.append("| meme_file_id | channel | searchable | stage | qdrant | meili | freshness | bucket | drill-down |")
    lines.append("|--------------|---------|------------|-------|--------|-------|-----------|--------|------------|")
    for report in summary.item_reports:
        url = f"/api/v1/pipeline/items/{report.meme_file_id}/detail"
        lines.append(
            f"| `{report.meme_file_id}` | {report.channel_title} | "
            f"{report.searchability or 'unknown'} | "
            f"{_format_stage_state(report.pipeline_stage, report.pipeline_status, report.failure_reason)} | "
            f"{_format_target_state(report.qdrant_status, report.qdrant_reason)} | "
            f"{_format_target_state(report.meili_status, report.meili_reason)} | "
            f"{_format_seconds(report.freshness_seconds)} | {report.slo_bucket} | "
            f"[detail]({url}) |"
        )
    lines.append("")
    return lines


def _render_errors_section(summary: CrawlerS04RunSummary) -> list[str]:
    lines = ["## Errors", ""]
    if not summary.errors:
        lines.append("_(none)_")
        lines.append("")
        return lines
    for error_line in summary.errors:
        lines.append(f"- {error_line}")
    lines.append("")
    return lines


def _format_stage_state(
    stage: object | None,
    status: object | None,
    reason: str | None,
) -> str:
    if stage is None or status is None:
        return "unknown"
    rendered = f"{_enum_value(stage)}:{_enum_value(status)}"
    if reason:
        rendered += f" ({reason})"
    return rendered


def _format_target_state(status: object | None, reason: str | None) -> str:
    if status is None:
        return "unknown"
    rendered = _enum_value(status)
    if reason:
        rendered += f" ({reason})"
    return rendered


def _enum_value(value: object) -> str:
    return getattr(value, "value", str(value))


def _overall_verdict_label(summary: CrawlerS04RunSummary) -> str:
    """Return a single pass/fail string for the overview header."""

    if _exit_code_from_summary(summary) == 0:
        return "PASS"
    reasons: list[str] = []
    if not summary.slo_p50_pass:
        reasons.append("p50 breached")
    if not summary.slo_p95_pass:
        reasons.append("p95 breached")
    if (
        summary.bounded_item_count > 0
        and summary.observed_item_count < summary.bounded_item_count
    ):
        reasons.append(
            f"observed {summary.observed_item_count} of {summary.bounded_item_count}",
        )
    if not reasons:
        reasons.append("unknown")
    return "FAIL (" + ", ".join(reasons) + ")"


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "—"
    if math.isnan(value) or math.isinf(value):
        return "—"
    return f"{value:.3f}"


def _format_bool(value: bool) -> str:
    return "yes" if value else "no"


def iter_expected_channel_titles(
    titles: Iterable[str],
) -> tuple[str, ...]:
    """Return an order-preserving, de-duplicated tuple of expected channel titles.

    Kept here (instead of in the harness module) so tests can call it
    without importing the script. Order preservation matters because the
    Markdown report prints stalled channels in the same order the
    operator listed them in the fixture.
    """

    seen: set[str] = set()
    ordered: list[str] = []
    for title in titles:
        if title in seen:
            continue
        seen.add(title)
        ordered.append(title)
    return tuple(ordered)


__all__ = [
    "_exit_code_from_summary",
    "iter_expected_channel_titles",
    "render_s04_markdown_report",
    "summarize_s04_run",
]
