"""Integration tests for ``scripts/verify_s04_runtime.py``.

These tests never touch the network, never import telethon, and never
construct a DB session. They cover the dry-run artifact round-trip, the
argparse surface, the pure aggregation + rendering + exit-code helpers,
and the YAML/JSON channel fixture loader. A real freshness DB path is
already exercised by ``tests/integration/test_crawler_routes.py``, so
this module is free to stay entirely in-process.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memexpert.models.enums import (
    ContentPipelineStage,
    ContentPipelineStageStatus,
    SyncTargetStatus,
)
from memexpert.schemas.crawler import (
    CrawlerFreshnessChannelBreakdown,
    CrawlerFreshnessSampleItem,
    CrawlerFreshnessSnapshot,
)
from memexpert.services.crawler_s04_report import (
    _exit_code_from_summary,
    render_s04_markdown_report,
    summarize_s04_run,
)

REPO_ROOT_FOR_SCRIPT = Path(__file__).resolve().parents[2]


def _load_verify_s04_runtime_module(name: str) -> Any:
    """Import scripts/verify_s04_runtime.py under a unique module name for tests."""

    spec = importlib.util.spec_from_file_location(
        name,
        REPO_ROOT_FOR_SCRIPT / "scripts" / "verify_s04_runtime.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _build_snapshot(
    *,
    item_freshness_values: tuple[float | None, ...],
    channel_titles: tuple[str, ...] = ("Primary",),
    slo_p50_seconds: float = 60.0,
    slo_p95_seconds: float = 180.0,
) -> CrawlerFreshnessSnapshot:
    """Return a synthetic :class:`CrawlerFreshnessSnapshot` for aggregation tests."""

    evaluated_at = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    per_channel: list[CrawlerFreshnessChannelBreakdown] = []
    sample_items: list[CrawlerFreshnessSampleItem] = []
    for channel_index, title in enumerate(channel_titles):
        channel_id = uuid.uuid5(uuid.NAMESPACE_OID, title)
        item_index = channel_index
        if item_index >= len(item_freshness_values):
            per_channel.append(
                CrawlerFreshnessChannelBreakdown(
                    source_channel_id=channel_id,
                    platform_id=f"@{title}",
                    channel_title=title,
                    item_count=0,
                    p50_seconds=None,
                    p95_seconds=None,
                    most_recent_item_at=None,
                    most_recent_freshness_seconds=None,
                )
            )
            continue
        freshness = item_freshness_values[item_index]
        meme_file_id = uuid.uuid5(uuid.NAMESPACE_OID, f"{title}:{item_index}")
        published_at = evaluated_at
        both_synced_at = None
        if freshness is not None:
            from datetime import timedelta

            both_synced_at = published_at + timedelta(seconds=freshness)
        sample_items.append(
            CrawlerFreshnessSampleItem(
                meme_file_id=meme_file_id,
                source_channel_id=channel_id,
                published_at=published_at,
                first_ingested_at=published_at,
                both_synced_at=both_synced_at,
                freshness_seconds=freshness,
            )
        )
        per_channel.append(
            CrawlerFreshnessChannelBreakdown(
                source_channel_id=channel_id,
                platform_id=f"@{title}",
                channel_title=title,
                item_count=1,
                p50_seconds=freshness,
                p95_seconds=freshness,
                most_recent_item_at=published_at,
                most_recent_freshness_seconds=freshness,
            )
        )

    valid_values = [value for value in item_freshness_values if value is not None]
    p50: float | None = None
    p95: float | None = None
    if valid_values:
        sorted_values = sorted(valid_values)
        p50 = sorted_values[len(sorted_values) // 2]
        p95 = sorted_values[-1]

    return CrawlerFreshnessSnapshot(
        snapshot_evaluated_at=evaluated_at,
        since=None,
        item_count=len(sample_items),
        p50_seconds=p50,
        p95_seconds=p95,
        slo_p50_seconds=slo_p50_seconds,
        slo_p95_seconds=slo_p95_seconds,
        slo_p50_pass=p50 is None or p50 < slo_p50_seconds,
        slo_p95_pass=p95 is None or p95 < slo_p95_seconds,
        per_channel=tuple(per_channel),
        sample_items=tuple(sample_items),
    )


def _build_summary(
    snapshot: CrawlerFreshnessSnapshot,
    *,
    bounded_item_count: int,
    expected_channel_titles: tuple[str, ...] = (),
    mode: str = "live",
) -> Any:
    now = datetime(2026, 4, 10, 12, 30, 0, tzinfo=UTC)
    return summarize_s04_run(
        snapshot,
        run_id="test-run",
        started_at=now,
        finished_at=now,
        mode=mode,  # type: ignore[arg-type]
        api_base_url="http://127.0.0.1:8000",
        channel_fixture_path="/tmp/fixture.yaml",
        bounded_item_count=bounded_item_count,
        expected_channel_titles=expected_channel_titles,
    )


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_defaults_are_sensible() -> None:
    """Argparse must accept an empty argv and emit the documented defaults."""

    module = _load_verify_s04_runtime_module("verify_s04_runtime_defaults")

    args = module.parse_args([])
    assert args.api_base_url == "http://127.0.0.1:8000"
    assert args.operator_token is None
    assert args.session_name == module.DEFAULT_SESSION_NAME
    assert args.catch_up_only is False
    assert args.live_duration_seconds == module.DEFAULT_LIVE_DURATION_SECONDS
    assert args.candidate_limit == module.DEFAULT_CANDIDATE_LIMIT
    assert args.stage_timeout_seconds == module.DEFAULT_STAGE_TIMEOUT_SECONDS
    assert args.poll_interval_seconds == module.DEFAULT_POLL_INTERVAL_SECONDS
    assert args.api_timeout_seconds == module.DEFAULT_API_TIMEOUT_SECONDS
    assert args.channel_fixture_path == module.DEFAULT_CHANNEL_FIXTURE_PATH
    assert args.dry_run is False
    assert args.dry_run_slo_scenario == "pass"


def test_parse_args_rejects_invalid_dry_run_scenario() -> None:
    """Unknown ``--dry-run-slo-scenario`` values must exit via argparse."""

    module = _load_verify_s04_runtime_module("verify_s04_runtime_invalid_scenario")

    with pytest.raises(SystemExit):
        module.parse_args(["--dry-run", "--dry-run-slo-scenario", "totally-made-up"])


# ---------------------------------------------------------------------------
# dry-run artifact round-trip
# ---------------------------------------------------------------------------


def test_dry_run_writes_artifacts_with_expected_fields(tmp_path: Path) -> None:
    """``--dry-run`` must persist a JSON + Markdown summary with the documented keys."""

    module = _load_verify_s04_runtime_module("verify_s04_runtime_dry_artifacts")

    exit_code = module.main(
        [
            "--dry-run",
            "--artifacts-dir",
            str(tmp_path),
            "--run-id",
            "dry-artifacts",
        ],
    )
    assert exit_code == 0

    run_dir = tmp_path / "dry-artifacts"
    report_json = run_dir / "report.json"
    report_md = run_dir / "report.md"
    assert report_json.is_file()
    assert report_md.is_file()

    payload = json.loads(report_json.read_text(encoding="utf-8"))
    required_keys = {
        "run_id",
        "started_at",
        "finished_at",
        "mode",
        "api_base_url",
        "channel_fixture_path",
        "bounded_item_count",
        "observed_item_count",
        "p50_seconds",
        "p95_seconds",
        "slo_p50_seconds",
        "slo_p95_seconds",
        "slo_p50_pass",
        "slo_p95_pass",
        "per_channel",
        "item_reports",
        "errors",
        "stalled_channels",
    }
    assert required_keys.issubset(payload.keys())
    assert payload["run_id"] == "dry-artifacts"
    assert payload["mode"] == "dry_run"
    assert payload["api_base_url"] is None
    assert payload["channel_fixture_path"] is None
    assert payload["bounded_item_count"] == 3
    assert payload["observed_item_count"] == 3

    markdown = report_md.read_text(encoding="utf-8")
    assert "# Crawler S04 freshness run dry-artifacts" in markdown
    assert "## Overview" in markdown
    assert "## Freshness" in markdown
    assert "## Per-channel breakdown" in markdown
    assert "## Stalled channels" in markdown
    assert "## Sample items" in markdown


def test_dry_run_pass_scenario_exits_zero(tmp_path: Path) -> None:
    """The ``pass`` scenario must resolve to exit 0."""

    module = _load_verify_s04_runtime_module("verify_s04_runtime_dry_pass")

    exit_code = module.main(
        [
            "--dry-run",
            "--dry-run-slo-scenario",
            "pass",
            "--artifacts-dir",
            str(tmp_path),
            "--run-id",
            "dry-pass",
        ],
    )
    assert exit_code == 0


def test_dry_run_fail_p95_scenario_exits_two(tmp_path: Path) -> None:
    """Breaching p95 must resolve to exit 2."""

    module = _load_verify_s04_runtime_module("verify_s04_runtime_dry_fail_p95")

    exit_code = module.main(
        [
            "--dry-run",
            "--dry-run-slo-scenario",
            "fail-p95",
            "--artifacts-dir",
            str(tmp_path),
            "--run-id",
            "dry-fail-p95",
        ],
    )
    assert exit_code == 2

    payload = json.loads(
        (tmp_path / "dry-fail-p95" / "report.json").read_text(encoding="utf-8"),
    )
    assert payload["slo_p95_pass"] is False


def test_dry_run_empty_snapshot_pass(tmp_path: Path) -> None:
    """A zero-item snapshot with a non-zero bounded budget must exit 2.

    The test name mirrors the task plan; the assertion matches the
    documented "observed < bounded forces exit 2" rule so an empty run
    cannot claim a trivial pass just because the snapshot SLO defaults
    to ``True`` on no data.
    """

    module = _load_verify_s04_runtime_module("verify_s04_runtime_dry_empty")

    exit_code = module.main(
        [
            "--dry-run",
            "--dry-run-slo-scenario",
            "empty",
            "--artifacts-dir",
            str(tmp_path),
            "--run-id",
            "dry-empty",
        ],
    )
    assert exit_code == 2


# ---------------------------------------------------------------------------
# summarize_s04_run + render_s04_markdown_report
# ---------------------------------------------------------------------------


def test_summarize_s04_run_empty_snapshot() -> None:
    """An empty snapshot yields zero observed items and no per-channel rows."""

    snapshot = _build_snapshot(item_freshness_values=(), channel_titles=())
    summary = _build_summary(
        snapshot,
        bounded_item_count=3,
        expected_channel_titles=("Primary", "Secondary"),
    )

    assert summary.observed_item_count == 0
    assert summary.bounded_item_count == 3
    assert summary.item_reports == ()
    assert summary.per_channel == ()
    assert summary.stalled_channels == ("Primary", "Secondary")
    assert _exit_code_from_summary(summary) == 2


def test_summarize_s04_run_mixed_slo_buckets() -> None:
    """Partial pass / partial breach populates bucket tags and stalled channels."""

    snapshot = _build_snapshot(
        item_freshness_values=(10.0, 200.0, None),
        channel_titles=("Fast", "Slow", "Incomplete"),
        slo_p50_seconds=60.0,
        slo_p95_seconds=180.0,
    )
    summary = _build_summary(
        snapshot,
        bounded_item_count=2,
        expected_channel_titles=("Fast", "Slow", "Incomplete", "Silent"),
    )

    bucket_by_channel = {report.channel_title: report.slo_bucket for report in summary.item_reports}
    assert bucket_by_channel["Fast"] == "pass"
    assert bucket_by_channel["Slow"] == "breached_p95"
    assert bucket_by_channel["Incomplete"] == "incomplete"
    # Only "Silent" is truly stalled — "Incomplete" has an item (just no freshness).
    assert "Silent" in summary.stalled_channels
    assert "Incomplete" not in summary.stalled_channels
    assert summary.observed_item_count == 2


def test_summarize_s04_run_propagates_stage_and_search_sync_evidence() -> None:
    """S04 artifacts must carry concrete stage/target evidence for blocked samples."""

    evaluated_at = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    channel_id = uuid.uuid5(uuid.NAMESPACE_OID, "Blocked")
    meme_file_id = uuid.uuid5(uuid.NAMESPACE_OID, "Blocked:item")
    snapshot = CrawlerFreshnessSnapshot(
        snapshot_evaluated_at=evaluated_at,
        since=None,
        item_count=1,
        p50_seconds=None,
        p95_seconds=None,
        slo_p50_seconds=60.0,
        slo_p95_seconds=180.0,
        slo_p50_pass=True,
        slo_p95_pass=True,
        per_channel=(
            CrawlerFreshnessChannelBreakdown(
                source_channel_id=channel_id,
                platform_id="@blocked",
                channel_title="Blocked",
                item_count=1,
            ),
        ),
        sample_items=(
            CrawlerFreshnessSampleItem(
                meme_file_id=meme_file_id,
                source_channel_id=channel_id,
                published_at=evaluated_at,
                first_ingested_at=evaluated_at,
                pipeline_stage=ContentPipelineStage.SYNC_MEILI,
                pipeline_status=ContentPipelineStageStatus.FAILED,
                failure_reason="sync_meili_malformed_payload",
                failure_text="missing document id",
                qdrant_status=SyncTargetStatus.SYNCED,
                meili_status=SyncTargetStatus.FAILED,
                meili_reason="sync_meili_malformed_payload",
                meili_error="missing document id",
                searchability="partially_searchable",
            ),
        ),
    )

    summary = _build_summary(snapshot, bounded_item_count=1)

    report = summary.item_reports[0]
    assert report.searchability == "partially_searchable"
    assert report.pipeline_stage is ContentPipelineStage.SYNC_MEILI
    assert report.pipeline_status is ContentPipelineStageStatus.FAILED
    assert report.meili_status is SyncTargetStatus.FAILED
    assert report.meili_reason == "sync_meili_malformed_payload"

    markdown = render_s04_markdown_report(summary)
    assert "partially_searchable" in markdown
    assert "sync_meili:failed (sync_meili_malformed_payload)" in markdown
    assert "synced" in markdown
    assert "failed (sync_meili_malformed_payload)" in markdown


def test_render_s04_markdown_report_surfaces_stalled_channels_and_slo_verdict() -> None:
    """The Markdown rendering must highlight stalled channels + SLO verdict."""

    snapshot = _build_snapshot(
        item_freshness_values=(300.0,),
        channel_titles=("Loud Channel",),
        slo_p50_seconds=60.0,
        slo_p95_seconds=180.0,
    )
    summary = _build_summary(
        snapshot,
        bounded_item_count=5,
        expected_channel_titles=("Loud Channel", "Quiet Channel"),
    )

    markdown = render_s04_markdown_report(summary)
    assert "Quiet Channel" in markdown
    assert "SLO verdict: **FAIL" in markdown
    assert "p95 breached" in markdown
    assert "observed 1 of 5" in markdown
    assert "breached_p95" in markdown


def test_exit_code_helper_matches_spec() -> None:
    """The exit code helper returns 0/2 per the documented taxonomy."""

    passing_snapshot = _build_snapshot(
        item_freshness_values=(5.0, 10.0, 12.0),
        channel_titles=("A", "B", "C"),
    )
    passing_summary = _build_summary(passing_snapshot, bounded_item_count=3)
    assert _exit_code_from_summary(passing_summary) == 0

    under_sampled = _build_summary(passing_snapshot, bounded_item_count=10)
    assert _exit_code_from_summary(under_sampled) == 2

    breached_snapshot = _build_snapshot(
        item_freshness_values=(300.0, 400.0),
        channel_titles=("A", "B"),
    )
    breached_summary = _build_summary(breached_snapshot, bounded_item_count=2)
    assert _exit_code_from_summary(breached_summary) == 2


# ---------------------------------------------------------------------------
# load_channel_fixture
# ---------------------------------------------------------------------------


def test_load_channel_fixture_reads_yaml_example(tmp_path: Path) -> None:
    """The curated example YAML fixture must parse via the harness loader."""

    module = _load_verify_s04_runtime_module("verify_s04_runtime_fixture_yaml")

    example = (
        REPO_ROOT_FOR_SCRIPT
        / "memexpert"
        / "crawlers"
        / "telegram"
        / "channels.example.yaml"
    )
    entries = module.load_channel_fixture(example)
    assert len(entries) == 2
    platform_ids = {entry.platform_id for entry in entries}
    assert platform_ids == {"@example_memes_en", "@example_memes_ru"}
    titles = {entry.title for entry in entries}
    assert titles == {"Example Memes (EN)", "Example Memes (RU)"}
    assert all(entry.session_name == "primary" for entry in entries)


def test_load_channel_fixture_reads_json(tmp_path: Path) -> None:
    """JSON fixtures must parse via the same loader for operators who prefer them."""

    module = _load_verify_s04_runtime_module("verify_s04_runtime_fixture_json")

    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "channels": [
                    {
                        "platform_id": "@json_chan",
                        "username": "json_chan",
                        "title": "JSON Channel",
                        "session_name": "primary",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    entries = module.load_channel_fixture(fixture_path)
    assert len(entries) == 1
    assert entries[0].platform_id == "@json_chan"
    assert entries[0].title == "JSON Channel"


def test_load_channel_fixture_missing_file_raises_setup_error(tmp_path: Path) -> None:
    """A missing fixture must surface as :class:`SetupError` with an actionable message."""

    module = _load_verify_s04_runtime_module("verify_s04_runtime_fixture_missing")

    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(module.SetupError) as excinfo:
        module.load_channel_fixture(missing)
    assert "does not exist" in str(excinfo.value)
    assert "channels.example.yaml" in str(excinfo.value)


def test_load_channel_fixture_rejects_missing_required_fields(tmp_path: Path) -> None:
    """A fixture missing ``platform_id`` must surface as :class:`SetupError`."""

    module = _load_verify_s04_runtime_module("verify_s04_runtime_fixture_invalid")

    fixture_path = tmp_path / "broken.yaml"
    fixture_path.write_text(
        "channels:\n"
        "  - username: lonely\n"
        "    title: Lonely\n"
        "    session_name: primary\n",
        encoding="utf-8",
    )
    with pytest.raises(module.SetupError):
        module.load_channel_fixture(fixture_path)
