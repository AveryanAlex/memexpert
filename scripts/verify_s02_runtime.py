#!/usr/bin/env python3
"""Prove the S02 heavy-worker chain against the live local corpus.

This harness uploads a bounded set of dataset files through the operator
surface, polls the enriched ``/items/{id}/detail`` route until every item has
reached a terminal state, then writes a machine-readable JSON summary plus a
Markdown rendering under ``.artifacts/s02-runtime-smoke/<run-id>/``.

The script is deliberately designed so its core aggregation + serialization
functions are importable from unit tests, and so a ``--dry-run`` flag can
exercise the report pipeline against fixture item details without needing a
live stack.
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
from memexpert.models.enums import ContentPipelineStageStatus
from memexpert.schemas.content_pipeline import (
    ContentPipelineErrorResponse,
    ContentPipelineItemDetail,
    ContentPipelineRunSummary,
    ContentPipelineUploadRead,
)
from memexpert.services.content_pipeline_reporting import render_markdown_report, summarize_run

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATASET_ROOT: Final = Path("/home/alex/Documents/MemeDataset")
DEFAULT_ARTIFACTS_DIR: Final = REPO_ROOT / ".artifacts" / "s02-runtime-smoke"
DEFAULT_CANDIDATE_LIMIT: Final = 8
DEFAULT_STAGE_TIMEOUT_SECONDS: Final = 120.0
DEFAULT_POLL_INTERVAL_SECONDS: Final = 1.0
DEFAULT_API_TIMEOUT_SECONDS: Final = 20.0
SUPPORTED_EXTENSION_TO_MIME: Final[dict[str, str]] = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
TERMINAL_STATUSES: Final[frozenset[ContentPipelineStageStatus]] = frozenset(
    {
        ContentPipelineStageStatus.SUCCEEDED,
        ContentPipelineStageStatus.FAILED,
        ContentPipelineStageStatus.DUPLICATE,
    }
)


class SmokeError(RuntimeError):
    """Raised when the S02 proof harness cannot continue truthfully."""


@dataclass(frozen=True, slots=True)
class DatasetFile:
    """One deterministic dataset candidate collected for the corpus run."""

    path: Path
    content_type: str


@dataclass(frozen=True, slots=True)
class S02RunArtifacts:
    """Stable artifact paths written during the S02 proof run."""

    root: Path
    report_json: Path
    report_markdown: Path


class PipelineApiClient:
    """Typed HTTP client wrapper for the enriched operator pipeline surface."""

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
                f"GET /health returned unexpected status {response.status_code}: {response.text!r}"
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise SmokeError(f"GET /health returned non-JSON output: {exc}; body={response.text!r}") from exc
        if payload != {"status": "ok"}:
            raise SmokeError(f"GET /health returned unexpected payload: {payload!r}")

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
                    "source_id": f"s02-smoke-{run_id}",
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
            f"{exc}; body={response.text!r}"
        ) from exc

    if response.status_code != expected_status:
        try:
            error_payload = ContentPipelineErrorResponse.model_validate(payload)
        except ValidationError:
            rendered = json.dumps(payload, sort_keys=True) if isinstance(payload, dict | list) else repr(payload)
            raise SmokeError(
                f"{response.request.method} {response.request.url.path} failed with HTTP "
                f"{response.status_code} and a malformed payload: {rendered}"
            ) from None
        raise SmokeError(
            f"{response.request.method} {response.request.url.path} failed with HTTP "
            f"{response.status_code}: {error_payload.code.value} — {error_payload.detail}"
        )

    try:
        # ``model`` is a pydantic BaseModel subclass at the call sites; the generic
        # binding keeps the return type concrete for mypy-strict.
        return model.model_validate(payload)  # type: ignore[attr-defined, no-any-return]
    except ValidationError as exc:
        raise SmokeError(
            f"{response.request.method} {response.request.url.path} returned a malformed payload: {exc}"
        ) from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the S02 proof-harness configuration."""

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
        help="Maximum number of dataset files to drive through the heavy chain.",
    )
    parser.add_argument(
        "--stage-timeout",
        type=float,
        default=DEFAULT_STAGE_TIMEOUT_SECONDS,
        help="Seconds to wait for every uploaded item to reach a terminal state.",
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
    """Run the full S02 proof harness and return an exit code."""

    args = parse_args(argv)
    run_id = args.run_id or uuid.uuid7().hex[:12]
    artifacts = build_artifacts(artifacts_dir=args.artifacts_dir, run_id=run_id)

    if args.dry_run:
        summary = _run_dry(
            run_id=run_id,
            dataset_root=args.dataset_root,
            api_base_url=args.api_base_url,
        )
        write_reports(artifacts, summary)
        print(f"S02 dry-run artifacts: {artifacts.root}")
        return 0

    try:
        summary = _run_live(args, run_id=run_id)
    except SmokeError as exc:
        failure_summary = _build_failure_summary(
            run_id=run_id,
            dataset_root=args.dataset_root,
            api_base_url=args.api_base_url,
            errors=(str(exc),),
        )
        write_reports(artifacts, failure_summary)
        print(f"S02 run failed. Artifacts: {artifacts.root}", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1

    write_reports(artifacts, summary)
    print(f"S02 artifacts: {artifacts.root}")
    print(
        "S02 run complete: "
        f"ready={summary.stage_counts.ready_count}, "
        f"blocked={summary.stage_counts.blocked_count}, "
        f"merged={summary.stage_counts.merge_count}"
    )
    if summary.stage_counts.blocked_count > 0 or summary.errors:
        return 2
    return 0


def _run_live(args: argparse.Namespace, *, run_id: str) -> ContentPipelineRunSummary:
    settings = get_settings()
    started_at = datetime.now(tz=UTC)
    dataset_files = collect_dataset_files(
        args.dataset_root,
        allowed_mime_types=settings.pipeline_allowed_mime_types,
    )[: args.candidate_limit]
    if not dataset_files:
        raise SmokeError(
            f"Dataset root {args.dataset_root} does not contain any supported files",
        )

    operator_token = settings.pipeline_operator_token.get_secret_value()
    details: list[ContentPipelineItemDetail] = []
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
                detail = wait_for_terminal_state(
                    client=client,
                    meme_file_id=upload.meme_file_id,
                    stage_timeout_seconds=args.stage_timeout,
                    poll_interval_seconds=args.poll_interval,
                )
            except SmokeError as exc:
                errors.append(
                    f"Item {upload.meme_file_id} stalled before terminal state: {exc}"
                )
                # Capture the last observed state so the summary still contains
                # drill-down coordinates for the stuck item.
                try:
                    detail = client.get_item_detail(upload.meme_file_id)
                except SmokeError as inner_exc:
                    errors.append(
                        f"Item {upload.meme_file_id} final read failed: {inner_exc}"
                    )
                    continue
            details.append(detail)

    finished_at = datetime.now(tz=UTC)
    return summarize_run(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        dataset_root=str(args.dataset_root),
        api_base_url=args.api_base_url,
        items=details,
        errors=tuple(errors),
    )


def _run_dry(
    *,
    run_id: str,
    dataset_root: Path,
    api_base_url: str,
) -> ContentPipelineRunSummary:
    """Exercise the summary pipeline against an empty in-memory corpus.

    Dry-run mode is meant for smoke-testing the harness itself (argparse parsing,
    artifact writing, JSON shape) without a live stack. Tests can import this
    directly via :func:`main` or ``summarize_run`` when richer fixtures are
    needed.
    """

    now = datetime.now(tz=UTC)
    return summarize_run(
        run_id=run_id,
        started_at=now,
        finished_at=now,
        dataset_root=str(dataset_root),
        api_base_url=api_base_url,
        items=(),
        errors=("dry-run: no live stack was exercised",),
    )


def _build_failure_summary(
    *,
    run_id: str,
    dataset_root: Path,
    api_base_url: str,
    errors: tuple[str, ...],
) -> ContentPipelineRunSummary:
    now = datetime.now(tz=UTC)
    return summarize_run(
        run_id=run_id,
        started_at=now,
        finished_at=now,
        dataset_root=str(dataset_root),
        api_base_url=api_base_url,
        items=(),
        errors=errors,
    )


def collect_dataset_files(
    dataset_root: Path,
    *,
    allowed_mime_types: tuple[str, ...],
) -> list[DatasetFile]:
    """Return deterministic supported dataset files in sorted walk order."""

    if not dataset_root.exists():
        raise SmokeError(f"Dataset root {dataset_root} does not exist.")
    if not dataset_root.is_dir():
        raise SmokeError(f"Dataset root {dataset_root} is not a directory.")

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


def wait_for_terminal_state(
    *,
    client: PipelineApiClient,
    meme_file_id: uuid.UUID,
    stage_timeout_seconds: float,
    poll_interval_seconds: float,
) -> ContentPipelineItemDetail:
    """Poll the enriched detail route until the item reaches a terminal outcome."""

    deadline = time.monotonic() + stage_timeout_seconds
    last_detail: ContentPipelineItemDetail | None = None
    while time.monotonic() < deadline:
        detail = client.get_item_detail(meme_file_id)
        last_detail = detail
        if _is_terminal_detail(detail):
            return detail
        time.sleep(max(poll_interval_seconds, 0.1))

    snapshot = last_detail.model_dump(mode="json") if last_detail is not None else None
    raise SmokeError(
        f"Timed out waiting for pipeline item {meme_file_id} to reach a terminal state. "
        f"Last snapshot: {snapshot}"
    )


def _is_terminal_detail(detail: ContentPipelineItemDetail) -> bool:
    if detail.ready_event is not None:
        return True
    return detail.current_status in {
        ContentPipelineStageStatus.DUPLICATE,
        ContentPipelineStageStatus.FAILED,
    }


def build_artifacts(*, artifacts_dir: Path, run_id: str) -> S02RunArtifacts:
    """Create a per-run artifact directory and return its stable paths."""

    root = artifacts_dir / run_id
    root.mkdir(parents=True, exist_ok=True)
    return S02RunArtifacts(
        root=root,
        report_json=root / "report.json",
        report_markdown=root / "report.md",
    )


def write_reports(artifacts: S02RunArtifacts, summary: ContentPipelineRunSummary) -> None:
    """Persist the JSON + Markdown proof-run summary under the artifact directory."""

    payload = summary.model_dump(mode="json")
    artifacts.report_json.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    artifacts.report_markdown.write_text(render_markdown_report(summary), encoding="utf-8")


def iter_supported_extensions(allowed_mime_types: Iterable[str]) -> list[str]:
    """Return the sorted list of supported extensions for ops documentation reference."""

    allowed = set(allowed_mime_types)
    return sorted(
        extension for extension, mime in SUPPORTED_EXTENSION_TO_MIME.items() if mime in allowed
    )


if __name__ == "__main__":
    sys.exit(main())
