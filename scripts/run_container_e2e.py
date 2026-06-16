#!/usr/bin/env python3
"""Run the parallel-safe containerized PRD E2E suite."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.e2e.yml"
ARTIFACT_ROOT = ROOT / ".artifacts" / "e2e"
PROJECT_PREFIX: Final = "memexpert-e2e"
IMAGE_ENV_DEFAULTS: Final = {
    "MEMEXPERT_API_IMAGE": "memexpert-api:e2e-{run_id}",
    "MEMEXPERT_WORKER_IMAGE": "memexpert-worker:e2e-{run_id}",
    "MEMEXPERT_FRONTEND_IMAGE": "memexpert-frontend:e2e-{run_id}",
    "MEMEXPERT_E2E_RUNNER_IMAGE": "memexpert-e2e-runner:e2e-{run_id}",
}
WAITED_LONG_LIVED_SERVICES: Final = ("api", "frontend")
NON_HEALTHCHECKED_LONG_LIVED_SERVICES: Final = ("workers",)
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
    "workers",
    "frontend",
    "seed",
    "e2e-runner",
)
RUN_ID_RE = re.compile(r"[^a-z0-9-]+")


def main() -> int:
    run_id = sanitize_run_id(os.environ.get("E2E_RUN_ID") or uuid.uuid4().hex[:12])
    project_name = f"{PROJECT_PREFIX}-{run_id}"
    artifact_dir = (ARTIFACT_ROOT / run_id).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.chmod(0o777)

    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = project_name
    env["E2E_RUN_ID"] = run_id
    env["E2E_ARTIFACT_DIR"] = str(artifact_dir)
    defaulted_images = apply_default_image_tags(env, run_id=run_id)

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
            "started_at": started_at.isoformat(),
        },
    )

    exit_code = 0
    try:
        print(f"Starting {project_name}; artifacts: {artifact_dir}", flush=True)
        run_checked(
            [*compose, "up", "--detach", "--build", "--wait", "--wait-timeout", "420", *WAITED_LONG_LIVED_SERVICES],
            env=env,
        )
        run_checked([*compose, "up", "--detach", "--no-deps", *NON_HEALTHCHECKED_LONG_LIVED_SERVICES], env=env)
        for service in NON_HEALTHCHECKED_LONG_LIVED_SERVICES:
            assert_service_running(compose, service=service, env=env)
        run_checked([*compose, "build", "e2e-runner"], env=env)
        run_checked([*compose, "run", "--rm", "--no-deps", "seed"], env=env)
        run_checked([*compose, "run", "--rm", "--no-deps", "e2e-runner"], env=env)
    except subprocess.CalledProcessError as exc:
        exit_code = exc.returncode or 1
        print(f"Container PRD E2E failed with exit code {exit_code}.", file=sys.stderr, flush=True)
    finally:
        collect_artifacts(compose, env=env, artifact_dir=artifact_dir)
        append_metadata(
            artifact_dir,
            {
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "exit_code": exit_code,
                "keep_stack": os.environ.get("E2E_KEEP_STACK") == "1",
            },
        )
        if os.environ.get("E2E_KEEP_STACK") == "1":
            print(f"Keeping Compose stack {project_name} because E2E_KEEP_STACK=1.", flush=True)
        else:
            run_best_effort([*compose, "down", "-v", "--remove-orphans"], env=env)
            remove_defaulted_images(defaulted_images, env=env)

    return exit_code


def sanitize_run_id(raw: str) -> str:
    normalized = RUN_ID_RE.sub("-", raw.strip().lower()).strip("-")
    if not normalized:
        normalized = uuid.uuid4().hex[:12]
    return normalized[:48].strip("-") or uuid.uuid4().hex[:12]


def apply_default_image_tags(env: dict[str, str], *, run_id: str) -> list[str]:
    defaulted_images: list[str] = []
    for key, template in IMAGE_ENV_DEFAULTS.items():
        if env.get(key):
            continue
        image = template.format(run_id=run_id)
        env[key] = image
        defaulted_images.append(image)
    return defaulted_images


def run_checked(command: list[str], *, env: dict[str, str]) -> None:
    print(f"$ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def run_best_effort(command: list[str], *, env: dict[str, str]) -> None:
    print(f"$ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=False)


def assert_service_running(compose: list[str], *, service: str, env: dict[str, str]) -> None:
    command = [*compose, "ps", "--status", "running", "--services", service]
    print(f"$ {' '.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=ROOT, env=env, check=True, stdout=subprocess.PIPE, text=True)
    running_services = set(completed.stdout.splitlines())
    print(f"Observed running Compose services: {', '.join(sorted(running_services)) or '(none)'}", flush=True)
    if service not in running_services:
        print(f"Compose service {service!r} is not running.", file=sys.stderr, flush=True)
        raise subprocess.CalledProcessError(
            1,
            command,
            output=completed.stdout,
            stderr=f"Compose service {service!r} is not running.",
        )


def remove_defaulted_images(images: list[str], *, env: dict[str, str]) -> None:
    existing_images = [image for image in images if image_exists(image, env=env)]
    if not existing_images:
        return
    run_best_effort(["docker", "image", "rm", *existing_images], env=env)


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


def collect_artifacts(compose: list[str], *, env: dict[str, str], artifact_dir: Path) -> None:
    capture([*compose, "ps", "--all"], env=env, output_path=artifact_dir / "compose-ps.txt")
    capture([*compose, "config"], env=env, output_path=artifact_dir / "compose-config.yml")
    capture(
        [*compose, "logs", "--no-color", "--timestamps", *LOG_SERVICES],
        env=env,
        output_path=artifact_dir / "compose-logs.txt",
    )
    capture(["docker", "version"], env=env, output_path=artifact_dir / "docker-version.txt")
    capture(["docker", "compose", "version"], env=env, output_path=artifact_dir / "docker-compose-version.txt")


def capture(command: list[str], *, env: dict[str, str], output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8") as output_file:
        output_file.write(f"$ {' '.join(command)}\n")
        subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            stdout=output_file,
            stderr=subprocess.STDOUT,
            check=False,
        )


def write_metadata(artifact_dir: Path, payload: dict[str, object]) -> None:
    (artifact_dir / "run-metadata.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def append_metadata(artifact_dir: Path, payload: dict[str, object]) -> None:
    metadata_path = artifact_dir / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    metadata.update(payload)
    write_metadata(artifact_dir, metadata)


if __name__ == "__main__":
    raise SystemExit(main())
