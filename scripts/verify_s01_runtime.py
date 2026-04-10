#!/usr/bin/env python3
"""Prove the S01 operator content-pipeline flow against the local meme dataset."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, TextIO, cast
from urllib.parse import quote, unquote, urlparse

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

from memexpert.core.config import Settings, get_settings
from memexpert.core.storage import build_s3_client, get_pipeline_storage_settings
from memexpert.models.enums import ContentPipelineStage, ContentPipelineStageStatus
from memexpert.schemas.content_pipeline import (
    ContentPipelineErrorResponse,
    ContentPipelineItemFilter,
    ContentPipelineItemRead,
    ContentPipelineReplayAccepted,
    ContentPipelineUploadRead,
)

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_DATASET_ROOT: Final = Path("/home/alex/Documents/MemeDataset")
DEFAULT_COMPOSE_REQUIRED_SERVICES: Final[tuple[str, ...]] = (
    "postgres",
    "redis",
    "rabbitmq",
    "qdrant",
    "meilisearch",
    "minio",
    "imgproxy",
)
DEFAULT_API_TIMEOUT_SECONDS: Final = 20.0
DEFAULT_STAGE_TIMEOUT_SECONDS: Final = 20.0
DEFAULT_COMPOSE_TIMEOUT_SECONDS: Final = 60.0
DEFAULT_CANDIDATE_LIMIT: Final = 100
DEFAULT_RETRY_BACKOFF_SECONDS: Final = 30.0
SUPPORTED_EXTENSION_TO_MIME: Final[dict[str, str]] = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
NATIVE_PROCESS_PATTERNS: Final[tuple[str, ...]] = (
    "memexpert-api",
    "memexpert.api.main",
    "memexpert-workers",
    "memexpert.workers.main",
)


class SmokeError(RuntimeError):
    """Raised when the runtime smoke proof cannot continue truthfully."""


@dataclass(frozen=True, slots=True)
class DatasetFile:
    """One deterministic dataset file candidate for the smoke flow."""

    path: Path
    content_type: str


@dataclass(frozen=True, slots=True)
class ComposeServiceStatus:
    """Docker Compose runtime state parsed from `docker compose ps --format json`."""

    service: str
    state: str
    health: str | None


@dataclass(frozen=True, slots=True)
class RabbitManagementConfig:
    """HTTP management endpoint details derived from the RabbitMQ URL."""

    base_url: str
    vhost: str
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class UploadAttempt:
    """One upload attempt made while searching for a fresh non-duplicate file."""

    dataset_file: DatasetFile
    item: ContentPipelineUploadRead


@dataclass(frozen=True, slots=True)
class SmokeRunArtifacts:
    """Stable artifact paths written during the smoke run."""

    root: Path
    api_log: Path
    bootstrap_worker_log: Path
    failure_worker_log: Path
    replay_worker_log: Path
    alembic_log: Path
    report_json: Path


@dataclass(slots=True)
class ManagedProcess:
    """Small subprocess wrapper that captures stdout/stderr to a stable log file."""

    name: str
    command: tuple[str, ...]
    log_path: Path
    env_overrides: dict[str, str] = field(default_factory=dict)
    process: subprocess.Popen[str] | None = None
    _log_handle: TextIO | None = field(default=None, init=False, repr=False)

    def start(self) -> None:
        """Start the process and stream its combined output into the configured log."""

        if self.process is not None:
            raise RuntimeError(f"Process {self.name} is already running.")

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("w", encoding="utf-8")
        env = os.environ.copy()
        env.update(self.env_overrides)
        env.setdefault("PYTHONUNBUFFERED", "1")
        self.process = subprocess.Popen(
            self.command,
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def ensure_running(self) -> None:
        """Raise with log context if the process exited unexpectedly."""

        if self.process is None:
            raise RuntimeError(f"Process {self.name} was never started.")
        exit_code = self.process.poll()
        if exit_code is None:
            return
        raise SmokeError(
            f"{self.name} exited early with code {exit_code}. Log tail:\n{tail_log(self.log_path)}",
        )

    def stop(self, *, timeout_seconds: float = 10.0) -> None:
        """Terminate the process cleanly, then kill it if it does not exit in time."""

        if self.process is not None:
            exit_code = self.process.poll()
            if exit_code is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=timeout_seconds)
            self.process = None

        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None


class PipelineApiClient:
    """Typed HTTP client for the operator-only content-pipeline surface."""

    def __init__(self, *, base_url: str, operator_token: str, timeout_seconds: float) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={"X-Memexpert-Operator-Token": operator_token},
        )

    def close(self) -> None:
        """Dispose the underlying HTTP client."""

        self._client.close()

    def healthcheck(self) -> None:
        """Assert that the local API process responds with the documented health payload."""

        response = self._client.get("/health")
        payload = decode_json(response, context="GET /health")
        if response.status_code != 200 or payload != {"status": "ok"}:
            raise SmokeError(
                f"GET /health returned unexpected output: status={response.status_code}, payload={json.dumps(payload)}",
            )

    def upload_file(
        self,
        dataset_file: DatasetFile,
        *,
        source_id: str,
        post_id: str,
        views: int,
    ) -> ContentPipelineUploadRead:
        """Upload one dataset file through the real operator route."""

        with dataset_file.path.open("rb") as file_handle:
            response = self._client.post(
                "/api/v1/pipeline/uploads",
                data={
                    "source_platform": "telegram",
                    "source_id": source_id,
                    "post_id": post_id,
                    "views": str(views),
                },
                files={"file": (dataset_file.path.name, file_handle, dataset_file.content_type)},
            )

        return validate_model_response(
            response,
            expected_status=201,
            model_type=ContentPipelineUploadRead,
            context=f"POST /api/v1/pipeline/uploads for {dataset_file.path}",
        )

    def get_item(self, meme_file_id: uuid.UUID) -> ContentPipelineItemRead:
        """Read the durable inspect payload for one pipeline item."""

        response = self._client.get(f"/api/v1/pipeline/items/{meme_file_id}")
        return validate_model_response(
            response,
            expected_status=200,
            model_type=ContentPipelineItemRead,
            context=f"GET /api/v1/pipeline/items/{meme_file_id}",
        )

    def list_items(self, *, filter_by: ContentPipelineItemFilter) -> list[ContentPipelineItemRead]:
        """Read the filtered list surface and validate each returned item."""

        response = self._client.get("/api/v1/pipeline/items", params={"filter": filter_by.value})
        payload = decode_json(response, context=f"GET /api/v1/pipeline/items?filter={filter_by.value}")
        if response.status_code != 200:
            raise build_http_error(
                response=response,
                payload=payload,
                context=f"GET /api/v1/pipeline/items?filter={filter_by.value}",
            )
        if not isinstance(payload, list):
            raise SmokeError(
                "GET /api/v1/pipeline/items returned a malformed response: expected a JSON list of pipeline items.",
            )

        items: list[ContentPipelineItemRead] = []
        for index, item_payload in enumerate(payload, start=1):
            try:
                items.append(ContentPipelineItemRead.model_validate(item_payload))
            except ValidationError as exc:
                raise SmokeError(
                    "GET /api/v1/pipeline/items returned a malformed item at "
                    f"index {index}: {exc}",
                ) from exc
        return items

    def replay_item(
        self,
        meme_file_id: uuid.UUID,
        *,
        stage: ContentPipelineStage,
    ) -> ContentPipelineReplayAccepted:
        """Replay the failed stage through the real operator surface."""

        response = self._client.post(
            f"/api/v1/pipeline/items/{meme_file_id}/replay",
            json={"stage": stage.value},
        )
        return validate_model_response(
            response,
            expected_status=202,
            model_type=ContentPipelineReplayAccepted,
            context=f"POST /api/v1/pipeline/items/{meme_file_id}/replay",
        )


def parse_args() -> argparse.Namespace:
    """Parse the bounded smoke-run configuration."""

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
        help="Base URL for the local API process started by this script.",
    )
    parser.add_argument(
        "--fail-stage",
        choices=(ContentPipelineStage.TRANSCODE.value,),
        default=ContentPipelineStage.TRANSCODE.value,
        help="Pipeline stage to force into failure before replay. Only transcode is currently supported.",
    )
    parser.add_argument(
        "--compose-timeout",
        type=float,
        default=DEFAULT_COMPOSE_TIMEOUT_SECONDS,
        help="Seconds to wait for docker compose services to become healthy.",
    )
    parser.add_argument(
        "--api-timeout",
        type=float,
        default=DEFAULT_API_TIMEOUT_SECONDS,
        help="Seconds to wait for the native API process to become healthy.",
    )
    parser.add_argument(
        "--stage-timeout",
        type=float,
        default=DEFAULT_STAGE_TIMEOUT_SECONDS,
        help="Seconds to wait for failed and replayed stage movement.",
    )
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=DEFAULT_CANDIDATE_LIMIT,
        help="Maximum number of deterministic dataset files to try before declaring the dataset exhausted.",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=DEFAULT_RETRY_BACKOFF_SECONDS,
        help="Per-run RabbitMQ retry backoff used to keep the forced-failure message out of the way during replay.",
    )
    return parser.parse_args()


def main() -> None:
    """Run the complete upload → fail → inspect → duplicate → replay proof."""

    args = parse_args()
    run_id = uuid.uuid7().hex[:12]
    artifacts = build_artifacts(run_id)
    settings = get_settings()
    rabbit_management = build_rabbit_management_config(settings)
    api_env = build_runtime_env(run_id=run_id, retry_backoff_seconds=args.retry_backoff_seconds)

    dataset_files = collect_dataset_files(
        args.dataset_root,
        allowed_mime_types=settings.pipeline_allowed_mime_types,
    )
    parsed_api_base_url = validate_local_api_base_url(args.api_base_url)
    fail_stage = ContentPipelineStage(args.fail_stage)

    ensure_no_stale_native_processes()
    wait_for_compose_services(
        required_services=DEFAULT_COMPOSE_REQUIRED_SERVICES,
        timeout_seconds=args.compose_timeout,
    )
    ensure_pipeline_bucket_exists(settings)
    run_alembic_upgrade(artifacts.alembic_log)

    api_process = ManagedProcess(
        name="memexpert-api",
        command=(sys.executable, "-c", "from memexpert.api.main import main; main()"),
        env_overrides={
            **api_env,
            "APP_HOST": parsed_api_base_url.host,
            "APP_PORT": str(parsed_api_base_url.port),
        },
        log_path=artifacts.api_log,
    )
    bootstrap_worker = ManagedProcess(
        name="memexpert-workers-bootstrap",
        command=(sys.executable, "-c", "from memexpert.workers.main import main; main()"),
        env_overrides=api_env,
        log_path=artifacts.bootstrap_worker_log,
    )

    pipeline_client = PipelineApiClient(
        base_url=args.api_base_url,
        operator_token=settings.pipeline_operator_token.get_secret_value(),
        timeout_seconds=args.api_timeout,
    )

    skipped_duplicate_candidates: list[str] = []
    primary_attempt: UploadAttempt | None = None
    duplicate_attempt: UploadAttempt | None = None
    replay_result: ContentPipelineReplayAccepted | None = None
    failed_item: ContentPipelineItemRead | None = None
    succeeded_item: ContentPipelineItemRead | None = None
    failure_worker: ManagedProcess | None = None
    replay_worker: ManagedProcess | None = None

    try:
        api_process.start()
        wait_for_api_health(pipeline_client, api_process, timeout_seconds=args.api_timeout)

        bootstrap_worker.start()
        wait_for_worker_topology(
            process=bootstrap_worker,
            rabbit_management=rabbit_management,
            queue_name=api_env["PIPELINE_BROKER_TRANSCODE_QUEUE"],
            timeout_seconds=args.api_timeout,
        )
        bootstrap_worker.stop()

        primary_attempt = find_fresh_upload_candidate(
            client=pipeline_client,
            dataset_files=dataset_files,
            run_id=run_id,
            skipped_duplicate_candidates=skipped_duplicate_candidates,
            candidate_limit=args.candidate_limit,
        )

        assert_primary_upload_pending(primary_attempt.item, primary_attempt.dataset_file)

        failure_worker = ManagedProcess(
            name="memexpert-workers-failure",
            command=(sys.executable, "-c", "from memexpert.workers.main import main; main()"),
            env_overrides={
                **api_env,
                "PIPELINE_WORKER_FAIL_TRANSCODE_FOR_MEME_FILE_ID": str(primary_attempt.item.meme_file_id),
            },
            log_path=artifacts.failure_worker_log,
        )
        failure_worker.start()
        wait_for_worker_consumer(
            process=failure_worker,
            rabbit_management=rabbit_management,
            queue_name=api_env["PIPELINE_BROKER_TRANSCODE_QUEUE"],
            timeout_seconds=args.api_timeout,
        )

        failed_item = wait_for_item_state(
            client=pipeline_client,
            meme_file_id=primary_attempt.item.meme_file_id,
            timeout_seconds=args.stage_timeout,
            predicate=lambda item: (
                item.current_stage is ContentPipelineStage.TRANSCODE
                and item.current_status is ContentPipelineStageStatus.FAILED
                and item.normalized_reason == "forced_transcode_failure"
                and item.last_error_text
                == "Forced transcode failure requested by pipeline_worker_fail_transcode_for_meme_file_id."
                and item.attempt_count == 1
            ),
            description="forced transcode failure",
        )

        assert_item_present_in_filter(
            client=pipeline_client,
            filter_by=ContentPipelineItemFilter.FAILED,
            meme_file_id=primary_attempt.item.meme_file_id,
        )

        duplicate_upload_item = pipeline_client.upload_file(
            primary_attempt.dataset_file,
            source_id=f"runtime-smoke-duplicate-{run_id}",
            post_id=f"duplicate-{run_id}",
            views=2,
        )
        duplicate_attempt = UploadAttempt(dataset_file=primary_attempt.dataset_file, item=duplicate_upload_item)
        assert_duplicate_upload(duplicate_upload_item, primary_attempt.item.meme_file_id)
        assert_item_present_in_filter(
            client=pipeline_client,
            filter_by=ContentPipelineItemFilter.DUPLICATE,
            meme_file_id=duplicate_upload_item.meme_file_id,
        )

        failure_worker.stop()
        failure_worker = None

        replay_worker = ManagedProcess(
            name="memexpert-workers-replay",
            command=(sys.executable, "-c", "from memexpert.workers.main import main; main()"),
            env_overrides=api_env,
            log_path=artifacts.replay_worker_log,
        )
        replay_worker.start()
        wait_for_worker_consumer(
            process=replay_worker,
            rabbit_management=rabbit_management,
            queue_name=api_env["PIPELINE_BROKER_TRANSCODE_QUEUE"],
            timeout_seconds=args.api_timeout,
        )

        replay_result = pipeline_client.replay_item(primary_attempt.item.meme_file_id, stage=fail_stage)
        if replay_result.attempt != 2:
            raise SmokeError(
                "Replay did not reserve the second attempt as expected: "
                f"got attempt={replay_result.attempt}."
            )

        succeeded_item = wait_for_item_state(
            client=pipeline_client,
            meme_file_id=primary_attempt.item.meme_file_id,
            timeout_seconds=args.stage_timeout,
            predicate=lambda item: (
                item.current_stage is ContentPipelineStage.TRANSCODE
                and item.current_status is ContentPipelineStageStatus.SUCCEEDED
                and item.last_event_id == replay_result.replay_event_id
                and item.attempt_count == replay_result.attempt
                and item.web_video_object_key is not None
                and item.normalized_reason is None
                and item.last_error_text is None
            ),
            description="successful replayed transcode",
        )

        assert_item_absent_in_filter(
            client=pipeline_client,
            filter_by=ContentPipelineItemFilter.FAILED,
            meme_file_id=primary_attempt.item.meme_file_id,
        )

        write_report(
            artifacts=artifacts,
            run_id=run_id,
            api_base_url=args.api_base_url,
            dataset_root=args.dataset_root,
            broker_routing_prefix=api_env["PIPELINE_BROKER_ROUTING_KEY_PREFIX"],
            skipped_duplicate_candidates=skipped_duplicate_candidates,
            primary_attempt=primary_attempt,
            duplicate_attempt=duplicate_attempt,
            failed_item=failed_item,
            replay_result=replay_result,
            succeeded_item=succeeded_item,
        )

        print(f"Artifacts: {artifacts.root}")
        print(f"Primary file: {primary_attempt.dataset_file.path}")
        print(f"Primary item: {primary_attempt.item.meme_file_id}")
        if duplicate_attempt is not None:
            print(f"Duplicate item: {duplicate_attempt.item.meme_file_id}")
        print(f"Replay event: {replay_result.replay_event_id}")
        print("Smoke result: upload -> fail -> inspect -> duplicate -> replay succeeded.")
    except SmokeError as exc:
        write_report(
            artifacts=artifacts,
            run_id=run_id,
            api_base_url=args.api_base_url,
            dataset_root=args.dataset_root,
            broker_routing_prefix=api_env["PIPELINE_BROKER_ROUTING_KEY_PREFIX"],
            skipped_duplicate_candidates=skipped_duplicate_candidates,
            primary_attempt=primary_attempt,
            duplicate_attempt=duplicate_attempt,
            failed_item=failed_item,
            replay_result=replay_result,
            succeeded_item=succeeded_item,
            error=str(exc),
        )
        print(f"Artifacts: {artifacts.root}", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        if replay_worker is not None:
            replay_worker.stop()
        if failure_worker is not None:
            failure_worker.stop()
        bootstrap_worker.stop()
        api_process.stop()
        pipeline_client.close()


def build_artifacts(run_id: str) -> SmokeRunArtifacts:
    """Create a stable artifact directory for logs and the machine-readable report."""

    root = REPO_ROOT / ".artifacts" / "s01-runtime-smoke" / run_id
    root.mkdir(parents=True, exist_ok=True)
    return SmokeRunArtifacts(
        root=root,
        api_log=root / "api.log",
        bootstrap_worker_log=root / "worker-bootstrap.log",
        failure_worker_log=root / "worker-failure.log",
        replay_worker_log=root / "worker-replay.log",
        alembic_log=root / "alembic-upgrade.log",
        report_json=root / "report.json",
    )


def validate_local_api_base_url(api_base_url: str) -> httpx.URL:
    """Require a local HTTP API target because the script manages the native process itself."""

    parsed = httpx.URL(api_base_url)
    if parsed.scheme not in {"http", "https"}:
        raise SmokeError("--api-base-url must use http:// or https://.")
    if parsed.host not in {"127.0.0.1", "localhost"}:
        raise SmokeError(
            "--api-base-url must point at localhost because the smoke script starts the API process itself.",
        )
    if parsed.port is None:
        raise SmokeError("--api-base-url must include an explicit port.")
    return parsed


def collect_dataset_files(dataset_root: Path, *, allowed_mime_types: tuple[str, ...]) -> list[DatasetFile]:
    """Collect deterministic supported files directly from the external dataset root."""

    if not dataset_root.exists():
        raise SmokeError(
            f"Dataset root {dataset_root} does not exist. Point --dataset-root at /home/alex/Documents/MemeDataset.",
        )
    if not dataset_root.is_dir():
        raise SmokeError(f"Dataset root {dataset_root} is not a directory.")

    allowed_mime_type_set = frozenset(allowed_mime_types)
    dataset_files: list[DatasetFile] = []
    for candidate_path in sorted(dataset_root.rglob("*")):
        if candidate_path.is_dir():
            continue
        if not candidate_path.is_file():
            raise SmokeError(
                f"Dataset root {dataset_root} contains a non-file entry that the smoke script refuses to use: "
                f"{candidate_path}",
            )

        content_type = SUPPORTED_EXTENSION_TO_MIME.get(candidate_path.suffix.lower())
        if content_type is None or content_type not in allowed_mime_type_set:
            continue
        if candidate_path.stat().st_size <= 0:
            continue

        dataset_files.append(DatasetFile(path=candidate_path, content_type=content_type))

    if not dataset_files:
        allowed_list = ", ".join(sorted(allowed_mime_type_set))
        raise SmokeError(
            f"Dataset root {dataset_root} does not contain any supported non-empty files for {allowed_list}.",
        )
    return dataset_files


def ensure_no_stale_native_processes() -> None:
    """Refuse to run against stray native API or worker processes from earlier sessions."""

    result = subprocess.run(
        ["ps", "-eo", "pid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SmokeError(f"Failed to inspect local processes: {result.stderr.strip()}")

    stale_lines = [
        line.strip()
        for line in result.stdout.splitlines()
        if any(pattern in line for pattern in NATIVE_PROCESS_PATTERNS)
    ]
    if stale_lines:
        rendered_lines = "\n".join(f"- {line}" for line in stale_lines)
        raise SmokeError(
            "Found stale native memexpert API/worker processes. Stop them before running the smoke proof:\n"
            f"{rendered_lines}",
        )


def wait_for_compose_services(*, required_services: tuple[str, ...], timeout_seconds: float) -> None:
    """Wait until the required docker compose services are present and healthy."""

    deadline = time.monotonic() + timeout_seconds
    last_statuses: dict[str, ComposeServiceStatus] = {}

    while time.monotonic() < deadline:
        last_statuses = load_compose_statuses()
        if compose_services_ready(last_statuses, required_services=required_services):
            return
        time.sleep(1.0)

    raise SmokeError(
        "Timed out waiting for docker compose services. Current state:\n"
        f"{format_compose_statuses(last_statuses, required_services=required_services)}",
    )


def load_compose_statuses() -> dict[str, ComposeServiceStatus]:
    """Parse docker compose NDJSON status output into typed service records."""

    result = subprocess.run(
        ["docker", "compose", "ps", "--format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SmokeError(
            "Failed to inspect docker compose services. "
            f"stderr={result.stderr.strip() or '(empty)'}",
        )

    statuses: dict[str, ComposeServiceStatus] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SmokeError(f"docker compose ps returned malformed NDJSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SmokeError("docker compose ps returned a non-object JSON record.")

        service_name = payload.get("Service") or payload.get("Name")
        state_value = payload.get("State")
        health_value = payload.get("Health")
        if not isinstance(service_name, str) or not isinstance(state_value, str):
            raise SmokeError("docker compose ps omitted the Service or State fields.")

        statuses[service_name] = ComposeServiceStatus(
            service=service_name,
            state=state_value.strip().lower(),
            health=health_value.strip().lower() if isinstance(health_value, str) and health_value.strip() else None,
        )

    return statuses


def compose_services_ready(
    statuses: dict[str, ComposeServiceStatus],
    *,
    required_services: tuple[str, ...],
) -> bool:
    """Return whether every required compose service is running and healthy enough."""

    for service_name in required_services:
        status = statuses.get(service_name)
        if status is None:
            return False
        if status.state != "running":
            return False
        if status.health not in {None, "healthy"}:
            return False
    return True


def format_compose_statuses(
    statuses: dict[str, ComposeServiceStatus],
    *,
    required_services: tuple[str, ...],
) -> str:
    """Render a compact multiline compose status table for failures and reports."""

    lines: list[str] = []
    for service_name in required_services:
        status = statuses.get(service_name)
        if status is None:
            lines.append(f"- {service_name}: missing")
            continue
        health_text = status.health or "n/a"
        lines.append(f"- {service_name}: state={status.state}, health={health_text}")
    return "\n".join(lines)


def ensure_pipeline_bucket_exists(settings: Settings) -> None:
    """Create the configured S3 bucket on first run so the proof remains repeatable."""

    storage_settings = get_pipeline_storage_settings(settings)
    s3_client = build_s3_client(settings)

    try:
        _ = s3_client.head_bucket(Bucket=storage_settings.bucket)
        return
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code not in {"404", "NoSuchBucket", "NotFound"}:
            raise SmokeError(
                f"S3 bucket check failed for {storage_settings.bucket}: {exc}",
            ) from exc
    except BotoCoreError as exc:
        raise SmokeError(
            f"Unable to verify the S3-compatible bucket {storage_settings.bucket}: {exc}",
        ) from exc

    create_kwargs: dict[str, object] = {"Bucket": storage_settings.bucket}
    if storage_settings.region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": storage_settings.region}

    try:
        _ = s3_client.create_bucket(**create_kwargs)
        _ = s3_client.head_bucket(Bucket=storage_settings.bucket)
    except (ClientError, BotoCoreError) as exc:
        raise SmokeError(
            f"Failed to create the S3-compatible bucket {storage_settings.bucket}: {exc}",
        ) from exc


def run_alembic_upgrade(log_path: Path) -> None:
    """Apply the current Alembic head before the API touches PostgreSQL."""

    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    log_path.write_text(
        f"$ uv run alembic upgrade head\n\n{result.stdout}{result.stderr}",
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise SmokeError(
            "Alembic upgrade failed before the runtime smoke flow could start. "
            f"Log tail:\n{tail_log(log_path)}",
        )


def build_rabbit_management_config(settings: Settings) -> RabbitManagementConfig:
    """Derive RabbitMQ management API coordinates from the configured AMQP URL."""

    parsed = urlparse(settings.rabbitmq_url)
    if parsed.hostname is None:
        raise SmokeError("rabbitmq_url is missing a hostname, so the smoke script cannot inspect the worker queue.")

    base_scheme = "https" if parsed.scheme == "amqps" else "http"
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    vhost = unquote(parsed.path[1:]) if parsed.path and parsed.path != "/" else "/"
    if not username or not password:
        raise SmokeError("rabbitmq_url must include credentials so the smoke script can inspect queue readiness.")

    return RabbitManagementConfig(
        base_url=f"{base_scheme}://{parsed.hostname}:15672",
        vhost=vhost,
        username=username,
        password=password,
    )


def build_runtime_env(*, run_id: str, retry_backoff_seconds: float) -> dict[str, str]:
    """Build a unique per-run broker topology so stale retries cannot taint later smoke runs."""

    routing_prefix = f"pipeline.smoke.{run_id}"
    return {
        "PIPELINE_BROKER_EXCHANGE": f"memexpert.pipeline.smoke.{run_id}",
        "PIPELINE_BROKER_ROUTING_KEY_PREFIX": routing_prefix,
        "PIPELINE_BROKER_TRANSCODE_QUEUE": f"pipeline.smoke.{run_id}.transcode",
        "PIPELINE_BROKER_RETRY_EXCHANGE": f"memexpert.pipeline.smoke.{run_id}.retry",
        "PIPELINE_BROKER_RETRY_QUEUE": f"pipeline.smoke.{run_id}.retry",
        "PIPELINE_BROKER_DEAD_LETTER_EXCHANGE": f"memexpert.pipeline.smoke.{run_id}.dlx",
        "PIPELINE_BROKER_DEAD_LETTER_QUEUE": f"pipeline.smoke.{run_id}.dlq",
        "PIPELINE_BROKER_RETRY_BACKOFF_SECONDS": f"{retry_backoff_seconds:.1f}",
    }


def wait_for_api_health(
    client: PipelineApiClient,
    process: ManagedProcess,
    *,
    timeout_seconds: float,
) -> None:
    """Wait until the native API process responds to the shallow health route."""

    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        process.ensure_running()
        try:
            client.healthcheck()
            return
        except (SmokeError, httpx.HTTPError) as exc:
            last_error = str(exc)
            time.sleep(0.5)

    raise SmokeError(
        "Timed out waiting for the native API process to become healthy. "
        f"Last error: {last_error or '(none)'}\nLog tail:\n{tail_log(process.log_path)}",
    )


def wait_for_worker_topology(
    *,
    process: ManagedProcess,
    rabbit_management: RabbitManagementConfig,
    queue_name: str,
    timeout_seconds: float,
) -> None:
    """Wait until the worker has declared its queue topology through RabbitMQ management."""

    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        process.ensure_running()
        try:
            queue_payload = get_queue_payload(rabbit_management=rabbit_management, queue_name=queue_name)
            if queue_payload is not None:
                return
        except SmokeError as exc:
            last_error = str(exc)
        time.sleep(0.5)

    raise SmokeError(
        "Timed out waiting for the worker to declare RabbitMQ topology. "
        f"Last error: {last_error or '(none)'}\nLog tail:\n{tail_log(process.log_path)}",
    )


def wait_for_worker_consumer(
    *,
    process: ManagedProcess,
    rabbit_management: RabbitManagementConfig,
    queue_name: str,
    timeout_seconds: float,
) -> None:
    """Wait until a worker process is actively consuming the unique transcode queue."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        process.ensure_running()
        queue_payload = get_queue_payload(rabbit_management=rabbit_management, queue_name=queue_name)
        if queue_payload is not None:
            raw_consumers = queue_payload.get("consumers")
            if isinstance(raw_consumers, int) and raw_consumers >= 1:
                return
        time.sleep(0.5)

    raise SmokeError(
        "Timed out waiting for the worker consumer to attach to RabbitMQ. "
        f"Log tail:\n{tail_log(process.log_path)}",
    )


def get_queue_payload(
    *,
    rabbit_management: RabbitManagementConfig,
    queue_name: str,
) -> dict[str, object] | None:
    """Return one queue payload from RabbitMQ management, or None until it exists."""

    encoded_vhost = quote(rabbit_management.vhost, safe="")
    encoded_queue = quote(queue_name, safe="")
    url = f"{rabbit_management.base_url}/api/queues/{encoded_vhost}/{encoded_queue}"

    try:
        response = httpx.get(
            url,
            auth=(rabbit_management.username, rabbit_management.password),
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        raise SmokeError(f"Unable to reach RabbitMQ management while inspecting {queue_name}: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise SmokeError(
            f"RabbitMQ management returned {response.status_code} while inspecting queue {queue_name}: {response.text}",
        )

    payload = decode_json(response, context=f"RabbitMQ management queue inspect for {queue_name}")
    if not isinstance(payload, dict):
        raise SmokeError(f"RabbitMQ management returned malformed queue JSON for {queue_name}.")
    return cast("dict[str, object]", payload)


def find_fresh_upload_candidate(
    *,
    client: PipelineApiClient,
    dataset_files: list[DatasetFile],
    run_id: str,
    skipped_duplicate_candidates: list[str],
    candidate_limit: int,
) -> UploadAttempt:
    """Upload deterministic files until one produces a real pending transcode item."""

    if candidate_limit <= 0:
        raise SmokeError("--candidate-limit must be greater than zero.")

    for index, dataset_file in enumerate(dataset_files[:candidate_limit], start=1):
        item = client.upload_file(
            dataset_file,
            source_id=f"runtime-smoke-source-{run_id}-{index:03d}",
            post_id=f"runtime-smoke-post-{run_id}-{index:03d}",
            views=index,
        )
        if item.current_status is ContentPipelineStageStatus.DUPLICATE:
            skipped_duplicate_candidates.append(str(dataset_file.path))
            continue
        return UploadAttempt(dataset_file=dataset_file, item=item)

    attempted_paths = "\n".join(f"- {dataset_file.path}" for dataset_file in dataset_files[:candidate_limit])
    raise SmokeError(
        "Unable to find a fresh non-duplicate dataset file within the configured candidate limit. "
        "The smoke script only trusts a file that creates a new pending transcode item. "
        f"Attempted files:\n{attempted_paths}",
    )


def assert_primary_upload_pending(item: ContentPipelineUploadRead, dataset_file: DatasetFile) -> None:
    """Require a real upload to land in the pending transcode state before the worker runs."""

    if item.current_stage is not ContentPipelineStage.TRANSCODE:
        raise SmokeError(
            f"Fresh upload from {dataset_file.path} did not advance to transcode: {summarize_item(item)}",
        )
    if item.current_status is not ContentPipelineStageStatus.PENDING:
        raise SmokeError(
            f"Fresh upload from {dataset_file.path} is not pending for transcode: {summarize_item(item)}",
        )
    if not item.original_object_key.endswith(dataset_file.path.suffix.lower()):
        raise SmokeError(
            "Fresh upload returned an unexpected original object key: "
            f"file={dataset_file.path}, object_key={item.original_object_key}",
        )


def wait_for_item_state(
    *,
    client: PipelineApiClient,
    meme_file_id: uuid.UUID,
    timeout_seconds: float,
    predicate: Callable[[ContentPipelineItemRead], bool],
    description: str,
) -> ContentPipelineItemRead:
    """Poll the item-detail surface until the provided predicate matches."""

    deadline = time.monotonic() + timeout_seconds
    last_item: ContentPipelineItemRead | None = None
    while time.monotonic() < deadline:
        last_item = client.get_item(meme_file_id)
        if predicate(last_item):
            return last_item
        time.sleep(0.5)

    rendered_last_item = summarize_item(last_item) if last_item is not None else "<never observed>"
    raise SmokeError(
        "Timed out waiting for "
        f"{description} on pipeline item {meme_file_id}. "
        f"Last item snapshot: {rendered_last_item}",
    )


def assert_item_present_in_filter(
    *,
    client: PipelineApiClient,
    filter_by: ContentPipelineItemFilter,
    meme_file_id: uuid.UUID,
) -> None:
    """Require that the expected item appears in the named list surface."""

    items = client.list_items(filter_by=filter_by)
    if any(item.meme_file_id == meme_file_id for item in items):
        return
    raise SmokeError(
        f"Pipeline item {meme_file_id} was not visible in filter={filter_by.value}. "
        f"Observed item ids: {[str(item.meme_file_id) for item in items]}",
    )


def assert_item_absent_in_filter(
    *,
    client: PipelineApiClient,
    filter_by: ContentPipelineItemFilter,
    meme_file_id: uuid.UUID,
) -> None:
    """Require that the expected item no longer appears in the named list surface."""

    items = client.list_items(filter_by=filter_by)
    if not any(item.meme_file_id == meme_file_id for item in items):
        return
    raise SmokeError(
        f"Pipeline item {meme_file_id} still appears in filter={filter_by.value} after replay: "
        f"{[str(item.meme_file_id) for item in items]}",
    )


def assert_duplicate_upload(duplicate_item: ContentPipelineUploadRead, primary_meme_file_id: uuid.UUID) -> None:
    """Require that re-uploading the same dataset file returns the supported duplicate contract."""

    if duplicate_item.current_stage is not ContentPipelineStage.INGEST:
        raise SmokeError(f"Duplicate upload did not remain on ingest: {summarize_item(duplicate_item)}")
    if duplicate_item.current_status is not ContentPipelineStageStatus.DUPLICATE:
        raise SmokeError(f"Duplicate upload did not report duplicate status: {summarize_item(duplicate_item)}")
    if duplicate_item.normalized_reason != "duplicate_perceptual_hash":
        raise SmokeError(
            "Duplicate upload reported the wrong normalized reason: "
            f"{summarize_item(duplicate_item)}",
        )
    if duplicate_item.last_error_text is None or str(primary_meme_file_id) not in duplicate_item.last_error_text:
        raise SmokeError(
            "Duplicate upload did not explain which original item it matched: "
            f"{summarize_item(duplicate_item)}",
        )
    if len(duplicate_item.stages) != 1 or duplicate_item.stages[0].status is not ContentPipelineStageStatus.DUPLICATE:
        raise SmokeError(
            "Duplicate upload returned an unexpected stage journal payload: "
            f"{summarize_item(duplicate_item)}",
        )


def decode_json(response: httpx.Response, *, context: str) -> object:
    """Decode JSON and raise a contextual smoke error on malformed responses."""

    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise SmokeError(f"{context} returned non-JSON output: {exc}; body={response.text!r}") from exc


def validate_model_response[ModelT: BaseModel](
    response: httpx.Response,
    *,
    expected_status: int,
    model_type: type[ModelT],
    context: str,
) -> ModelT:
    """Decode JSON, validate the expected status code, and parse the response model."""

    payload = decode_json(response, context=context)
    if response.status_code != expected_status:
        raise build_http_error(response=response, payload=payload, context=context)
    try:
        return model_type.model_validate(payload)
    except ValidationError as exc:
        raise SmokeError(f"{context} returned a malformed payload: {exc}") from exc


def build_http_error(
    *,
    response: httpx.Response,
    payload: object,
    context: str,
) -> SmokeError:
    """Render unexpected HTTP responses with the documented error payload when available."""

    try:
        error_payload = ContentPipelineErrorResponse.model_validate(payload)
    except ValidationError:
        rendered_payload = json.dumps(payload, sort_keys=True) if isinstance(payload, (dict, list)) else repr(payload)
        return SmokeError(
            f"{context} failed with HTTP {response.status_code} and a malformed payload: {rendered_payload}",
        )

    return SmokeError(
        f"{context} failed with HTTP {response.status_code}: {error_payload.code.value} — {error_payload.detail}",
    )


def summarize_item(item: ContentPipelineItemRead | ContentPipelineUploadRead | None) -> str:
    """Render a compact JSON snapshot of a pipeline item for diagnostics."""

    if item is None:
        return "null"
    return json.dumps(item.model_dump(mode="json"), sort_keys=True)


def tail_log(log_path: Path, *, lines: int = 40) -> str:
    """Return the last few log lines without failing if the file is absent."""

    if not log_path.exists():
        return f"(missing log file: {log_path})"
    text = log_path.read_text(encoding="utf-8", errors="replace")
    split_lines = text.splitlines()
    if not split_lines:
        return "(log file is empty)"
    return "\n".join(split_lines[-lines:])


def write_report(
    *,
    artifacts: SmokeRunArtifacts,
    run_id: str,
    api_base_url: str,
    dataset_root: Path,
    broker_routing_prefix: str,
    skipped_duplicate_candidates: list[str],
    primary_attempt: UploadAttempt | None,
    duplicate_attempt: UploadAttempt | None,
    failed_item: ContentPipelineItemRead | None,
    replay_result: ContentPipelineReplayAccepted | None,
    succeeded_item: ContentPipelineItemRead | None,
    error: str | None = None,
) -> None:
    """Persist a machine-readable summary for later inspection or task evidence."""

    payload: dict[str, object] = {
        "run_id": run_id,
        "api_base_url": api_base_url,
        "dataset_root": str(dataset_root),
        "broker_routing_prefix": broker_routing_prefix,
        "skipped_duplicate_candidates": skipped_duplicate_candidates,
        "artifacts": {
            "api_log": str(artifacts.api_log),
            "bootstrap_worker_log": str(artifacts.bootstrap_worker_log),
            "failure_worker_log": str(artifacts.failure_worker_log),
            "replay_worker_log": str(artifacts.replay_worker_log),
            "alembic_log": str(artifacts.alembic_log),
        },
        "primary_attempt": {
            "dataset_file": str(primary_attempt.dataset_file.path),
            "item": primary_attempt.item.model_dump(mode="json"),
        }
        if primary_attempt is not None
        else None,
        "duplicate_attempt": {
            "dataset_file": str(duplicate_attempt.dataset_file.path),
            "item": duplicate_attempt.item.model_dump(mode="json"),
        }
        if duplicate_attempt is not None
        else None,
        "failed_item": failed_item.model_dump(mode="json") if failed_item is not None else None,
        "replay_result": replay_result.model_dump(mode="json") if replay_result is not None else None,
        "succeeded_item": succeeded_item.model_dump(mode="json") if succeeded_item is not None else None,
        "error": error,
    }
    artifacts.report_json.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
