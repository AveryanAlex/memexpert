#!/usr/bin/env python3
"""Prove the S04 Telegram crawler chain against the live freshness SLO.

This harness is the operator-runnable proof surface for milestone M002
slice S04: it asks the running pipeline stack what the current bounded
freshness snapshot looks like and decides pass/fail against the SLO
numbers configured in ``memexpert.core.config.Settings``. It mirrors the
structural shape of ``scripts/verify_s02_runtime.py`` and
``scripts/verify_s03_runtime.py`` so operators learn a single harness
grammar across the three slices.

Live mode semantics:

1. The harness assumes the operator has already started the crawler
   runtime (catch-up + live listener) out-of-band. The runbook at
   ``docs/ops/content-pipeline-telegram-crawler.md`` documents the ad-hoc
   driver path used until the worker entrypoint is wired.
2. The harness then polls ``GET /api/v1/crawler/freshness`` on a
   configurable interval, captures the final snapshot once the budget
   expires, and writes a JSON + Markdown artifact pair under
   ``.artifacts/s04-runtime-smoke/<run-id>/``.
3. Exit 0 iff the final snapshot proves both p50 and p95 are inside the
   SLO AND the observed item count reached ``--candidate-limit``.
   Everything else exits 2 (runtime failure); setup errors exit 1.

Dry-run mode (``--dry-run``) never touches the network. It takes a
canned :class:`CrawlerFreshnessSnapshot` built per the requested
``--dry-run-slo-scenario`` and runs it through the same
``summarize_s04_run`` + ``render_s04_markdown_report`` pipeline the live
path uses. Tests exercise the dry-run path to assert the argparse,
artifact, and exit-code surfaces without a stack.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

import httpx
from pydantic import ValidationError

from memexpert.core.config import get_settings
from memexpert.schemas.content_pipeline import ContentPipelineErrorResponse
from memexpert.schemas.crawler import (
    CrawlerChannelRead,
    CrawlerFreshnessChannelBreakdown,
    CrawlerFreshnessSampleItem,
    CrawlerFreshnessSnapshot,
    CrawlerS04RunSummary,
)
from memexpert.services.crawler_s04_report import (
    _exit_code_from_summary,
    iter_expected_channel_titles,
    render_s04_markdown_report,
    summarize_s04_run,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_ARTIFACTS_DIR: Final = REPO_ROOT / ".artifacts" / "s04-runtime-smoke"
DEFAULT_CHANNEL_FIXTURE_PATH: Final = (
    REPO_ROOT / "memexpert" / "crawlers" / "telegram" / "channels.example.yaml"
)
DEFAULT_CANDIDATE_LIMIT: Final = 8
DEFAULT_LIVE_DURATION_SECONDS: Final = 120.0
DEFAULT_STAGE_TIMEOUT_SECONDS: Final = 300.0
DEFAULT_POLL_INTERVAL_SECONDS: Final = 5.0
DEFAULT_API_TIMEOUT_SECONDS: Final = 30.0
DEFAULT_SESSION_NAME: Final = "primary"

_DRY_RUN_SCENARIOS: Final[frozenset[str]] = frozenset(
    {"pass", "fail-p50", "fail-p95", "empty"},
)


class SmokeError(RuntimeError):
    """Raised when the S04 proof harness cannot continue truthfully."""


class SetupError(SmokeError):
    """Raised when a pre-flight check blocks the harness before it begins work.

    Kept distinct from :class:`SmokeError` so :func:`main` can translate
    setup problems into exit code ``1`` while real runtime freshness
    failures fall out via the summary + :func:`_exit_code_from_summary`
    path with exit code ``2``. Missing fixture files, API reachability
    failures, and missing channel rows all raise this class.
    """


@dataclass(frozen=True, slots=True)
class CrawlerChannelFixtureEntry:
    """One curated channel the operator expects the harness to observe.

    Kept as a plain dataclass (not a pydantic schema) because the fixture
    file is operator configuration — not a durable service contract —
    and we want the "malformed fixture" failure mode to surface as a
    readable :class:`SetupError` from :func:`load_channel_fixture`, not
    a pydantic ``ValidationError`` the operator has to decode.
    """

    platform_id: str
    username: str | None
    title: str
    session_name: str


@dataclass(frozen=True, slots=True)
class S04RunArtifacts:
    """Stable artifact paths written during an S04 proof run."""

    root: Path
    report_json: Path
    report_markdown: Path


@dataclass(slots=True)
class _FixtureTemplate:
    """In-process curated-channel fixture used by the dry-run path.

    The dry-run harness cannot read :data:`DEFAULT_CHANNEL_FIXTURE_PATH`
    in tests because the file is meant to be replaced with real operator
    channels and the YAML loader must round-trip through its own happy
    path in integration tests. Instead, dry-run mode uses a hard-coded
    three-channel template that produces stable, reproducible fixtures
    for the aggregation + rendering tests.
    """

    entries: tuple[CrawlerChannelFixtureEntry, ...] = field(
        default=(
            CrawlerChannelFixtureEntry(
                platform_id="@dry_run_channel_a",
                username="dry_run_channel_a",
                title="Dry-run Channel A",
                session_name=DEFAULT_SESSION_NAME,
            ),
            CrawlerChannelFixtureEntry(
                platform_id="@dry_run_channel_b",
                username="dry_run_channel_b",
                title="Dry-run Channel B",
                session_name=DEFAULT_SESSION_NAME,
            ),
            CrawlerChannelFixtureEntry(
                platform_id="@dry_run_channel_c",
                username="dry_run_channel_c",
                title="Dry-run Channel C",
                session_name=DEFAULT_SESSION_NAME,
            ),
        ),
    )


_DRY_RUN_TEMPLATE = _FixtureTemplate()


class CrawlerApiClient:
    """Typed HTTP client wrapper for the operator crawler surface."""

    def __init__(self, *, base_url: str, operator_token: str, timeout_seconds: float) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={"X-Memexpert-Operator-Token": operator_token},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CrawlerApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def healthcheck(self) -> None:
        response = self._client.get("/health")
        if response.status_code != 200:
            raise SetupError(
                f"GET /health returned unexpected status {response.status_code}: {response.text!r}",
            )

    def list_channels(self) -> list[CrawlerChannelRead]:
        response = self._client.get("/api/v1/crawler/channels")
        payload = _unpack_json_or_raise(response)
        if response.status_code != 200:
            raise SmokeError(_format_api_error("GET /api/v1/crawler/channels", response, payload))
        if not isinstance(payload, list):
            raise SmokeError(
                "GET /api/v1/crawler/channels returned a non-list payload: "
                f"{type(payload).__name__}",
            )
        try:
            return [CrawlerChannelRead.model_validate(entry) for entry in payload]
        except ValidationError as exc:
            raise SmokeError(
                f"GET /api/v1/crawler/channels returned a malformed payload: {exc}",
            ) from exc

    def get_freshness(self) -> CrawlerFreshnessSnapshot:
        response = self._client.get("/api/v1/crawler/freshness")
        payload = _unpack_json_or_raise(response)
        if response.status_code != 200:
            raise SmokeError(_format_api_error("GET /api/v1/crawler/freshness", response, payload))
        try:
            return CrawlerFreshnessSnapshot.model_validate(payload)
        except ValidationError as exc:
            raise SmokeError(
                f"GET /api/v1/crawler/freshness returned a malformed payload: {exc}",
            ) from exc

    def resume_channel(self, source_channel_id: uuid.UUID) -> None:
        """Call the idempotent resume route on one curated channel.

        The harness uses this as the closest-supported "ack the channel is
        expected to be live" signal T03 exposes. It never advances the
        checkpoint or injects new messages; it exists only so the run
        artifact can record that the operator asked for live mode on the
        fixture channels before polling freshness.
        """

        response = self._client.post(
            f"/api/v1/crawler/channels/{source_channel_id}/resume",
        )
        if response.status_code != 200:
            payload = _unpack_json_or_raise(response)
            raise SmokeError(
                _format_api_error(
                    f"POST /api/v1/crawler/channels/{source_channel_id}/resume",
                    response,
                    payload,
                ),
            )


def _unpack_json_or_raise(response: httpx.Response) -> object:
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise SmokeError(
            f"{response.request.method} {response.request.url.path} returned non-JSON output: "
            f"{exc}; body={response.text!r}",
        ) from exc


def _format_api_error(
    route: str,
    response: httpx.Response,
    payload: object,
) -> str:
    try:
        error_payload = ContentPipelineErrorResponse.model_validate(payload)
    except ValidationError:
        rendered = (
            json.dumps(payload, sort_keys=True)
            if isinstance(payload, dict | list)
            else repr(payload)
        )
        return (
            f"{route} failed with HTTP {response.status_code} and a malformed "
            f"payload: {rendered}"
        )
    return (
        f"{route} failed with HTTP {response.status_code}: "
        f"{error_payload.code.value} — {error_payload.detail}"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the S04 proof-harness configuration."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-base-url",
        default="http://127.0.0.1:8000",
        help="Base URL of the running local API (default: %(default)s).",
    )
    parser.add_argument(
        "--operator-token",
        default=None,
        help=(
            "Operator token for the /api/v1/crawler/* routes. Defaults to "
            "PIPELINE_OPERATOR_TOKEN from the process settings; required in live mode."
        ),
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=DEFAULT_ARTIFACTS_DIR,
        help="Directory to write per-run JSON + Markdown summaries under.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Override the per-run identifier (defaults to a fresh uuid7 prefix).",
    )
    parser.add_argument(
        "--session-name",
        default=DEFAULT_SESSION_NAME,
        help="Telethon session the curated fixture is bound to (default: %(default)s).",
    )
    parser.add_argument(
        "--catch-up-only",
        action="store_true",
        help=(
            "Skip the live-listener observation phase. The harness will only poll "
            "freshness once after healthchecking the API."
        ),
    )
    parser.add_argument(
        "--live-duration-seconds",
        type=float,
        default=DEFAULT_LIVE_DURATION_SECONDS,
        help=(
            "Seconds to observe the live crawler for after the initial healthcheck "
            "(default: %(default)s). Ignored when --catch-up-only is set."
        ),
    )
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=DEFAULT_CANDIDATE_LIMIT,
        help=(
            "Maximum items the harness asks the freshness snapshot to aggregate AND "
            "the minimum item count a passing run must observe (default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--stage-timeout-seconds",
        type=float,
        default=DEFAULT_STAGE_TIMEOUT_SECONDS,
        help="Hard cap on the entire live-mode observation budget (default: %(default)s).",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="Seconds between freshness polls in live mode (default: %(default)s).",
    )
    parser.add_argument(
        "--api-timeout-seconds",
        type=float,
        default=DEFAULT_API_TIMEOUT_SECONDS,
        help="Per-request HTTP timeout used by the operator API client (default: %(default)s).",
    )
    parser.add_argument(
        "--channel-fixture-path",
        type=Path,
        default=DEFAULT_CHANNEL_FIXTURE_PATH,
        help=(
            "Path to the curated channel fixture file used in live mode "
            "(default: %(default)s). Accepts JSON (``*.json``) or the YAML shape "
            "documented in the example fixture."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Skip live HTTP calls and drive the aggregation + rendering path "
            "against a canned fixture snapshot. The artifact directory is still written."
        ),
    )
    parser.add_argument(
        "--dry-run-slo-scenario",
        choices=sorted(_DRY_RUN_SCENARIOS),
        default="pass",
        help=(
            "Which canned SLO scenario the dry-run path should reproduce "
            "(default: %(default)s). Only honored when --dry-run is set."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the full S04 proof harness and return an exit code.

    Exit codes:
        ``0`` — every bounded condition held (slo_p50_pass AND slo_p95_pass AND
               observed_item_count >= bounded_item_count).
        ``1`` — setup failure (fixture missing, API unreachable, channels not
               seeded, credentials missing).
        ``2`` — runtime failure (SLO breached or under-sampled corpus).
    """

    args = parse_args(argv)
    run_id = args.run_id or uuid.uuid7().hex[:12]
    artifacts = build_artifacts(artifacts_dir=args.artifacts_dir, run_id=run_id)

    if args.dry_run:
        summary = _run_dry(args=args, run_id=run_id)
        write_reports(artifacts, summary)
        print(f"S04 dry-run artifacts: {artifacts.root}")
        return _exit_code_from_summary(summary)

    try:
        summary = _run_live(args=args, run_id=run_id)
    except SetupError as exc:
        failure_summary = _build_failure_summary(
            run_id=run_id,
            mode="catch_up_only" if args.catch_up_only else "live",
            api_base_url=args.api_base_url,
            channel_fixture_path=str(args.channel_fixture_path),
            bounded_item_count=args.candidate_limit,
            errors=(str(exc),),
        )
        write_reports(artifacts, failure_summary)
        print(f"S04 setup failure. Artifacts: {artifacts.root}", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print(
            "Hint: make sure the crawler stack is running, the fixture is seeded, "
            "and PIPELINE_OPERATOR_TOKEN is set.",
            file=sys.stderr,
        )
        return 1
    except SmokeError as exc:
        failure_summary = _build_failure_summary(
            run_id=run_id,
            mode="catch_up_only" if args.catch_up_only else "live",
            api_base_url=args.api_base_url,
            channel_fixture_path=str(args.channel_fixture_path),
            bounded_item_count=args.candidate_limit,
            errors=(str(exc),),
        )
        write_reports(artifacts, failure_summary)
        print(f"S04 run failed. Artifacts: {artifacts.root}", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    write_reports(artifacts, summary)
    print(f"S04 artifacts: {artifacts.root}")
    verdict = "PASS" if _exit_code_from_summary(summary) == 0 else "FAIL"
    print(
        f"S04 run {verdict}: p50={summary.p50_seconds}, p95={summary.p95_seconds}, "
        f"observed={summary.observed_item_count}/{summary.bounded_item_count}",
    )
    return _exit_code_from_summary(summary)


def _run_live(*, args: argparse.Namespace, run_id: str) -> CrawlerS04RunSummary:
    """Drive the live freshness-polling path and return a populated summary."""

    started_at = datetime.now(tz=UTC)
    fixture_entries = load_channel_fixture(args.channel_fixture_path)
    expected_titles = iter_expected_channel_titles(entry.title for entry in fixture_entries)
    operator_token = _resolve_operator_token(args.operator_token)

    with CrawlerApiClient(
        base_url=args.api_base_url,
        operator_token=operator_token,
        timeout_seconds=args.api_timeout_seconds,
    ) as client:
        client.healthcheck()
        channels = client.list_channels()
        channels_by_platform_id = {row.platform_id: row for row in channels}
        missing_channels = [
            entry.platform_id
            for entry in fixture_entries
            if entry.platform_id not in channels_by_platform_id
        ]
        if missing_channels:
            raise SetupError(
                "Fixture channels are not seeded in the source_channels table: "
                + ", ".join(missing_channels)
                + ". Seed them via SQL or an Alembic data migration before running "
                "the harness (see docs/ops/content-pipeline-telegram-crawler.md).",
            )

        if not args.catch_up_only:
            for entry in fixture_entries:
                client.resume_channel(channels_by_platform_id[entry.platform_id].id)

        final_snapshot = _poll_freshness_until_budget_exhausted(
            client=client,
            catch_up_only=args.catch_up_only,
            live_duration_seconds=args.live_duration_seconds,
            stage_timeout_seconds=args.stage_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            candidate_limit=args.candidate_limit,
        )

    finished_at = datetime.now(tz=UTC)
    return summarize_s04_run(
        final_snapshot,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        mode="catch_up_only" if args.catch_up_only else "live",
        api_base_url=args.api_base_url,
        channel_fixture_path=str(args.channel_fixture_path),
        bounded_item_count=args.candidate_limit,
        expected_channel_titles=expected_titles,
    )


def _poll_freshness_until_budget_exhausted(
    *,
    client: CrawlerApiClient,
    catch_up_only: bool,
    live_duration_seconds: float,
    stage_timeout_seconds: float,
    poll_interval_seconds: float,
    candidate_limit: int,
) -> CrawlerFreshnessSnapshot:
    """Poll freshness until the configured budget expires or SLO is met.

    The polling loop is deliberately forgiving: the harness does NOT abort
    early on the first SLO breach because freshness is a moving target
    and one transient spike should not disqualify a run before catch-up
    completes. We only stop early when the snapshot clearly demonstrates
    a passing verdict (``observed_item_count >= candidate_limit`` AND
    both SLO pass flags are ``True``), which lets a healthy stack exit
    fast without waiting for the full ``--live-duration-seconds`` budget.
    """

    observation_budget = (
        0.0 if catch_up_only else max(0.0, float(live_duration_seconds))
    )
    hard_cap = max(observation_budget, float(stage_timeout_seconds))
    deadline = time.monotonic() + hard_cap
    last_snapshot = client.get_freshness()
    if catch_up_only:
        return last_snapshot

    while time.monotonic() < deadline:
        if _snapshot_meets_slo(last_snapshot, candidate_limit=candidate_limit):
            return last_snapshot
        time.sleep(max(poll_interval_seconds, 0.5))
        last_snapshot = client.get_freshness()
    return last_snapshot


def _snapshot_meets_slo(
    snapshot: CrawlerFreshnessSnapshot,
    *,
    candidate_limit: int,
) -> bool:
    """Return ``True`` when the live snapshot already passes the documented rule."""

    if not snapshot.slo_p50_pass or not snapshot.slo_p95_pass:
        return False
    synced_item_count = sum(
        1
        for item in snapshot.sample_items
        if item.freshness_seconds is not None
    )
    return synced_item_count >= candidate_limit


def _resolve_operator_token(explicit_token: str | None) -> str:
    """Return the operator token for live mode, preferring the CLI override."""

    if explicit_token is not None and explicit_token.strip():
        return explicit_token.strip()
    settings = get_settings()
    configured = settings.pipeline_operator_token.get_secret_value().strip()
    if not configured:
        raise SetupError(
            "No operator token provided. Set PIPELINE_OPERATOR_TOKEN in the "
            "environment or pass --operator-token on the CLI.",
        )
    return configured


def _run_dry(*, args: argparse.Namespace, run_id: str) -> CrawlerS04RunSummary:
    """Exercise the aggregation + rendering pipeline against a canned snapshot."""

    scenario = args.dry_run_slo_scenario
    settings = get_settings()
    slo_p50 = settings.crawler_freshness_slo_p50_seconds
    slo_p95 = settings.crawler_freshness_slo_p95_seconds
    snapshot = _build_dry_run_snapshot(
        scenario=scenario,
        slo_p50_seconds=slo_p50,
        slo_p95_seconds=slo_p95,
    )
    expected_titles = iter_expected_channel_titles(
        entry.title for entry in _DRY_RUN_TEMPLATE.entries
    )
    now = datetime.now(tz=UTC)
    # The ``empty`` scenario still asks the harness to observe the
    # template channels so tests can assert that an under-sampled run
    # fails (``observed=0`` while ``bounded>0``). Setting the bounded
    # budget to zero here would silently turn the scenario into a
    # trivial pass, which contradicts the documented exit-code rule.
    bounded_item_count = len(_DRY_RUN_TEMPLATE.entries)
    return summarize_s04_run(
        snapshot,
        run_id=run_id,
        started_at=now - timedelta(seconds=1),
        finished_at=now,
        mode="dry_run",
        api_base_url=None,
        channel_fixture_path=None,
        bounded_item_count=bounded_item_count,
        expected_channel_titles=expected_titles,
        errors=(f"dry-run scenario={scenario}: no live stack was exercised",),
    )


def _build_dry_run_snapshot(
    *,
    scenario: str,
    slo_p50_seconds: float,
    slo_p95_seconds: float,
) -> CrawlerFreshnessSnapshot:
    """Return a canned freshness snapshot for one of the documented scenarios.

    Scenarios:
        ``pass`` — every fixture channel produces one sub-SLO item.
        ``fail-p50`` — freshness values sit between ``slo_p50`` and ``slo_p95``.
        ``fail-p95`` — freshness values cross ``slo_p95``.
        ``empty`` — no channels, no items, no SLO samples.
    """

    now = datetime.now(tz=UTC)
    entries = _DRY_RUN_TEMPLATE.entries

    if scenario == "empty":
        return CrawlerFreshnessSnapshot(
            snapshot_evaluated_at=now,
            since=None,
            item_count=0,
            p50_seconds=None,
            p95_seconds=None,
            slo_p50_seconds=slo_p50_seconds,
            slo_p95_seconds=slo_p95_seconds,
            slo_p50_pass=True,
            slo_p95_pass=True,
            per_channel=(),
            sample_items=(),
        )

    if scenario == "fail-p50":
        freshness_values = tuple(
            slo_p50_seconds + index * 2.0 for index in range(1, len(entries) + 1)
        )
    elif scenario == "fail-p95":
        freshness_values = tuple(
            slo_p95_seconds + 10.0 + index for index in range(len(entries))
        )
    else:  # "pass"
        freshness_values = tuple(
            max(1.0, slo_p50_seconds / 2.0 - index) for index in range(len(entries))
        )

    per_channel: list[CrawlerFreshnessChannelBreakdown] = []
    sample_items: list[CrawlerFreshnessSampleItem] = []
    for index, entry in enumerate(entries):
        channel_id = uuid.uuid5(uuid.NAMESPACE_OID, entry.platform_id)
        freshness = freshness_values[index]
        published_at = now - timedelta(seconds=freshness + 1.0)
        both_synced_at = published_at + timedelta(seconds=freshness)
        meme_file_id = uuid.uuid5(uuid.NAMESPACE_OID, f"{entry.platform_id}:0")
        sample_item = CrawlerFreshnessSampleItem(
            meme_file_id=meme_file_id,
            source_channel_id=channel_id,
            published_at=published_at,
            first_ingested_at=published_at,
            both_synced_at=both_synced_at,
            freshness_seconds=freshness,
        )
        sample_items.append(sample_item)
        per_channel.append(
            CrawlerFreshnessChannelBreakdown(
                source_channel_id=channel_id,
                platform_id=entry.platform_id,
                channel_title=entry.title,
                item_count=1,
                p50_seconds=freshness,
                p95_seconds=freshness,
                most_recent_item_at=published_at,
                most_recent_freshness_seconds=freshness,
            )
        )

    p50 = _simple_percentile(freshness_values, 0.5)
    p95 = _simple_percentile(freshness_values, 0.95)
    return CrawlerFreshnessSnapshot(
        snapshot_evaluated_at=now,
        since=None,
        item_count=len(sample_items),
        p50_seconds=p50,
        p95_seconds=p95,
        slo_p50_seconds=slo_p50_seconds,
        slo_p95_seconds=slo_p95_seconds,
        slo_p50_pass=p50 < slo_p50_seconds,
        slo_p95_pass=p95 < slo_p95_seconds,
        per_channel=tuple(per_channel),
        sample_items=tuple(sample_items),
    )


def _simple_percentile(values: Sequence[float], fraction: float) -> float:
    """Return a linear-interpolated percentile over ``values``.

    Kept intentionally local to the harness: the freshness service module
    owns its own copy and the reporting service module owns another. A
    script-side duplicate here is explicit and scoped to the dry-run
    canned-snapshot construction, so the three copies cannot drift apart
    without a visible test diff.
    """

    if not values:
        raise ValueError("_simple_percentile requires at least one value.")
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = fraction * (len(sorted_values) - 1)
    lower = int(rank)
    upper = lower + 1 if rank != lower else lower
    if upper >= len(sorted_values):
        return sorted_values[-1]
    lower_value = sorted_values[lower]
    upper_value = sorted_values[upper]
    return lower_value + (upper_value - lower_value) * (rank - lower)


def _build_failure_summary(
    *,
    run_id: str,
    mode: Literal["live", "catch_up_only"],
    api_base_url: str,
    channel_fixture_path: str,
    bounded_item_count: int,
    errors: tuple[str, ...],
) -> CrawlerS04RunSummary:
    """Return a minimal summary for a live-mode setup/runtime failure.

    The failure summary still needs to reference the configured SLO
    thresholds so the JSON payload carries honest numbers even when the
    harness never reached the freshness endpoint. We read them straight
    from ``get_settings()`` — this is the same source the running API
    uses, so the failure artifact reflects the SLO the operator would
    have been measured against on a successful run.
    """

    now = datetime.now(tz=UTC)
    settings = get_settings()
    return CrawlerS04RunSummary(
        run_id=run_id,
        started_at=now,
        finished_at=now,
        mode=mode,
        api_base_url=api_base_url,
        channel_fixture_path=channel_fixture_path,
        bounded_item_count=bounded_item_count,
        observed_item_count=0,
        p50_seconds=None,
        p95_seconds=None,
        slo_p50_seconds=settings.crawler_freshness_slo_p50_seconds,
        slo_p95_seconds=settings.crawler_freshness_slo_p95_seconds,
        slo_p50_pass=True,
        slo_p95_pass=True,
        per_channel=(),
        item_reports=(),
        errors=errors,
        stalled_channels=(),
    )


def load_channel_fixture(path: Path) -> list[CrawlerChannelFixtureEntry]:
    """Return the curated channel entries listed in the fixture file.

    Accepts JSON (``*.json``) and a minimal YAML subset matching the
    example fixture shape documented at
    :data:`DEFAULT_CHANNEL_FIXTURE_PATH`. The YAML path is intentionally
    hand-rolled so the harness does not depend on PyYAML (which is only
    available as a transitive ``uvicorn[standard]`` extra in this repo
    today). Any parse or schema failure raises :class:`SetupError` with
    an actionable message — the fixture is operator configuration, and
    the error needs to tell the operator what to fix.
    """

    if not path.is_file():
        raise SetupError(
            f"Channel fixture {path} does not exist. "
            "Copy memexpert/crawlers/telegram/channels.example.yaml and point "
            "--channel-fixture-path at the resulting file.",
        )
    raw_text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = _parse_fixture_json(raw_text, path=path)
    else:
        payload = _parse_fixture_yaml_subset(raw_text, path=path)
    return _validate_fixture_payload(payload, path=path)


def _parse_fixture_json(raw_text: str, *, path: Path) -> object:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SetupError(
            f"Channel fixture {path} is not valid JSON: {exc}",
        ) from exc


def _parse_fixture_yaml_subset(raw_text: str, *, path: Path) -> dict[str, object]:
    """Parse the narrowly-scoped YAML subset the example fixture uses.

    Supports only the exact ``channels:`` / ``- key: value`` shape shown in
    ``channels.example.yaml``. Comments start with ``#``, blank lines are
    ignored, and string values may be optionally quoted with either
    ``"`` or ``'``. Anything else raises :class:`SetupError`.

    The parser stays purpose-built so the harness has zero runtime
    dependency on PyYAML (see the module docstring for the rationale).
    """

    lines = raw_text.splitlines()
    channels: list[dict[str, object]] = []
    current_entry: dict[str, object] | None = None
    inside_channels = False

    for raw_line in lines:
        line = raw_line.rstrip()
        comment_stripped = _strip_yaml_comment(line)
        if not comment_stripped.strip():
            continue
        if not inside_channels:
            if comment_stripped.strip() == "channels:":
                inside_channels = True
                continue
            # Ignore any preamble lines before ``channels:``.
            continue
        if comment_stripped.startswith("  - "):
            if current_entry is not None:
                channels.append(current_entry)
            current_entry = {}
            key_value = comment_stripped[4:]
            _apply_yaml_key_value(current_entry, key_value, path=path)
            continue
        if comment_stripped.startswith("    ") and current_entry is not None:
            _apply_yaml_key_value(current_entry, comment_stripped.strip(), path=path)
            continue
        raise SetupError(
            f"Channel fixture {path} has an unexpected YAML line: {raw_line!r}. "
            "Only the shape used by channels.example.yaml is supported.",
        )

    if current_entry is not None:
        channels.append(current_entry)
    return {"channels": channels}


def _strip_yaml_comment(line: str) -> str:
    """Return ``line`` with any trailing ``# comment`` removed.

    The stripper respects simple double/single quoted values so a ``#``
    inside a quoted title does not accidentally truncate the value. It
    does not attempt to handle arbitrary YAML escape sequences — the
    operator-facing fixture does not need them.
    """

    in_single = False
    in_double = False
    for index, character in enumerate(line):
        if character == "'" and not in_double:
            in_single = not in_single
        elif character == '"' and not in_single:
            in_double = not in_double
        elif character == "#" and not in_single and not in_double:
            return line[:index].rstrip()
    return line


def _apply_yaml_key_value(
    entry: dict[str, object],
    key_value: str,
    *,
    path: Path,
) -> None:
    if ":" not in key_value:
        raise SetupError(
            f"Channel fixture {path} has an entry missing a `:` separator: {key_value!r}",
        )
    key, _, raw_value = key_value.partition(":")
    key = key.strip()
    raw_value = raw_value.strip()
    if not key:
        raise SetupError(
            f"Channel fixture {path} has an entry with an empty key: {key_value!r}",
        )
    entry[key] = _coerce_yaml_scalar(raw_value)


def _coerce_yaml_scalar(raw_value: str) -> object:
    """Return the Python value corresponding to a YAML scalar literal."""

    if raw_value == "" or raw_value.lower() == "null" or raw_value == "~":
        return None
    if (raw_value.startswith('"') and raw_value.endswith('"')) or (
        raw_value.startswith("'") and raw_value.endswith("'")
    ):
        return raw_value[1:-1]
    if raw_value.lower() in {"true", "false"}:
        return raw_value.lower() == "true"
    return raw_value


def _validate_fixture_payload(
    payload: object,
    *,
    path: Path,
) -> list[CrawlerChannelFixtureEntry]:
    if not isinstance(payload, dict):
        raise SetupError(
            f"Channel fixture {path} must be a mapping with a `channels` key.",
        )
    raw_channels = payload.get("channels")
    if not isinstance(raw_channels, list) or not raw_channels:
        raise SetupError(
            f"Channel fixture {path} must define a non-empty `channels` list.",
        )
    entries: list[CrawlerChannelFixtureEntry] = []
    for index, raw_entry in enumerate(raw_channels):
        if not isinstance(raw_entry, dict):
            raise SetupError(
                f"Channel fixture {path} channel[{index}] is not a mapping: {raw_entry!r}",
            )
        entry = cast("dict[str, object]", raw_entry)
        try:
            platform_id = _require_non_blank_string(entry, "platform_id", path=path)
            title = _require_non_blank_string(entry, "title", path=path)
            session_name = _require_non_blank_string(entry, "session_name", path=path)
        except KeyError as exc:
            raise SetupError(
                f"Channel fixture {path} channel[{index}] is missing required key: {exc.args[0]!r}",
            ) from exc
        username_raw = entry.get("username")
        username = (
            username_raw.strip() if isinstance(username_raw, str) and username_raw.strip() else None
        )
        entries.append(
            CrawlerChannelFixtureEntry(
                platform_id=platform_id,
                username=username,
                title=title,
                session_name=session_name,
            )
        )
    return entries


def _require_non_blank_string(
    mapping: Mapping[str, object],
    key: str,
    *,
    path: Path,
) -> str:
    if key not in mapping:
        raise KeyError(key)
    value = mapping[key]
    if not isinstance(value, str) or not value.strip():
        raise SetupError(
            f"Channel fixture {path} must define a non-blank string for `{key}`, got {value!r}.",
        )
    return value.strip()


def build_artifacts(*, artifacts_dir: Path, run_id: str) -> S04RunArtifacts:
    """Create a per-run artifact directory and return its stable paths."""

    root = artifacts_dir / run_id
    root.mkdir(parents=True, exist_ok=True)
    return S04RunArtifacts(
        root=root,
        report_json=root / "report.json",
        report_markdown=root / "report.md",
    )


def write_reports(
    artifacts: S04RunArtifacts,
    summary: CrawlerS04RunSummary,
) -> None:
    """Persist the JSON + Markdown proof-run summary under the artifact directory."""

    payload = summary.model_dump(mode="json")
    artifacts.report_json.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    artifacts.report_markdown.write_text(
        render_s04_markdown_report(summary),
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())
