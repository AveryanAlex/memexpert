"""NSFW classification adapter boundary used by the heavy content pipeline."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol

import httpx

from memexpert.core.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Durable classification output persisted on a ``Meme`` during S02."""

    model: str
    is_nsfw: bool
    nsfw_score: float


class ClassificationError(RuntimeError):
    """Base error raised when classification work cannot complete."""


class ClassificationProviderUnavailableError(ClassificationError):
    """Raised when the classification provider is unreachable or not configured."""


class ClassificationTimeoutError(ClassificationError):
    """Raised when classification provider execution exceeds the configured timeout."""


class ClassificationMalformedResponseError(ClassificationError):
    """Raised when the classification provider returns data the pipeline cannot trust."""


class ClassificationClientProtocol(Protocol):
    """Typed classification boundary used by the worker runtime and tests."""

    async def classify_image(self, *, image_bytes: bytes, mime_type: str) -> ClassificationResult: ...


class PipelineClassificationClient:
    """HTTP adapter for the NSFW classification service.

    The adapter is deliberately thin: S02 only needs a truthful ``is_nsfw`` boolean and
    a score above a configured threshold. Concrete provider URLs/headers are pluggable
    via settings so operators can swap providers without touching service logic.
    """

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def classify_image(self, *, image_bytes: bytes, mime_type: str) -> ClassificationResult:
        api_url = self._resolve_api_url()
        payload = self._build_request_payload(image_bytes=image_bytes, mime_type=mime_type)
        try:
            async with httpx.AsyncClient(timeout=self._settings.pipeline_classification_timeout_seconds) as client:
                response = await client.post(
                    api_url,
                    json=payload,
                    headers=self._build_request_headers(),
                )
        except httpx.TimeoutException as exc:
            raise ClassificationTimeoutError(
                f"Timed out after {self._settings.pipeline_classification_timeout_seconds:.2f}s while "
                "calling the classification provider.",
            ) from exc
        except httpx.HTTPError as exc:
            raise ClassificationProviderUnavailableError(
                f"Classification provider unavailable: {exc}",
            ) from exc

        if response.status_code >= 500:
            raise ClassificationProviderUnavailableError(
                f"Classification provider returned upstream error {response.status_code}.",
            )
        if response.status_code >= 400:
            raise ClassificationProviderUnavailableError(
                f"Classification provider rejected the request with status {response.status_code}.",
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ClassificationMalformedResponseError(
                "Classification provider returned a non-JSON response.",
            ) from exc

        return self._parse_response_body(body)

    def _resolve_api_url(self) -> str:
        configured_url = self._settings.pipeline_classification_api_url
        if configured_url is None:
            raise ClassificationProviderUnavailableError(
                "Classification API URL is not configured (set PIPELINE_CLASSIFICATION_API_URL).",
            )
        return configured_url

    def _build_request_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        configured_key = self._settings.pipeline_classification_api_key
        if configured_key is not None:
            raw_api_key = configured_key.get_secret_value().strip()
            if raw_api_key:
                headers["Authorization"] = f"Bearer {raw_api_key}"
        return headers

    def _build_request_payload(self, *, image_bytes: bytes, mime_type: str) -> dict[str, object]:
        encoded_bytes = base64.b64encode(image_bytes).decode("ascii")
        return {
            "model": self._settings.pipeline_classification_model,
            "image": {
                "content_type": mime_type,
                "base64": encoded_bytes,
            },
        }

    def _parse_response_body(self, body: object) -> ClassificationResult:
        if not isinstance(body, dict):
            raise ClassificationMalformedResponseError(
                "Classification response body is not a JSON object.",
            )
        raw_score = body.get("nsfw_score")
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            raise ClassificationMalformedResponseError(
                "Classification response is missing the 'nsfw_score' numeric field.",
            )
        nsfw_score = float(raw_score)
        if not 0.0 <= nsfw_score <= 1.0:
            raise ClassificationMalformedResponseError(
                "Classification nsfw_score must be between 0 and 1.",
            )
        return ClassificationResult(
            model=self._settings.pipeline_classification_model,
            is_nsfw=nsfw_score >= self._settings.pipeline_classification_nsfw_threshold,
            nsfw_score=nsfw_score,
        )


class FakeClassificationClient:
    """Safe deterministic classifier used by local E2E smoke runs."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def classify_image(self, *, image_bytes: bytes, mime_type: str) -> ClassificationResult:
        _ = (image_bytes, mime_type)
        nsfw_score = self._settings.pipeline_fake_classification_nsfw_score
        return ClassificationResult(
            model=self._settings.pipeline_classification_model,
            is_nsfw=nsfw_score >= self._settings.pipeline_classification_nsfw_threshold,
            nsfw_score=nsfw_score,
        )


def build_pipeline_classification_client(
    *,
    settings: Settings | None = None,
) -> ClassificationClientProtocol:
    """Return the configured classification provider without changing live defaults."""

    resolved_settings = settings or get_settings()
    if resolved_settings.pipeline_classification_provider_mode == "fake":
        return FakeClassificationClient(settings=resolved_settings)
    return PipelineClassificationClient(settings=resolved_settings)


__all__ = [
    "ClassificationClientProtocol",
    "ClassificationError",
    "ClassificationMalformedResponseError",
    "ClassificationProviderUnavailableError",
    "ClassificationResult",
    "ClassificationTimeoutError",
    "FakeClassificationClient",
    "PipelineClassificationClient",
    "build_pipeline_classification_client",
]
