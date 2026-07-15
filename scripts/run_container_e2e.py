#!/usr/bin/env python3
"""Run the parallel-safe containerized PRD E2E suite."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.e2e.yml"
ARTIFACT_ROOT = ROOT / ".artifacts" / "e2e"
PROJECT_PREFIX: Final = "memexpert-e2e"
PROJECT_CLAIM_SUFFIX: Final = "claim"
PROJECT_CLAIM_LABEL: Final = "com.memexpert.e2e.claim"
PROJECT_CLAIM_PROJECT_LABEL: Final = "com.memexpert.e2e.project"
IMAGE_ENV_DEFAULTS: Final = {
    "MEMEXPERT_MAIN_IMAGE": "memexpert-main:e2e-{run_id}",
    "MEMEXPERT_WORKER_IMAGE": "memexpert-worker:e2e-{run_id}",
    "MEMEXPERT_FRONTEND_IMAGE": "memexpert-frontend:e2e-{run_id}",
    "MEMEXPERT_E2E_RUNNER_IMAGE": "memexpert-e2e-runner:e2e-{run_id}",
}
TRUTHY_VALUES: Final = {"1", "true", "yes", "on"}
WAITED_LONG_LIVED_SERVICES: Final = ("api", "frontend")
EXPLICITLY_GATED_LONG_LIVED_SERVICES: Final = (
    "worker-media",
    "worker-ocr",
    "worker-enrichment",
    "worker-sync",
    "worker-telegram",
)
LOG_SERVICES: Final = (
    "postgres",
    "redis",
    "rabbitmq",
    "qdrant",
    "meilisearch",
    "minio",
    "minio-init",
    "imgproxy",
    "migrate",
    "api",
    "worker-media",
    "worker-ocr",
    "worker-enrichment",
    "worker-sync",
    "worker-telegram",
    "frontend",
    "seed",
    "e2e-runner",
)
RUN_ID_RE = re.compile(r"[^a-z0-9-]+")
MAX_RUN_ID_LENGTH: Final = 48
RUN_ID_HASH_LENGTH: Final = 12
PIPELINE_CONSUMER_WAIT_TIMEOUT_SECONDS: Final = 90.0
PIPELINE_CONSUMER_POLL_INTERVAL_SECONDS: Final = 1.0
RABBITMQCTL_TIMEOUT_SECONDS: Final = 10.0
PIPELINE_CONSUMER_ROLE_ARGUMENT: Final = "x-memexpert-worker-role"
REQUIRED_PIPELINE_CONSUMER_QUEUES: Final = frozenset(
    {
        "pipeline.media_inspect",
        "pipeline.transcode",
        "pipeline.ocr",
        "pipeline.embed",
        "pipeline.classify",
        "pipeline.sync_qdrant",
        "pipeline.sync_meili",
    },
)
EXPECTED_PIPELINE_CONSUMER_ROLES: Final = {
    "pipeline.media_inspect": "media",
    "pipeline.transcode": "media",
    "pipeline.ocr": "ocr",
    "pipeline.embed": "enrichment",
    "pipeline.classify": "enrichment",
    "pipeline.sync_qdrant": "sync",
    "pipeline.sync_meili": "sync",
}


class E2ERunCollisionError(RuntimeError):
    """Raised when a requested artifact directory or Compose project is already owned."""


class RabbitMQConsumerInspectionError(RuntimeError):
    """Raised for a transient RabbitMQ CLI or consumer JSON inspection failure."""


class PipelineConsumerReadinessError(RuntimeError):
    """Raised when required pipeline consumers do not register before the deadline."""


class PipelineConsumerOwnershipError(RuntimeError):
    """Raised when a pipeline queue is consumed by the wrong worker role."""


@dataclass(frozen=True, slots=True)
class CommandFailure:
    """Serializable details for a best-effort command that did not succeed."""

    command: tuple[str, ...]
    message: str
    returncode: int | None = None
    output_path: str | None = None

    def as_metadata(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "command": list(self.command),
            "message": self.message,
            "returncode": self.returncode,
        }
        if self.output_path is not None:
            payload["output_path"] = self.output_path
        return payload


@dataclass(frozen=True, slots=True)
class ProjectClaim:
    """An atomically-created Docker network that owns one Compose project name."""

    network_name: str
    network_id: str


def main() -> int:
    run_id = sanitize_run_id(os.environ.get("E2E_RUN_ID") or uuid.uuid4().hex[:12])
    project_name = f"{PROJECT_PREFIX}-{run_id}"
    try:
        artifact_dir = create_artifact_dir(ARTIFACT_ROOT, run_id=run_id)
    except E2ERunCollisionError as exc:
        print(f"Container PRD E2E refused to start: {exc}", file=sys.stderr, flush=True)
        return 2

    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = project_name
    env["E2E_RUN_ID"] = run_id
    env["E2E_ARTIFACT_DIR"] = str(artifact_dir)
    defaulted_images = apply_default_image_tags(env, run_id=run_id)
    skip_image_build = is_truthy(env.get("E2E_SKIP_IMAGE_BUILD"))

    compose = ["docker", "compose", "-p", project_name, "-f", str(COMPOSE_FILE)]
    started_at = datetime.now(tz=UTC)
    write_metadata(
        artifact_dir,
        {
            "run_id": run_id,
            "project_name": project_name,
            "compose_file": str(COMPOSE_FILE),
            "artifact_dir": str(artifact_dir),
            "images": {key: env[key] for key in IMAGE_ENV_DEFAULTS},
            "skip_image_build": skip_image_build,
            "started_at": started_at.isoformat(),
        },
    )

    exit_code = 0
    failure: dict[str, object] | None = None
    project_claim: ProjectClaim | None = None
    project_claim_released = False
    project_available = False
    artifact_capture_failures: list[CommandFailure] = []
    cleanup_failures: list[CommandFailure] = []
    keep_stack = is_truthy(env.get("E2E_KEEP_STACK"))
    try:
        print(f"Starting {project_name}; artifacts: {artifact_dir}", flush=True)
        project_claim = claim_project(project_name, env=env)
        assert_project_available(project_name, env=env)
        project_available = True
        if skip_image_build:
            assert_images_exist([env[key] for key in IMAGE_ENV_DEFAULTS], env=env)
        run_checked(
            compose_up_command(
                compose,
                skip_image_build=skip_image_build,
                wait=True,
                services=WAITED_LONG_LIVED_SERVICES,
            ),
            env=env,
        )
        run_checked(
            compose_up_command(
                compose,
                skip_image_build=skip_image_build,
                no_deps=True,
                services=EXPLICITLY_GATED_LONG_LIVED_SERVICES,
            ),
            env=env,
        )
        wait_for_pipeline_consumers(compose, env=env)
        wait_for_pipeline_consumer_ownership(compose, env=env)
        if not skip_image_build:
            run_checked([*compose, "build", "e2e-runner"], env=env)
        run_checked([*compose, "run", "--rm", "--no-deps", "seed"], env=env)
        run_checked([*compose, "run", "--rm", "--no-deps", "e2e-runner"], env=env)
    except E2ERunCollisionError as exc:
        exit_code = 2
        failure = {"kind": "collision", "message": str(exc)}
        print(f"Container PRD E2E refused to share existing state: {exc}", file=sys.stderr, flush=True)
    except subprocess.CalledProcessError as exc:
        exit_code = exc.returncode or 1
        failure = {
            "kind": "command",
            "message": str(exc),
            "command": _command_as_list(exc.cmd),
            "returncode": exc.returncode,
        }
        print(f"Container PRD E2E failed with exit code {exit_code}.", file=sys.stderr, flush=True)
    except Exception as exc:
        exit_code = 1
        failure = {
            "kind": "python_exception",
            "type": type(exc).__name__,
            "message": str(exc),
        }
        print("Container PRD E2E failed with an unexpected Python exception.", file=sys.stderr, flush=True)
        traceback.print_exc()
    finally:
        if project_available:
            try:
                artifact_capture_failures.extend(collect_artifacts(compose, env=env, artifact_dir=artifact_dir))
            except Exception as exc:  # pragma: no cover - defensive containment around best-effort diagnostics
                message = f"Artifact collection raised {type(exc).__name__}: {exc}"
                print(message, file=sys.stderr, flush=True)
                artifact_capture_failures.append(CommandFailure(command=(), message=message))

            if keep_stack:
                print(f"Keeping Compose stack {project_name} because E2E_KEEP_STACK is enabled.", flush=True)
            else:
                cleanup_failure = run_best_effort([*compose, "down", "-v", "--remove-orphans"], env=env)
                if cleanup_failure is not None:
                    cleanup_failures.append(cleanup_failure)
                try:
                    cleanup_failures.extend(remove_defaulted_images(defaulted_images, env=env))
                except Exception as exc:  # pragma: no cover - defensive containment around best-effort cleanup
                    message = f"Per-run image cleanup raised {type(exc).__name__}: {exc}"
                    print(message, file=sys.stderr, flush=True)
                    cleanup_failures.append(CommandFailure(command=("docker", "image", "rm"), message=message))
                if not cleanup_failures and project_claim is not None:
                    try:
                        claim_release_failure = release_project_claim(project_claim, env=env)
                    except Exception as exc:  # pragma: no cover - defensive containment around best-effort cleanup
                        message = f"Project claim release raised {type(exc).__name__}: {exc}"
                        print(message, file=sys.stderr, flush=True)
                        claim_release_failure = CommandFailure(
                            command=("docker", "network", "rm", project_claim.network_name),
                            message=message,
                        )
                    if claim_release_failure is None:
                        project_claim_released = True
                    else:
                        cleanup_failures.append(claim_release_failure)

        if artifact_capture_failures or cleanup_failures:
            print(
                "Container PRD E2E recorded harness failures: "
                f"artifact_capture={len(artifact_capture_failures)}, cleanup={len(cleanup_failures)}.",
                file=sys.stderr,
                flush=True,
            )

        final_metadata: dict[str, object] = {
            "finished_at": datetime.now(tz=UTC).isoformat(),
            "exit_code": exit_code,
            "keep_stack": keep_stack,
            "project_available_at_start": project_available,
            "project_claim": (
                {
                    "network_name": project_claim.network_name,
                    "network_id": project_claim.network_id,
                    "released": project_claim_released,
                    "retained_reason": _claim_retained_reason(
                        released=project_claim_released,
                        keep_stack=keep_stack,
                        project_available=project_available,
                        cleanup_failures=cleanup_failures,
                    ),
                }
                if project_claim is not None
                else None
            ),
            "artifact_capture_failures": [item.as_metadata() for item in artifact_capture_failures],
            "cleanup_failures": [item.as_metadata() for item in cleanup_failures],
        }
        if failure is not None:
            final_metadata["failure"] = failure
        try:
            append_metadata(artifact_dir, final_metadata)
        except Exception as exc:  # pragma: no cover - a broken artifact filesystem cannot persist its own failure
            exit_code = exit_code or 1
            print(
                f"Unable to finalize E2E run metadata: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    return exit_code


def sanitize_run_id(raw: str) -> str:
    normalized = RUN_ID_RE.sub("-", raw.strip().lower()).strip("-")
    if not normalized:
        normalized = uuid.uuid4().hex[:12]
    if len(normalized) <= MAX_RUN_ID_LENGTH:
        return normalized
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:RUN_ID_HASH_LENGTH]
    prefix_length = MAX_RUN_ID_LENGTH - RUN_ID_HASH_LENGTH - 1
    prefix = normalized[:prefix_length].rstrip("-") or "run"
    return f"{prefix}-{digest}"


def create_artifact_dir(root: Path, *, run_id: str) -> Path:
    """Exclusively claim one run's artifact directory without reusing prior state."""

    root.mkdir(parents=True, exist_ok=True)
    artifact_dir = (root / run_id).resolve()
    try:
        artifact_dir.mkdir()
    except FileExistsError as exc:
        raise E2ERunCollisionError(
            f"artifact directory {artifact_dir} already exists; choose a different E2E_RUN_ID or remove it explicitly",
        ) from exc
    artifact_dir.chmod(0o777)
    return artifact_dir


def apply_default_image_tags(env: dict[str, str], *, run_id: str) -> list[str]:
    defaulted_images: list[str] = []
    for key, template in IMAGE_ENV_DEFAULTS.items():
        if env.get(key):
            continue
        image = template.format(run_id=run_id)
        env[key] = image
        defaulted_images.append(image)
    return defaulted_images


def is_truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in TRUTHY_VALUES


def compose_up_command(
    compose: list[str],
    *,
    skip_image_build: bool,
    services: tuple[str, ...],
    wait: bool = False,
    no_deps: bool = False,
) -> list[str]:
    command = [*compose, "up", "--detach"]
    if no_deps:
        command.append("--no-deps")
    command.append("--no-build" if skip_image_build else "--build")
    if wait:
        command.extend(["--wait", "--wait-timeout", "420"])
    command.extend(services)
    return command


def assert_images_exist(images: list[str], *, env: dict[str, str]) -> None:
    missing_images = [image for image in dict.fromkeys(images) if not image_exists(image, env=env)]
    if not missing_images:
        return
    formatted_images = ", ".join(missing_images)
    message = f"E2E_SKIP_IMAGE_BUILD=1 but required Docker images are missing: {formatted_images}"
    print(message, file=sys.stderr, flush=True)
    raise subprocess.CalledProcessError(
        1,
        ["docker", "image", "inspect", *missing_images],
        stderr=message,
    )


def run_checked(command: list[str], *, env: dict[str, str]) -> None:
    print(f"$ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def run_best_effort(command: list[str], *, env: dict[str, str]) -> CommandFailure | None:
    print(f"$ {' '.join(command)}", flush=True)
    try:
        completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    except Exception as exc:  # best-effort cleanup must continue to subsequent commands
        message = f"command raised {type(exc).__name__}: {exc}"
        print(f"Best-effort command failed: {message}", file=sys.stderr, flush=True)
        return CommandFailure(command=tuple(command), message=message)
    if completed.returncode == 0:
        return None
    message = f"command exited with status {completed.returncode}"
    print(f"Best-effort command failed: {message}", file=sys.stderr, flush=True)
    return CommandFailure(command=tuple(command), message=message, returncode=completed.returncode)


def claim_project(project_name: str, *, env: dict[str, str]) -> ProjectClaim:
    """Atomically claim a Compose project name using a dedicated Docker network."""

    network_name = f"{project_name}-{PROJECT_CLAIM_SUFFIX}"
    command = [
        "docker",
        "network",
        "create",
        "--label",
        f"{PROJECT_CLAIM_LABEL}=true",
        "--label",
        f"{PROJECT_CLAIM_PROJECT_LABEL}={project_name}",
        network_name,
    ]
    print(f"$ {' '.join(command)}", flush=True)
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        inspect_command = ["docker", "network", "inspect", network_name]
        inspected = subprocess.run(
            inspect_command,
            cwd=ROOT,
            env=env,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if inspected.returncode == 0:
            raise E2ERunCollisionError(
                f"Docker project claim network {network_name!r} already exists; another worktree or retained "
                "E2E stack owns this E2E_RUN_ID",
            )
        raise subprocess.CalledProcessError(
            completed.returncode,
            command,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    network_id = completed.stdout.strip()
    if not network_id:
        raise RuntimeError(f"Docker created project claim network {network_name!r} without returning its id.")
    return ProjectClaim(network_name=network_name, network_id=network_id)


def release_project_claim(claim: ProjectClaim, *, env: dict[str, str]) -> CommandFailure | None:
    """Release an E2E project claim only after all normal cleanup has succeeded."""

    return run_best_effort(["docker", "network", "rm", claim.network_name], env=env)


def _claim_retained_reason(
    *,
    released: bool,
    keep_stack: bool,
    project_available: bool,
    cleanup_failures: list[CommandFailure],
) -> str | None:
    if released:
        return None
    if keep_stack:
        return "keep_stack"
    if not project_available:
        return "project_preflight_failed"
    if cleanup_failures:
        return "cleanup_failed"
    return "normal_teardown_not_completed"


def assert_project_available(project_name: str, *, env: dict[str, str]) -> None:
    """Reject any existing Docker resources owned by the requested Compose project."""

    project_label = f"label=com.docker.compose.project={project_name}"
    resource_commands = (
        ("containers", ["docker", "container", "ls", "--all", "--quiet", "--filter", project_label]),
        ("volumes", ["docker", "volume", "ls", "--quiet", "--filter", project_label]),
        ("networks", ["docker", "network", "ls", "--quiet", "--filter", project_label]),
    )
    collisions: list[str] = []
    for resource_kind, command in resource_commands:
        print(f"$ {' '.join(command)}", flush=True)
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        resource_ids = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if resource_ids:
            collisions.append(f"{resource_kind}={','.join(resource_ids)}")
    if collisions:
        raise E2ERunCollisionError(
            f"Compose project {project_name!r} already owns Docker resources ({'; '.join(collisions)}); "
            "choose a different E2E_RUN_ID or remove that stack explicitly",
        )


def parse_consumer_queues(output: str) -> set[str]:
    """Parse RabbitMQ's JSON consumer rows into the set of queues with consumers."""

    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RabbitMQConsumerInspectionError(
            f"rabbitmqctl list_consumers did not return valid JSON: {exc}; output={output[:500]!r}",
        ) from exc
    if not isinstance(payload, list):
        raise RabbitMQConsumerInspectionError(
            f"rabbitmqctl list_consumers must return a JSON array, got {type(payload).__name__}",
        )

    queues: set[str] = set()
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise RabbitMQConsumerInspectionError(
                f"rabbitmqctl consumer row {index} must be an object, got {type(row).__name__}",
            )
        queue_name = row.get("queue_name")
        if not isinstance(queue_name, str) or not queue_name.strip():
            raise RabbitMQConsumerInspectionError(
                f"rabbitmqctl consumer row {index} has invalid queue_name={queue_name!r}",
            )
        queues.add(queue_name)
    return queues


def parse_consumer_arguments(value: object, *, row_index: int) -> dict[str, object]:
    """Normalize RabbitMQ JSON consumer arguments into a string-keyed mapping."""

    if value is None:
        return {}
    if isinstance(value, dict):
        arguments: dict[str, object] = {}
        for key, argument_value in value.items():
            if not isinstance(key, str):
                raise RabbitMQConsumerInspectionError(
                    f"rabbitmqctl consumer row {row_index} has non-string argument keys",
                )
            arguments[key] = argument_value
        return arguments
    if not isinstance(value, list):
        raise RabbitMQConsumerInspectionError(
            f"rabbitmqctl consumer row {row_index} has invalid arguments={value!r}",
        )

    arguments = {}
    for argument_index, entry in enumerate(value):
        if not isinstance(entry, list) or len(entry) != 3:
            raise RabbitMQConsumerInspectionError(
                f"rabbitmqctl consumer row {row_index} argument {argument_index} "
                f"must be a [name, type, value] triplet, got {entry!r}",
            )
        name, value_type, argument_value = entry
        if not isinstance(name, str) or not isinstance(value_type, str):
            raise RabbitMQConsumerInspectionError(
                f"rabbitmqctl consumer row {row_index} argument {argument_index} "
                f"has invalid name/type metadata: {entry!r}",
            )
        if name in arguments:
            raise RabbitMQConsumerInspectionError(
                f"rabbitmqctl consumer row {row_index} repeats argument {name!r}",
            )
        arguments[name] = argument_value
    return arguments


def parse_pipeline_consumer_ownership(output: str) -> dict[str, list[str]]:
    """Parse worker ownership from inspectable RabbitMQ consumer metadata."""

    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RabbitMQConsumerInspectionError(
            f"rabbitmqctl list_consumers did not return valid JSON: {exc}; output={output[:500]!r}",
        ) from exc
    if not isinstance(payload, list):
        raise RabbitMQConsumerInspectionError(
            f"rabbitmqctl list_consumers must return a JSON array, got {type(payload).__name__}",
        )

    ownership: dict[str, list[str]] = {}
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise RabbitMQConsumerInspectionError(
                f"rabbitmqctl consumer row {index} must be an object, got {type(row).__name__}",
            )
        queue_name = row.get("queue_name")
        if not isinstance(queue_name, str) or not queue_name.strip():
            raise RabbitMQConsumerInspectionError(
                f"rabbitmqctl consumer row {index} has invalid queue_name={queue_name!r}",
            )
        consumer_arguments = parse_consumer_arguments(row.get("arguments"), row_index=index)
        role = consumer_arguments.get(PIPELINE_CONSUMER_ROLE_ARGUMENT)
        if role is None:
            continue
        if not isinstance(role, str) or not role.strip():
            raise RabbitMQConsumerInspectionError(
                f"rabbitmqctl consumer row {index} has invalid "
                f"{PIPELINE_CONSUMER_ROLE_ARGUMENT}={role!r}",
            )
        ownership.setdefault(queue_name, []).append(role)
    return ownership


def inspect_pipeline_consumers(
    compose: list[str],
    *,
    env: dict[str, str],
    timeout_seconds: float,
) -> set[str]:
    """Fetch the current RabbitMQ consumer table through the Compose service."""

    command = [
        *compose,
        "exec",
        "-T",
        "rabbitmq",
        "rabbitmqctl",
        "list_consumers",
        "--formatter=json",
        "--silent",
    ]
    print(f"$ {' '.join(command)}", flush=True)
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(0.1, timeout_seconds),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RabbitMQConsumerInspectionError(f"rabbitmqctl consumer inspection failed: {exc}") from exc
    if completed.returncode != 0:
        stdout = completed.stdout.strip()[:1000]
        stderr = completed.stderr.strip()[:1000]
        raise RabbitMQConsumerInspectionError(
            f"rabbitmqctl list_consumers exited with status {completed.returncode}; "
            f"stdout={stdout!r}; stderr={stderr!r}",
        )
    return parse_consumer_queues(completed.stdout)


def inspect_pipeline_consumer_ownership(
    compose: list[str],
    *,
    env: dict[str, str],
    timeout_seconds: float,
) -> dict[str, list[str]]:
    """Fetch queue-to-worker-role ownership through RabbitMQ consumer metadata."""

    command = [
        *compose,
        "exec",
        "-T",
        "rabbitmq",
        "rabbitmqctl",
        "list_consumers",
        "--formatter=json",
        "--silent",
    ]
    print(f"$ {' '.join(command)}", flush=True)
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(0.1, timeout_seconds),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RabbitMQConsumerInspectionError(f"rabbitmqctl consumer inspection failed: {exc}") from exc
    if completed.returncode != 0:
        stdout = completed.stdout.strip()[:1000]
        stderr = completed.stderr.strip()[:1000]
        raise RabbitMQConsumerInspectionError(
            f"rabbitmqctl list_consumers exited with status {completed.returncode}; "
            f"stdout={stdout!r}; stderr={stderr!r}",
        )
    return parse_pipeline_consumer_ownership(completed.stdout)


def wait_for_pipeline_consumers(
    compose: list[str],
    *,
    env: dict[str, str],
    required_queues: frozenset[str] = REQUIRED_PIPELINE_CONSUMER_QUEUES,
    timeout_seconds: float = PIPELINE_CONSUMER_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = PIPELINE_CONSUMER_POLL_INTERVAL_SECONDS,
    inspect_consumers: Callable[[float], set[str]] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait until every required E2E pipeline queue has a registered consumer."""

    deadline = monotonic() + timeout_seconds
    attempts = 0
    last_observed: set[str] = set()
    last_error: str | None = None

    def inspect(remaining_seconds: float) -> set[str]:
        if inspect_consumers is not None:
            return inspect_consumers(remaining_seconds)
        return inspect_pipeline_consumers(
            compose,
            env=env,
            timeout_seconds=min(RABBITMQCTL_TIMEOUT_SECONDS, remaining_seconds),
        )

    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        attempts += 1
        try:
            last_observed = inspect(remaining)
            last_error = None
        except (RabbitMQConsumerInspectionError, OSError, subprocess.TimeoutExpired) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            print(
                f"RabbitMQ consumer readiness attempt {attempts} was transiently unavailable: {last_error}",
                file=sys.stderr,
                flush=True,
            )
        else:
            missing = required_queues - last_observed
            print(
                "RabbitMQ consumer readiness attempt "
                f"{attempts}: observed={','.join(sorted(last_observed)) or '(none)'}; "
                f"missing={','.join(sorted(missing)) or '(none)'}.",
                flush=True,
            )
            if not missing:
                print(f"All required pipeline consumers registered after {attempts} attempt(s).", flush=True)
                return

        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(poll_interval_seconds, remaining))

    missing = required_queues - last_observed
    raise PipelineConsumerReadinessError(
        f"Timed out after {timeout_seconds:.1f}s waiting for RabbitMQ pipeline consumers; attempts={attempts}; "
        f"missing={','.join(sorted(missing)) or '(none)'}; "
        f"observed={','.join(sorted(last_observed)) or '(none)'}; last_error={last_error or '(none)'}",
    )


def wait_for_pipeline_consumer_ownership(
    compose: list[str],
    *,
    env: dict[str, str],
    expected_roles: dict[str, str] = EXPECTED_PIPELINE_CONSUMER_ROLES,
    timeout_seconds: float = PIPELINE_CONSUMER_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = PIPELINE_CONSUMER_POLL_INTERVAL_SECONDS,
    inspect_consumers: Callable[[float], dict[str, list[str]]] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait until every required queue has exactly one consumer from its intended role."""

    deadline = monotonic() + timeout_seconds
    attempts = 0
    last_observed: dict[str, list[str]] = {}
    last_error: str | None = None
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        attempts += 1
        try:
            last_observed = (
                inspect_consumers(remaining)
                if inspect_consumers is not None
                else inspect_pipeline_consumer_ownership(
                    compose,
                    env=env,
                    timeout_seconds=min(RABBITMQCTL_TIMEOUT_SECONDS, remaining),
                )
            )
            last_error = None
        except (RabbitMQConsumerInspectionError, OSError, subprocess.TimeoutExpired) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            mismatches = {
                queue_name: roles
                for queue_name, expected_role in expected_roles.items()
                if (roles := last_observed.get(queue_name, [])) != [expected_role]
            }
            print(
                f"RabbitMQ consumer ownership attempt {attempts}: mismatches={json.dumps(mismatches, sort_keys=True)}.",
                flush=True,
            )
            if not mismatches:
                print(f"All pipeline consumers have the intended role after {attempts} attempt(s).", flush=True)
                return

        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(poll_interval_seconds, remaining))

    raise PipelineConsumerOwnershipError(
        f"Timed out after {timeout_seconds:.1f}s waiting for RabbitMQ consumer role ownership; "
        f"attempts={attempts}; expected={json.dumps(expected_roles, sort_keys=True)}; "
        f"observed={json.dumps(last_observed, sort_keys=True)}; last_error={last_error or '(none)'}",
    )


def remove_defaulted_images(images: list[str], *, env: dict[str, str]) -> list[CommandFailure]:
    failures: list[CommandFailure] = []
    existing_images: list[str] = []
    for image in images:
        try:
            if image_exists(image, env=env):
                existing_images.append(image)
        except Exception as exc:  # keep inspecting/removing other per-run image tags
            command = ("docker", "image", "inspect", image)
            message = f"command raised {type(exc).__name__}: {exc}"
            print(f"Best-effort image inspection failed: {message}", file=sys.stderr, flush=True)
            failures.append(CommandFailure(command=command, message=message))
    if not existing_images:
        return failures
    failure = run_best_effort(["docker", "image", "rm", *existing_images], env=env)
    if failure is not None:
        failures.append(failure)
    return failures


def image_exists(image: str, *, env: dict[str, str]) -> bool:
    completed = subprocess.run(
        ["docker", "image", "inspect", image],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def collect_artifacts(compose: list[str], *, env: dict[str, str], artifact_dir: Path) -> list[CommandFailure]:
    captures = (
        ([*compose, "ps", "--all"], artifact_dir / "compose-ps.txt"),
        ([*compose, "config"], artifact_dir / "compose-config.yml"),
        (
            [*compose, "logs", "--no-color", "--timestamps", *LOG_SERVICES],
            artifact_dir / "compose-logs.txt",
        ),
        (["docker", "version"], artifact_dir / "docker-version.txt"),
        (["docker", "compose", "version"], artifact_dir / "docker-compose-version.txt"),
    )
    failures: list[CommandFailure] = []
    for command, output_path in captures:
        failure = capture(command, env=env, output_path=output_path)
        if failure is not None:
            failures.append(failure)
    return failures


def capture(command: list[str], *, env: dict[str, str], output_path: Path) -> CommandFailure | None:
    """Capture one diagnostics command and leave an in-file marker on failure."""

    print(f"$ {' '.join(command)} > {output_path}", flush=True)
    try:
        with output_path.open("w", encoding="utf-8") as output_file:
            output_file.write(f"$ {' '.join(command)}\n")
            output_file.flush()
            try:
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=env,
                    stdout=output_file,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            except Exception as exc:
                message = f"command raised {type(exc).__name__}: {exc}"
                output_file.write(f"\n!!! ARTIFACT CAPTURE FAILED: {message} !!!\n")
                print(f"Artifact capture failed for {output_path}: {message}", file=sys.stderr, flush=True)
                return CommandFailure(
                    command=tuple(command),
                    message=message,
                    output_path=str(output_path),
                )
            if completed.returncode == 0:
                return None
            message = f"command exited with status {completed.returncode}"
            output_file.write(f"\n!!! ARTIFACT CAPTURE FAILED: {message} !!!\n")
            print(f"Artifact capture failed for {output_path}: {message}", file=sys.stderr, flush=True)
            return CommandFailure(
                command=tuple(command),
                message=message,
                returncode=completed.returncode,
                output_path=str(output_path),
            )
    except Exception as exc:
        message = f"unable to write capture file: {type(exc).__name__}: {exc}"
        print(f"Artifact capture failed for {output_path}: {message}", file=sys.stderr, flush=True)
        return CommandFailure(command=tuple(command), message=message, output_path=str(output_path))


def _command_as_list(command: object) -> list[str]:
    if isinstance(command, (list, tuple)):
        return [str(part) for part in command]
    return [str(command)]


def write_metadata(artifact_dir: Path, payload: dict[str, object]) -> None:
    (artifact_dir / "run-metadata.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def append_metadata(artifact_dir: Path, payload: dict[str, object]) -> None:
    metadata_path = artifact_dir / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    metadata.update(payload)
    write_metadata(artifact_dir, metadata)


if __name__ == "__main__":
    raise SystemExit(main())
