"""Integration tests for ``scripts/verify_s03_runtime.py``."""

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
    ContentLanguage,
    ContentPipelineStage,
    ContentPipelineStageStatus,
    SyncTargetKind,
    SyncTargetStatus,
)
from memexpert.schemas.content_pipeline import (
    ContentPipelineCanonicalContext,
    ContentPipelineClassificationDetail,
    ContentPipelineItemDetail,
    ContentPipelineMergeDetail,
    ContentPipelineOCRDetail,
    ContentPipelineReadyEventSummary,
    ContentPipelineStageJournalRead,
    PerTargetSyncStatus,
    SmokeProofResult,
    SmokeProofTargetResult,
)

REPO_ROOT_FOR_SCRIPT = Path(__file__).resolve().parents[2]


def _load_verify_s03_runtime_module(name: str) -> Any:
    """Import scripts/verify_s03_runtime.py under a unique module name for tests."""

    spec = importlib.util.spec_from_file_location(
        name,
        REPO_ROOT_FOR_SCRIPT / "scripts" / "verify_s03_runtime.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _build_stage_entry(
    *,
    stage: ContentPipelineStage,
    status: ContentPipelineStageStatus,
    finished_offset_seconds: float | None = 0.5,
) -> ContentPipelineStageJournalRead:
    base = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    if finished_offset_seconds is not None:
        from datetime import timedelta

        started_at = base
        finished_at = base + timedelta(seconds=finished_offset_seconds)
    return ContentPipelineStageJournalRead(
        id=uuid.uuid7(),
        meme_file_id=uuid.uuid7(),
        stage=stage,
        status=status,
        attempt_count=1,
        last_event_id=uuid.uuid7(),
        normalized_reason=None,
        last_error_text=None,
        is_retryable=False,
        retry_after=None,
        started_at=started_at,
        finished_at=finished_at,
        created_at=base,
        updated_at=base,
    )


def _build_dual_synced_item() -> ContentPipelineItemDetail:
    meme_file_id = uuid.uuid7()
    meme_id = uuid.uuid7()
    now = datetime(2026, 4, 10, 12, 5, 0, tzinfo=UTC)
    return ContentPipelineItemDetail(
        meme_id=meme_id,
        meme_file_id=meme_file_id,
        current_stage=ContentPipelineStage.SYNC_MEILI,
        current_status=ContentPipelineStageStatus.SUCCEEDED,
        original_object_key=f"pipeline/originals/{meme_file_id}/original.png",
        web_video_object_key=None,
        last_event_id=uuid.uuid7(),
        normalized_reason=None,
        last_error_text=None,
        attempt_count=1,
        stages=(
            _build_stage_entry(
                stage=ContentPipelineStage.INGEST,
                status=ContentPipelineStageStatus.SUCCEEDED,
            ),
            _build_stage_entry(
                stage=ContentPipelineStage.TRANSCODE,
                status=ContentPipelineStageStatus.SUCCEEDED,
            ),
            _build_stage_entry(
                stage=ContentPipelineStage.OCR,
                status=ContentPipelineStageStatus.SUCCEEDED,
            ),
            _build_stage_entry(
                stage=ContentPipelineStage.EMBED,
                status=ContentPipelineStageStatus.SUCCEEDED,
            ),
            _build_stage_entry(
                stage=ContentPipelineStage.CLASSIFY,
                status=ContentPipelineStageStatus.SUCCEEDED,
            ),
            _build_stage_entry(
                stage=ContentPipelineStage.SYNC_QDRANT,
                status=ContentPipelineStageStatus.SUCCEEDED,
            ),
            _build_stage_entry(
                stage=ContentPipelineStage.SYNC_MEILI,
                status=ContentPipelineStageStatus.SUCCEEDED,
            ),
        ),
        ocr=ContentPipelineOCRDetail(
            engine="paddleocr",
            fallback_engine=None,
            fallback_used=False,
            low_confidence=False,
            confidence=0.91,
            language=ContentLanguage.EN,
            extracted_text="hello world",
            source_object_key="pipeline/derived/example/web.png",
            last_event_id=uuid.uuid7(),
        ),
        merge=ContentPipelineMergeDetail(),
        classification=ContentPipelineClassificationDetail(is_nsfw=False, classified=True),
        canonical=ContentPipelineCanonicalContext(
            canonical_meme_id=meme_id,
            canonical_primary_file_id=meme_file_id,
            is_canonical_primary=True,
            quality_score=0.77,
            ocr_text="hello world",
            language=ContentLanguage.EN,
        ),
        ready_event=ContentPipelineReadyEventSummary(
            event_id=uuid.uuid7(),
            classify_finished_at=now,
            meme_file_ready=True,
        ),
        sync_targets={
            SyncTargetKind.QDRANT: PerTargetSyncStatus(
                target=SyncTargetKind.QDRANT,
                status=SyncTargetStatus.SYNCED,
                last_event_id=uuid.uuid7(),
                last_success_at=now,
                last_attempt_at=now,
                attempt_count=1,
            ),
            SyncTargetKind.MEILISEARCH: PerTargetSyncStatus(
                target=SyncTargetKind.MEILISEARCH,
                status=SyncTargetStatus.SYNCED,
                last_event_id=uuid.uuid7(),
                last_success_at=now,
                last_attempt_at=now,
                attempt_count=1,
            ),
        },
    )


def _build_partially_searchable_item() -> ContentPipelineItemDetail:
    """Return a fixture item whose Meilisearch half is failed (partially searchable)."""

    item = _build_dual_synced_item()
    now = datetime(2026, 4, 10, 12, 5, 0, tzinfo=UTC)
    return item.model_copy(
        update={
            "sync_targets": {
                SyncTargetKind.QDRANT: item.sync_targets[SyncTargetKind.QDRANT],
                SyncTargetKind.MEILISEARCH: PerTargetSyncStatus(
                    target=SyncTargetKind.MEILISEARCH,
                    status=SyncTargetStatus.FAILED,
                    last_event_id=uuid.uuid7(),
                    normalized_reason="sync_meili_provider_blocked",
                    last_error_text="meilisearch down",
                    last_attempt_at=now,
                    attempt_count=2,
                ),
            },
        },
    )


def _build_passing_smoke(meme_file_id: uuid.UUID) -> SmokeProofResult:
    return SmokeProofResult(
        meme_file_id=meme_file_id,
        query=None,
        both_targets_searchable=True,
        targets=(
            SmokeProofTargetResult(
                target=SyncTargetKind.QDRANT,
                searchable=True,
                reason=None,
                latency_ms=12.0,
                matched_by="both",
            ),
            SmokeProofTargetResult(
                target=SyncTargetKind.MEILISEARCH,
                searchable=True,
                reason=None,
                latency_ms=8.0,
                matched_by="both",
            ),
        ),
        evaluated_at=datetime(2026, 4, 10, 12, 6, 0, tzinfo=UTC),
    )


def _build_stale_smoke(meme_file_id: uuid.UUID) -> SmokeProofResult:
    """Return a smoke proof that contradicts a snapshot that claims both-synced."""

    return SmokeProofResult(
        meme_file_id=meme_file_id,
        query=None,
        both_targets_searchable=False,
        targets=(
            SmokeProofTargetResult(
                target=SyncTargetKind.QDRANT,
                searchable=True,
                reason=None,
                latency_ms=12.0,
                matched_by="both",
            ),
            SmokeProofTargetResult(
                target=SyncTargetKind.MEILISEARCH,
                searchable=False,
                reason="document_not_found",
                latency_ms=None,
                matched_by=None,
            ),
        ),
        evaluated_at=datetime(2026, 4, 10, 12, 6, 0, tzinfo=UTC),
    )


def test_verify_s03_runtime_parse_args_round_trips_argparse_defaults_and_overrides() -> None:
    """Argparse must accept the documented flags and surface them as the expected types."""

    module = _load_verify_s03_runtime_module("verify_s03_runtime_args")

    args = module.parse_args(
        [
            "--dataset-root",
            "/tmp/dataset",
            "--api-base-url",
            "http://127.0.0.1:9000",
            "--artifacts-dir",
            "/tmp/artifacts",
            "--candidate-limit",
            "3",
            "--stage-timeout",
            "12",
            "--poll-interval",
            "0.25",
            "--api-timeout",
            "6",
            "--dry-run",
            "--run-id",
            "custom-run-id",
        ],
    )
    assert args.dataset_root == Path("/tmp/dataset")
    assert args.api_base_url == "http://127.0.0.1:9000"
    assert args.artifacts_dir == Path("/tmp/artifacts")
    assert args.candidate_limit == 3
    assert args.stage_timeout == 12.0
    assert args.poll_interval == 0.25
    assert args.api_timeout == 6.0
    assert args.dry_run is True
    assert args.run_id == "custom-run-id"


def test_verify_s03_runtime_parse_args_rejects_invalid_types() -> None:
    """Argparse must reject non-numeric values for typed flags."""

    module = _load_verify_s03_runtime_module("verify_s03_runtime_invalid_args")

    with pytest.raises(SystemExit):
        _ = module.parse_args(["--candidate-limit", "not-a-number"])


def test_verify_s03_runtime_dry_run_writes_artifacts_with_required_fields(tmp_path: Path) -> None:
    """``--dry-run`` must persist a machine-readable JSON plus Markdown summary."""

    module = _load_verify_s03_runtime_module("verify_s03_runtime_dry")

    exit_code = module.main(
        [
            "--dry-run",
            "--artifacts-dir",
            str(tmp_path),
            "--run-id",
            "dry-run-artifacts",
        ],
    )
    assert exit_code == 0

    run_dir = tmp_path / "dry-run-artifacts"
    report_json = run_dir / "report.json"
    report_md = run_dir / "report.md"
    assert report_json.is_file()
    assert report_md.is_file()

    payload = json.loads(report_json.read_text(encoding="utf-8"))
    expected_keys = {
        "run_id",
        "started_at",
        "finished_at",
        "bounded_item_count",
        "qdrant_synced_count",
        "meilisearch_synced_count",
        "both_synced_count",
        "partial_count",
        "blocked_by_qdrant_count",
        "blocked_by_meili_count",
        "smoke_pass_count",
        "stale_snapshot_ids",
        "item_reports",
        "errors",
    }
    assert expected_keys.issubset(payload.keys())
    assert payload["run_id"] == "dry-run-artifacts"
    assert payload["bounded_item_count"] == 0
    markdown = report_md.read_text(encoding="utf-8")
    assert "# Content pipeline S03 search-sync run dry-run-artifacts" in markdown
    assert "## Sync counts" in markdown


def test_verify_s03_runtime_returns_exit_2_when_stale_snapshots_present(tmp_path: Path) -> None:
    """A summary containing ``stale_snapshot_ids`` must resolve to exit code 2."""

    module = _load_verify_s03_runtime_module("verify_s03_runtime_stale")

    item = _build_dual_synced_item()
    stale_smoke = _build_stale_smoke(item.meme_file_id)
    summary = module.summarize_s03_run(
        run_id="stale-run",
        started_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 4, 10, 12, 1, 0, tzinfo=UTC),
        items=(item,),
        smoke_results={item.meme_file_id: stale_smoke},
    )
    assert summary.stale_snapshot_ids == (item.meme_file_id,)
    assert module._exit_code_from_summary(summary) == 2


def test_verify_s03_runtime_returns_exit_0_when_all_items_pass(tmp_path: Path) -> None:
    """A summary with N items all dual-synced and smoke-passing must exit 0."""

    module = _load_verify_s03_runtime_module("verify_s03_runtime_pass")

    item = _build_dual_synced_item()
    passing_smoke = _build_passing_smoke(item.meme_file_id)
    summary = module.summarize_s03_run(
        run_id="pass-run",
        started_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 4, 10, 12, 1, 0, tzinfo=UTC),
        items=(item,),
        smoke_results={item.meme_file_id: passing_smoke},
    )
    assert summary.both_synced_count == 1
    assert summary.smoke_pass_count == 1
    assert summary.stale_snapshot_ids == ()
    assert module._exit_code_from_summary(summary) == 0


def test_verify_s03_runtime_markdown_report_surfaces_blocked_and_replay_links(tmp_path: Path) -> None:
    """The Markdown rendering must surface per-target blocked items + replay drill-down links."""

    module = _load_verify_s03_runtime_module("verify_s03_runtime_markdown")

    item = _build_dual_synced_item()
    stale_smoke = _build_stale_smoke(item.meme_file_id)
    summary = module.summarize_s03_run(
        run_id="markdown-run",
        started_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 4, 10, 12, 1, 0, tzinfo=UTC),
        items=(item,),
        smoke_results={item.meme_file_id: stale_smoke},
    )

    # Exercise the renderer that the harness also uses to persist report.md.
    artifacts = module.build_artifacts(artifacts_dir=tmp_path, run_id="markdown-run")
    module.write_reports(artifacts, summary)

    markdown_text = (tmp_path / "markdown-run" / "report.md").read_text(encoding="utf-8")
    assert "Blocked items (per target)" in markdown_text
    assert "meilisearch: document_not_found" in markdown_text
    assert f"/api/v1/pipeline/items/{item.meme_file_id}/sync/meili/replay" in markdown_text


def test_verify_s03_runtime_dataset_missing_path_exits_1_with_actionable_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing dataset root must trigger the setup-failure exit code and a hint."""

    module = _load_verify_s03_runtime_module("verify_s03_runtime_missing_dataset")
    missing_root = tmp_path / "does-not-exist"

    exit_code = module.main(
        [
            "--dataset-root",
            str(missing_root),
            "--api-base-url",
            "http://127.0.0.1:9999",
            "--artifacts-dir",
            str(tmp_path / "artifacts"),
            "--run-id",
            "missing",
        ],
    )
    assert exit_code == 1

    captured = capsys.readouterr()
    assert "Dataset root" in captured.err
    assert "does not exist" in captured.err
    assert "Hint" in captured.err


def test_verify_s03_runtime_summarize_s03_run_tallies_partially_searchable_items() -> None:
    """Items whose outcome is ``partially_searchable`` must land in the partial counter."""

    module = _load_verify_s03_runtime_module("verify_s03_runtime_partial_tally")

    partial_item = _build_partially_searchable_item()
    summary = module.summarize_s03_run(
        run_id="partial",
        started_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 4, 10, 12, 1, 0, tzinfo=UTC),
        items=(partial_item,),
        smoke_results={},
    )
    assert summary.partial_count == 1
    assert summary.both_synced_count == 0
    assert summary.qdrant_synced_count == 1
    assert summary.meilisearch_synced_count == 0
