#!/usr/bin/env python3
"""Prove the S03 search-sync chain against the live local corpus.

This harness mirrors ``scripts/verify_s02_runtime.py`` in shape but adds two
new steps on top of the heavy-worker proof:

1. Each uploaded item is polled until BOTH sync targets (Qdrant and
   Meilisearch) reach a terminal ``synced`` or ``failed`` state.
2. Once polling terminates, the harness calls the
   ``POST /api/v1/pipeline/search/smoke`` route per item and embeds the
   per-item :class:`SmokeProofResult` into the JSON + Markdown artifact
   written under ``.artifacts/s03-runtime-smoke/<run-id>/``.

The aggregation + serialization helpers are importable from tests so the
``--dry-run`` path can exercise the JSON + Markdown pipeline with in-memory
fixtures — no docker, no running API, no dataset on disk.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import httpx
from pydantic import ValidationError

from memexpert.core.config import get_settings
from memexpert.models.enums import SyncTargetKind, SyncTargetStatus
from memexpert.schemas.content_pipeline import (
    ContentPipelineErrorResponse,
    ContentPipelineItemDetail,
    ContentPipelineS03RunItemReport,
    ContentPipelineS03RunSummary,
    ContentPipelineUploadRead,
    SmokeProofResult,
    SmokeProofTargetResult,
)
from memexpert.services.content_pipeline_reporting import (
    OUTCOME_BLOCKED,
    OUTCOME_PARTIALLY_SEARCHABLE,
    OUTCOME_READY,
    _classify_outcome,  # noqa: PLC2701 - reuse the canonical outcome classifier.
)
from memexpert.services.content_pipeline_smoke import render_s03_markdown_report

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATASET_ROOT: Final = Path("/home/alex/Documents/MemeDataset")
DEFAULT_ARTIFACTS_DIR: Final = REPO_ROOT / ".artifacts" / "s03-runtime-smoke"
DEFAULT_CANDIDATE_LIMIT: Final = 8
# S03 adds two sync stages after classify, so the stage timeout is longer
# than the S02 harness's default. Operators can lower this via the flag
# when running against a local stack with no provider latency.
DEFAULT_STAGE_TIMEOUT_SECONDS: Final = 240.0
DEFAULT_POLL_INTERVAL_SECONDS: Final = 1.0
DEFAULT_API_TIMEOUT_SECONDS: Final = 20.0
SUPPORTED_EXTENSION_TO_MIME: Final[dict[str, str]] = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_SYNC_TERMINAL_STATUSES: Final[frozenset[SyncTargetStatus]] = frozenset(
    {SyncTargetStatus.SYNCED, SyncTargetStatus.FAILED},
)


class SmokeError(RuntimeError):
    """Raised when the S03 proof harness cannot continue truthfully."""


class DatasetMissingError(SmokeError):
    """Raised when the dataset root does not exist or is unreadable.

    Kept as a distinct exception so :func:`main` can translate dataset
    problems into exit code ``1`` (setup failure) while true smoke
    failures return exit code ``2`` (runtime fail).
    """


@dataclass(frozen=True, slots=True)
class DatasetFile:
    """One deterministic dataset candidate collected for the corpus run."""

    path: Path
    content_type: str


@dataclass(frozen=True, slots=True)
class S03RunArtifacts:
    """Stable artifact paths written during the S03 proof run."""

    root: Path
    report_json: Path
    report_markdown: Path


class PipelineApiClient:
    """Typed HTTP client wrapper for the S03 operator pipeline surface."""

    def __init__(self, *, base_url: str, operator_token: str, timeout_seconds: float) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={"X-Memexpert-Operator-Token": operator_token},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PipelineApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def healthcheck(self) -> None:
        response = self._client.get("/health")
        if response.status_code != 200:
            raise SmokeError(
                f"GET /health returned unexpected status {response.status_code}: {response.text!r}",
            )

    def upload_file(
        self,
        dataset_file: DatasetFile,
        *,
        run_id: str,
        sequence: int,
    ) -> ContentPipelineUploadRead:
        with dataset_file.path.open("rb") as file_handle:
            response = self._client.post(
                "/api/v1/pipeline/uploads",
                data={
                    "source_platform": "telegram",
                    "source_id": f"s03-smoke-{run_id}",
                    "post_id": f"{run_id}-{sequence:04d}",
                    "views": str(sequence),
                },
                files={"file": (dataset_file.path.name, file_handle, dataset_file.content_type)},
            )
        return _validate_response(response, expected_status=201, model=ContentPipelineUploadRead)

    def get_item_detail(self, meme_file_id: uuid.UUID) -> ContentPipelineItemDetail:
        response = self._client.get(f"/api/v1/pipeline/items/{meme_file_id}/detail")
        return _validate_response(
            response,
            expected_status=200,
            model=ContentPipelineItemDetail,
        )

    def run_smoke_proof(self, meme_file_id: uuid.UUID) -> SmokeProofResult:
        response = self._client.post(
            "/api/v1/pipeline/search/smoke",
            json={"meme_file_id": str(meme_file_id)},
        )
        return _validate_response(response, expected_status=200, model=SmokeProofResult)


def _validate_response[ModelT](
    response: httpx.Response,
    *,
    expected_status: int,
    model: type[ModelT],
) -> ModelT:
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise SmokeError(
            f"{response.request.method} {response.request.url.path} returned non-JSON output: "
            f"{exc}; body={response.text!r}",
        ) from exc

    if response.status_code != expected_status:
        try:
            error_payload = ContentPipelineErrorResponse.model_validate(payload)
        except ValidationError:
            rendered = json.dumps(payload, sort_keys=True) if isinstance(payload, dict | list) else repr(payload)
            raise SmokeError(
                f"{response.request.method} {response.request.url.path} failed with HTTP "
                f"{response.status_code} and a malformed payload: {rendered}",
            ) from None
        raise SmokeError(
            f"{response.request.method} {response.request.url.path} failed with HTTP "
            f"{response.status_code}: {error_payload.code.value} — {error_payload.detail}",
        )

    try:
        return model.model_validate(payload)  # type: ignore[attr-defined, no-any-return]
    except ValidationError as exc:
        raise SmokeError(
            f"{response.request.method} {response.request.url.path} returned a malformed payload: {exc}",
        ) from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the S03 proof-harness configuration."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=DEFAULT_DATASET_ROOT,
        help="Path to the real local meme dataset (default: %(default)s).",
    )
    parser.add_argument(
        "--api-base-url",
        default="http://127.0.0.1:8000",
        help="Base URL of the running local API (default: %(default)s).",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=DEFAULT_ARTIFACTS_DIR,
        help="Directory to write per-run JSON + Markdown summaries under.",
    )
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=DEFAULT_CANDIDATE_LIMIT,
        help="Maximum number of dataset files to drive through the search-sync chain.",
    )
    parser.add_argument(
        "--stage-timeout",
        type=float,
        default=DEFAULT_STAGE_TIMEOUT_SECONDS,
        help="Seconds to wait for every uploaded item to reach dual-terminal sync state.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="Seconds between item-detail polls while waiting for terminal state.",
    )
    parser.add_argument(
        "--api-timeout",
        type=float,
        default=DEFAULT_API_TIMEOUT_SECONDS,
        help="Per-request HTTP timeout used by the operator API client.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Skip live HTTP calls and exercise the summary pipeline against "
            "an in-memory fixture. The artifact directory is still written."
        ),
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Override the per-run identifier (defaults to a fresh uuid7 prefix).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the full S03 proof harness and return an exit code.

    Exit codes:
        ``0`` — every expected condition held (all items dual-synced AND
               smoke-proof passed AND no stale snapshots).
        ``1`` — setup failure (dataset missing, API unreachable,
               configuration error that blocks the harness before work
               can begin).
        ``2`` — runtime failure (a bounded item did not dual-sync, or a
               smoke proof rejected one, or at least one snapshot was
               stale relative to its smoke proof).
    """

    args = parse_args(argv)
    run_id = args.run_id or uuid.uuid7().hex[:12]
    artifacts = build_artifacts(artifacts_dir=args.artifacts_dir, run_id=run_id)

    if args.dry_run:
        summary = _run_dry(run_id=run_id)
        write_reports(artifacts, summary)
        print(f"S03 dry-run artifacts: {artifacts.root}")
        return _exit_code_from_summary(summary)

    try:
        summary = _run_live(args, run_id=run_id)
    except DatasetMissingError as exc:
        failure_summary = _build_failure_summary(run_id=run_id, errors=(str(exc),))
        write_reports(artifacts, failure_summary)
        print(f"S03 setup failure. Artifacts: {artifacts.root}", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print(
            "Hint: pass --dataset-root to point at a populated local corpus.",
            file=sys.stderr,
        )
        return 1
    except SmokeError as exc:
        failure_summary = _build_failure_summary(run_id=run_id, errors=(str(exc),))
        write_reports(artifacts, failure_summary)
        print(f"S03 run failed. Artifacts: {artifacts.root}", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    write_reports(artifacts, summary)
    print(f"S03 artifacts: {artifacts.root}")
    print(
        "S03 run complete: "
        f"both_synced={summary.both_synced_count}/{summary.bounded_item_count}, "
        f"smoke_pass={summary.smoke_pass_count}, "
        f"stale={len(summary.stale_snapshot_ids)}",
    )
    return _exit_code_from_summary(summary)


def _exit_code_from_summary(summary: ContentPipelineS03RunSummary) -> int:
    """Translate a run summary into the documented tri-state exit code.

    A run with zero bounded items is neutral: every counter is zero, no
    smoke proof ran, and there is no "pass" to claim. The dry-run path
    exercises this with an empty corpus and expects exit code 0 when the
    stale snapshot list is empty, so we treat ``bounded_item_count == 0``
    as a no-op success unless the errors tuple carries real content.
    """

    if summary.bounded_item_count == 0:
        if summary.stale_snapshot_ids:
            return 2
        return 0
    if summary.stale_snapshot_ids:
        return 2
    if summary.both_synced_count != summary.bounded_item_count:
        return 2
    if summary.smoke_pass_count != summary.bounded_item_count:
        return 2
    return 0


def _run_live(args: argparse.Namespace, *, run_id: str) -> ContentPipelineS03RunSummary:
    settings = get_settings()
    started_at = datetime.now(tz=UTC)
    try:
        dataset_files = collect_dataset_files(
            args.dataset_root,
            allowed_mime_types=settings.pipeline_allowed_mime_types,
        )[: args.candidate_limit]
    except DatasetMissingError:
        raise
    if not dataset_files:
        raise DatasetMissingError(
            f"Dataset root {args.dataset_root} does not contain any supported files",
        )

    operator_token = settings.pipeline_operator_token.get_secret_value()
    details: list[ContentPipelineItemDetail] = []
    smoke_results: dict[uuid.UUID, SmokeProofResult] = {}
    errors: list[str] = []
    with PipelineApiClient(
        base_url=args.api_base_url,
        operator_token=operator_token,
        timeout_seconds=args.api_timeout,
    ) as client:
        client.healthcheck()
        for sequence, dataset_file in enumerate(dataset_files, start=1):
            print(f"[{sequence}/{len(dataset_files)}] Uploading {dataset_file.path}")
            upload = client.upload_file(dataset_file, run_id=run_id, sequence=sequence)
            try:
                detail = wait_for_dual_terminal_sync(
                    client=client,
                    meme_file_id=upload.meme_file_id,
                    stage_timeout_seconds=args.stage_timeout,
                    poll_interval_seconds=args.poll_interval,
                )
            except SmokeError as exc:
                errors.append(
                    f"Item {upload.meme_file_id} stalled before dual-terminal sync: {exc}",
                )
                try:
                    detail = client.get_item_detail(upload.meme_file_id)
                except SmokeError as inner_exc:
                    errors.append(
                        f"Item {upload.meme_file_id} final read failed: {inner_exc}",
                    )
                    continue
            details.append(detail)

            try:
                smoke_results[upload.meme_file_id] = client.run_smoke_proof(upload.meme_file_id)
            except SmokeError as exc:
                errors.append(f"Item {upload.meme_file_id} smoke proof failed: {exc}")

    finished_at = datetime.now(tz=UTC)
    return summarize_s03_run(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        items=details,
        smoke_results=smoke_results,
        errors=tuple(errors),
    )


def _run_dry(*, run_id: str) -> ContentPipelineS03RunSummary:
    """Exercise the aggregation pipeline against an empty in-memory corpus.

    An empty dry-run is the authoritative "harness smoke test" that
    confirms argparse parsing, artifact directory creation, JSON + Markdown
    serialization, and exit-code logic all agree without needing a running
    stack. Tests that need richer fixtures call :func:`summarize_s03_run`
    directly with fabricated item details + smoke results.
    """

    now = datetime.now(tz=UTC)
    return summarize_s03_run(
        run_id=run_id,
        started_at=now,
        finished_at=now,
        items=(),
        smoke_results={},
        errors=("dry-run: no live stack was exercised",),
    )


def _build_failure_summary(
    *,
    run_id: str,
    errors: tuple[str, ...],
) -> ContentPipelineS03RunSummary:
    now = datetime.now(tz=UTC)
    return summarize_s03_run(
        run_id=run_id,
        started_at=now,
        finished_at=now,
        items=(),
        smoke_results={},
        errors=errors,
    )


def summarize_s03_run(
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    items: Sequence[ContentPipelineItemDetail],
    smoke_results: dict[uuid.UUID, SmokeProofResult],
    errors: tuple[str, ...] = (),
) -> ContentPipelineS03RunSummary:
    """Derive the :class:`ContentPipelineS03RunSummary` from bounded item details.

    Pure aggregator so tests can call it without any HTTP / docker / file
    IO. ``items`` are the enriched detail reads the harness collected from
    the API; ``smoke_results`` maps one entry per item (keyed by
    ``meme_file_id``) when the smoke proof succeeded at least at the
    request level. Items without a smoke entry show ``smoke_result=None``.
    """

    item_reports = tuple(
        _build_s03_item_report(item, smoke_results.get(item.meme_file_id))
        for item in items
    )

    qdrant_synced = sum(
        1
        for report in item_reports
        if report.qdrant_status is SyncTargetStatus.SYNCED
    )
    meili_synced = sum(
        1
        for report in item_reports
        if report.meili_status is SyncTargetStatus.SYNCED
    )
    both_synced = sum(1 for report in item_reports if report.outcome == OUTCOME_READY)
    partial_count = sum(
        1
        for report in item_reports
        if report.outcome == OUTCOME_PARTIALLY_SEARCHABLE
    )
    blocked_by_qdrant = sum(
        1
        for report in item_reports
        if report.outcome == OUTCOME_BLOCKED
        and report.qdrant_status is SyncTargetStatus.FAILED
    )
    blocked_by_meili = sum(
        1
        for report in item_reports
        if report.outcome == OUTCOME_BLOCKED
        and report.meili_status is SyncTargetStatus.FAILED
    )
    smoke_pass = sum(
        1
        for report in item_reports
        if report.smoke_result is not None and report.smoke_result.both_targets_searchable
    )
    stale_ids = tuple(
        report.meme_file_id
        for report in item_reports
        if report.outcome == OUTCOME_READY
        and report.smoke_result is not None
        and not report.smoke_result.both_targets_searchable
    )

    return ContentPipelineS03RunSummary(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        bounded_item_count=len(items),
        qdrant_synced_count=qdrant_synced,
        meilisearch_synced_count=meili_synced,
        both_synced_count=both_synced,
        partial_count=partial_count,
        blocked_by_qdrant_count=blocked_by_qdrant,
        blocked_by_meili_count=blocked_by_meili,
        smoke_pass_count=smoke_pass,
        stale_snapshot_ids=stale_ids,
        item_reports=item_reports,
        errors=errors,
    )


def _build_s03_item_report(
    item: ContentPipelineItemDetail,
    smoke_result: SmokeProofResult | None,
) -> ContentPipelineS03RunItemReport:
    outcome = _classify_outcome(item)
    qdrant_status_entry = item.sync_targets.get(SyncTargetKind.QDRANT)
    meili_status_entry = item.sync_targets.get(SyncTargetKind.MEILISEARCH)
    failure_reason: str | None = None
    if smoke_result is None:
        failure_reason = "smoke_proof_missing"
    elif not smoke_result.both_targets_searchable:
        failure_reason = _first_smoke_failure_reason(smoke_result.targets)
    return ContentPipelineS03RunItemReport(
        meme_file_id=item.meme_file_id,
        outcome=outcome,
        qdrant_status=qdrant_status_entry.status if qdrant_status_entry is not None else None,
        meili_status=meili_status_entry.status if meili_status_entry is not None else None,
        smoke_result=smoke_result,
        failure_reason=failure_reason,
    )


def _first_smoke_failure_reason(
    targets: Sequence[SmokeProofTargetResult],
) -> str | None:
    """Return the first per-target ``reason`` string for a failed smoke proof.

    Used to populate :attr:`ContentPipelineS03RunItemReport.failure_reason`
    so the operator Markdown table shows at-a-glance why a target was not
    searchable without drilling into the nested :class:`SmokeProofResult`.
    """

    for target_result in targets:
        if not target_result.searchable and target_result.reason is not None:
            return target_result.reason
    return None


def collect_dataset_files(
    dataset_root: Path,
    *,
    allowed_mime_types: tuple[str, ...],
) -> list[DatasetFile]:
    """Return deterministic supported dataset files in sorted walk order."""

    if not dataset_root.exists():
        raise DatasetMissingError(f"Dataset root {dataset_root} does not exist.")
    if not dataset_root.is_dir():
        raise DatasetMissingError(f"Dataset root {dataset_root} is not a directory.")

    allowed_mime_type_set = frozenset(allowed_mime_types)
    dataset_files: list[DatasetFile] = []
    for candidate_path in sorted(dataset_root.rglob("*")):
        if not candidate_path.is_file():
            continue
        content_type = SUPPORTED_EXTENSION_TO_MIME.get(candidate_path.suffix.lower())
        if content_type is None or content_type not in allowed_mime_type_set:
            continue
        if candidate_path.stat().st_size <= 0:
            continue
        dataset_files.append(DatasetFile(path=candidate_path, content_type=content_type))
    return dataset_files


def wait_for_dual_terminal_sync(
    *,
    client: PipelineApiClient,
    meme_file_id: uuid.UUID,
    stage_timeout_seconds: float,
    poll_interval_seconds: float,
) -> ContentPipelineItemDetail:
    """Poll the detail route until BOTH sync targets reach a terminal status."""

    deadline = time.monotonic() + stage_timeout_seconds
    last_detail: ContentPipelineItemDetail | None = None
    while time.monotonic() < deadline:
        detail = client.get_item_detail(meme_file_id)
        last_detail = detail
        if _is_dual_terminal_detail(detail):
            return detail
        time.sleep(max(poll_interval_seconds, 0.1))

    snapshot = last_detail.model_dump(mode="json") if last_detail is not None else None
    raise SmokeError(
        f"Timed out waiting for pipeline item {meme_file_id} to reach dual-terminal sync. "
        f"Last snapshot: {snapshot}",
    )


def _is_dual_terminal_detail(detail: ContentPipelineItemDetail) -> bool:
    """Return ``True`` when both sync targets reached ``synced`` or ``failed``."""

    qdrant_status = detail.sync_targets.get(SyncTargetKind.QDRANT)
    meili_status = detail.sync_targets.get(SyncTargetKind.MEILISEARCH)
    if qdrant_status is None or meili_status is None:
        return False
    return (
        qdrant_status.status in _SYNC_TERMINAL_STATUSES
        and meili_status.status in _SYNC_TERMINAL_STATUSES
    )


def build_artifacts(*, artifacts_dir: Path, run_id: str) -> S03RunArtifacts:
    """Create a per-run artifact directory and return its stable paths."""

    root = artifacts_dir / run_id
    root.mkdir(parents=True, exist_ok=True)
    return S03RunArtifacts(
        root=root,
        report_json=root / "report.json",
        report_markdown=root / "report.md",
    )


def write_reports(
    artifacts: S03RunArtifacts,
    summary: ContentPipelineS03RunSummary,
) -> None:
    """Persist the JSON + Markdown proof-run summary under the artifact directory."""

    payload = summary.model_dump(mode="json")
    artifacts.report_json.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    artifacts.report_markdown.write_text(
        render_s03_markdown_report(summary),
        encoding="utf-8",
    )


def iter_supported_extensions(allowed_mime_types: Iterable[str]) -> list[str]:
    """Return the sorted list of supported extensions for ops documentation reference."""

    allowed = set(allowed_mime_types)
    return sorted(
        extension for extension, mime in SUPPORTED_EXTENSION_TO_MIME.items() if mime in allowed
    )


if __name__ == "__main__":
    sys.exit(main())
