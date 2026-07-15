#!/usr/bin/env python3
"""Validate production Compose app-service environment propagation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
COMPOSE_FILE: Final = ROOT / "docker-compose.prod.example.yml"
ENV_FILE: Final = ROOT / ".env.prod.example"
WORKER_SERVICES: Final = (
    "worker-media",
    "worker-ocr",
    "worker-enrichment",
    "worker-sync",
    "worker-telegram",
)
APP_SERVICES: Final = ("migrate", "api", *WORKER_SERVICES, "telegram-crawler", "scheduler")

COMMON_PROVIDER_AUTH_ENV_KEYS: Final = (
    "PIPELINE_VOYAGE_PROVIDER_MODE",
    "PIPELINE_VOYAGE_MODEL",
    "PIPELINE_VOYAGE_OUTPUT_DIMENSIONS",
    "PIPELINE_VOYAGE_API_URL",
    "PIPELINE_VOYAGE_API_KEY",
    "PIPELINE_VOYAGE_TIMEOUT_SECONDS",
    "PIPELINE_CLASSIFICATION_PROVIDER_MODE",
    "PIPELINE_CLASSIFICATION_API_URL",
    "PIPELINE_CLASSIFICATION_API_KEY",
    "PIPELINE_CLASSIFICATION_MODEL",
    "PIPELINE_CLASSIFICATION_TIMEOUT_SECONDS",
    "PIPELINE_CLASSIFICATION_NSFW_THRESHOLD",
    "PIPELINE_SEO_PROVIDER_MODE",
    "PIPELINE_SEO_MODEL",
    "PIPELINE_SEO_API_BASE_URL",
    "PIPELINE_SEO_API_KEY",
    "PIPELINE_SEO_TIMEOUT_SECONDS",
    "PIPELINE_SEO_MAX_ATTEMPTS",
    "PIPELINE_SEO_IMAGE_MAX_BYTES",
    "PIPELINE_SEO_PROMPT_VERSION",
    "AUTH_ACCESS_TOKEN_TTL_SECONDS",
    "AUTH_ACCESS_COOKIE_NAME",
    "AUTH_ACCESS_COOKIE_SECURE",
    "AUTH_ACCESS_COOKIE_HTTPONLY",
    "AUTH_ACCESS_COOKIE_SAMESITE",
    "AUTH_ACCESS_COOKIE_PATH",
    "AUTH_ACCESS_COOKIE_DOMAIN",
    "AUTH_TELEGRAM_BOT_TOKEN",
    "AUTH_TELEGRAM_BOT_USERNAME",
    "AUTH_TELEGRAM_LOGIN_MAX_AGE_SECONDS",
    "AUTH_TELEGRAM_MINIAPP_MAX_AGE_SECONDS",
    "AUTH_TELEGRAM_LINK_CODE_TTL_SECONDS",
    "AUTH_TELEGRAM_LINK_RETURN_URL",
    "AUTH_GOOGLE_CLIENT_ID",
    "AUTH_GOOGLE_CLIENT_SECRET",
    "AUTH_GOOGLE_REDIRECT_URI",
    "AUTH_GOOGLE_TOKEN_URL",
    "AUTH_GOOGLE_USERINFO_URL",
    "AUTH_GOOGLE_TIMEOUT_SECONDS",
)
WORKER_ENV_KEYS: Final = (
    "PIPELINE_WORKER_PREFETCH_COUNT",
    "PIPELINE_OCR_PROVIDER_MODE",
    "PIPELINE_OCR_PRIMARY_ENGINE",
    "PIPELINE_OCR_PADDLE_COMMAND",
    "PIPELINE_OCR_FALLBACK_ENGINE",
    "PIPELINE_OCR_FALLBACK_COMMAND",
    "PIPELINE_OCR_TIMEOUT_SECONDS",
    "PIPELINE_OCR_LOW_CONFIDENCE_THRESHOLD",
    "DATABASE_POOL_SIZE",
    "DATABASE_MAX_OVERFLOW",
    "DATABASE_POOL_TIMEOUT_SECONDS",
    "DATABASE_APPLICATION_NAME",
    "RUNTIME_HEALTH_FILE",
)
SERVICE_SPECIFIC_ENV_KEYS: Final = {
    "api": ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"),
    **{service_name: WORKER_ENV_KEYS for service_name in WORKER_SERVICES},
    "worker-telegram": (*WORKER_ENV_KEYS, "TELEGRAM_API_ID", "TELEGRAM_API_HASH"),
    "telegram-crawler": ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"),
    "scheduler": ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"),
}


class ComposeEnvValidationError(RuntimeError):
    """Raised when rendered production Compose env keys are missing."""


def main() -> int:
    try:
        config = render_compose_config()
        validate_app_environments(config)
    except ComposeEnvValidationError as exc:
        print(exc, file=sys.stderr)
        return 1

    print("Production Compose app environment validation passed.")
    return 0


def render_compose_config() -> dict[str, object]:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(ENV_FILE),
        "-f",
        str(COMPOSE_FILE),
        "config",
        "--format",
        "json",
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise ComposeEnvValidationError(
            "Production Compose config render failed.\n"
            f"Command: {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}",
        )

    try:
        rendered_config = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ComposeEnvValidationError(f"Production Compose config did not render valid JSON: {exc}") from exc
    if not isinstance(rendered_config, dict):
        raise ComposeEnvValidationError("Production Compose config JSON root is not an object.")
    return rendered_config


def validate_app_environments(config: dict[str, object]) -> None:
    services = config.get("services")
    if not isinstance(services, dict):
        raise ComposeEnvValidationError("Production Compose config is missing a services object.")

    failures: list[str] = []
    for service_name in APP_SERVICES:
        service = services.get(service_name)
        if not isinstance(service, dict):
            failures.append(f"{service_name}: service is missing")
            continue

        actual_env_keys = collect_environment_keys(service.get("environment"))
        expected_env_keys = set(COMMON_PROVIDER_AUTH_ENV_KEYS)
        expected_env_keys.update(SERVICE_SPECIFIC_ENV_KEYS.get(service_name, ()))
        missing_env_keys = sorted(expected_env_keys - actual_env_keys)
        if missing_env_keys:
            failures.append(f"{service_name}: missing {', '.join(missing_env_keys)}")

    if failures:
        raise ComposeEnvValidationError(
            "Production Compose app environment propagation is incomplete:\n" + "\n".join(failures),
        )


def collect_environment_keys(environment: object) -> set[str]:
    if isinstance(environment, dict):
        return {str(key) for key in environment}
    if isinstance(environment, list):
        keys: set[str] = set()
        for entry in environment:
            if isinstance(entry, str):
                keys.add(entry.split("=", maxsplit=1)[0])
        return keys
    return set()


if __name__ == "__main__":
    raise SystemExit(main())
