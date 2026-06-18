"""Voyage multimodal embedding adapter boundary used by the heavy content pipeline."""

from __future__ import annotations

import array
import base64
import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx

from memexpert.core.config import Settings, get_settings

# Voyage returns float32 embeddings; the pipeline serializes them to raw little-endian
# bytes before persisting them in ``EmbeddingCache.embedding`` so downstream similarity
# lookups can reconstruct the vector without JSON parsing overhead.
_FLOAT32_ARRAY_TYPE_CODE = "f"
_FLOAT32_BYTE_WIDTH = 4


@dataclass(frozen=True, slots=True)
class VoyageEmbeddingResult:
    """One embedding vector returned by the Voyage multimodal provider."""

    model: str
    dimensions: int
    vector: tuple[float, ...]
    input_hash: str

    @property
    def embedding_bytes(self) -> bytes:
        """Return the ``float32`` little-endian bytes persisted in ``EmbeddingCache``."""

        float_array = array.array(_FLOAT32_ARRAY_TYPE_CODE, self.vector)
        if float_array.itemsize != _FLOAT32_BYTE_WIDTH:
            raise VoyageMalformedResponseError(
                "Voyage embedding could not be encoded as float32 bytes on this platform.",
            )
        return float_array.tobytes()


class VoyageEmbeddingError(RuntimeError):
    """Base error raised when Voyage embedding work cannot complete."""


class VoyageProviderUnavailableError(VoyageEmbeddingError):
    """Raised when the Voyage provider is unreachable or not configured."""


class VoyageTimeoutError(VoyageEmbeddingError):
    """Raised when Voyage provider execution exceeds the configured timeout."""


class VoyageMalformedResponseError(VoyageEmbeddingError):
    """Raised when the Voyage provider returns a response the pipeline cannot persist."""


class VoyageClientProtocol(Protocol):
    """Typed Voyage adapter surface used by worker runtime and tests."""

    async def embed_image(self, *, image_bytes: bytes, mime_type: str) -> VoyageEmbeddingResult: ...

    async def embed_text(self, *, text: str) -> VoyageEmbeddingResult: ...


class _FakeMarker(StrEnum):
    CAT = "cat"
    DOG = "dog"
    FROG = "frog"
    UNKNOWN = "unknown"


def build_voyage_input_hash(image_bytes: bytes) -> str:
    """Return the deterministic SHA-256 key that de-duplicates embedding cache rows."""

    return hashlib.sha256(image_bytes).hexdigest()


class PipelineVoyageClient:
    """HTTP adapter that calls the Voyage multimodal embeddings endpoint with strict typing.

    The Voyage Python SDK does not ship a first-party typed async client at the time of S02,
    so this wrapper talks to the documented REST endpoint directly. All network work is lazy
    (no client created at import time) so tests can inject a fake and `create_app()` stays
    side-effect free.
    """

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def embed_image(self, *, image_bytes: bytes, mime_type: str) -> VoyageEmbeddingResult:
        api_key = self._resolve_api_key()
        payload = self._build_request_payload(image_bytes=image_bytes, mime_type=mime_type)
        vector = await self._post_embedding_request(api_key=api_key, payload=payload)
        expected_dimensions = self._settings.pipeline_voyage_output_dimensions
        if len(vector) != expected_dimensions:
            raise VoyageMalformedResponseError(
                f"Voyage embedding returned {len(vector)} dims, expected {expected_dimensions}.",
            )
        return VoyageEmbeddingResult(
            model=self._settings.pipeline_voyage_model,
            dimensions=expected_dimensions,
            vector=vector,
            input_hash=build_voyage_input_hash(image_bytes),
        )

    async def embed_text(self, *, text: str) -> VoyageEmbeddingResult:
        api_key = self._resolve_api_key()
        payload = self._build_text_request_payload(text=text)
        vector = await self._post_embedding_request(api_key=api_key, payload=payload)
        expected_dimensions = self._settings.pipeline_voyage_output_dimensions
        if len(vector) != expected_dimensions:
            raise VoyageMalformedResponseError(
                f"Voyage embedding returned {len(vector)} dims, expected {expected_dimensions}.",
            )
        return VoyageEmbeddingResult(
            model=self._settings.pipeline_voyage_model,
            dimensions=expected_dimensions,
            vector=vector,
            input_hash=build_voyage_text_input_hash(text),
        )

    async def _post_embedding_request(self, *, api_key: str, payload: dict[str, object]) -> tuple[float, ...]:
        try:
            async with httpx.AsyncClient(timeout=self._settings.pipeline_voyage_timeout_seconds) as client:
                response = await client.post(
                    self._settings.pipeline_voyage_api_url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.TimeoutException as exc:
            raise VoyageTimeoutError(
                f"Timed out after {self._settings.pipeline_voyage_timeout_seconds:.2f}s while calling Voyage.",
            ) from exc
        except httpx.HTTPError as exc:
            raise VoyageProviderUnavailableError(f"Voyage provider unavailable: {exc}") from exc

        if response.status_code >= 500:
            raise VoyageProviderUnavailableError(
                f"Voyage provider returned upstream error {response.status_code}.",
            )
        if response.status_code >= 400:
            raise VoyageProviderUnavailableError(
                f"Voyage provider rejected the embedding request with status {response.status_code}.",
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise VoyageMalformedResponseError("Voyage provider returned a non-JSON response.") from exc

        return _extract_voyage_vector(body)

    def _resolve_api_key(self) -> str:
        configured_key = self._settings.pipeline_voyage_api_key
        if configured_key is None:
            raise VoyageProviderUnavailableError(
                "Voyage API key is not configured (set PIPELINE_VOYAGE_API_KEY).",
            )
        raw_api_key = configured_key.get_secret_value().strip()
        if not raw_api_key:
            raise VoyageProviderUnavailableError("Voyage API key is blank in configuration.")
        return raw_api_key

    def _build_request_payload(self, *, image_bytes: bytes, mime_type: str) -> dict[str, object]:
        encoded_bytes = base64.b64encode(image_bytes).decode("ascii")
        return {
            "model": self._settings.pipeline_voyage_model,
            "output_dimension": self._settings.pipeline_voyage_output_dimensions,
            "input_type": "image",
            "inputs": [
                {
                    "content": [
                        {
                            "type": "image_base64",
                            "image_base64": f"data:{mime_type};base64,{encoded_bytes}",
                        }
                    ],
                }
            ],
        }

    def _build_text_request_payload(self, *, text: str) -> dict[str, object]:
        return {
            "model": self._settings.pipeline_voyage_model,
            "output_dimension": self._settings.pipeline_voyage_output_dimensions,
            "input_type": "query",
            "inputs": [
                {
                    "content": [
                        {
                            "type": "text",
                            "text": text,
                        }
                    ],
                }
            ],
        }


class FakeVoyageClient:
    """Deterministic embedding provider for local E2E runs without Voyage credentials."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def embed_image(self, *, image_bytes: bytes, mime_type: str) -> VoyageEmbeddingResult:
        _ = mime_type
        marker = _detect_fake_image_marker(image_bytes)
        return VoyageEmbeddingResult(
            model=self._settings.pipeline_voyage_model,
            dimensions=self._settings.pipeline_voyage_output_dimensions,
            vector=_fake_vector_for_marker(
                marker,
                dimensions=self._settings.pipeline_voyage_output_dimensions,
                stable_input=image_bytes,
            ),
            input_hash=build_voyage_input_hash(image_bytes),
        )

    async def embed_text(self, *, text: str) -> VoyageEmbeddingResult:
        marker = _detect_fake_text_marker(text)
        return VoyageEmbeddingResult(
            model=self._settings.pipeline_voyage_model,
            dimensions=self._settings.pipeline_voyage_output_dimensions,
            vector=_fake_vector_for_marker(
                marker,
                dimensions=self._settings.pipeline_voyage_output_dimensions,
                stable_input=text.encode("utf-8"),
            ),
            input_hash=build_voyage_text_input_hash(text),
        )


def build_pipeline_voyage_client(*, settings: Settings | None = None) -> VoyageClientProtocol:
    """Return the configured Voyage-compatible embedding provider."""

    resolved_settings = settings or get_settings()
    if resolved_settings.pipeline_voyage_provider_mode == "fake":
        return FakeVoyageClient(settings=resolved_settings)
    return PipelineVoyageClient(settings=resolved_settings)


def decode_embedding_bytes(embedding_bytes: bytes, *, dimensions: int) -> tuple[float, ...]:
    """Decode ``EmbeddingCache.embedding`` bytes into a typed vector for Qdrant lookups."""

    expected_length = dimensions * _FLOAT32_BYTE_WIDTH
    if len(embedding_bytes) != expected_length:
        raise VoyageMalformedResponseError(
            f"Embedding bytes have length {len(embedding_bytes)}, expected {expected_length}.",
        )
    float_array = array.array(_FLOAT32_ARRAY_TYPE_CODE)
    float_array.frombytes(embedding_bytes)
    if float_array.itemsize != _FLOAT32_BYTE_WIDTH:
        raise VoyageMalformedResponseError(
            "Embedding bytes could not be decoded as float32 on this platform.",
        )
    return tuple(float(value) for value in float_array)


def build_voyage_text_input_hash(text: str) -> str:
    """Return the deterministic SHA-256 key used for cached text query embeddings."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _detect_fake_text_marker(text: str) -> _FakeMarker:
    normalized_text = text.casefold()
    if "cat" in normalized_text:
        return _FakeMarker.CAT
    if "dog" in normalized_text:
        return _FakeMarker.DOG
    if "frog" in normalized_text or "other" in normalized_text:
        return _FakeMarker.FROG
    return _FakeMarker.UNKNOWN


def _detect_fake_image_marker(image_bytes: bytes) -> _FakeMarker:
    import io

    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        return _FakeMarker.UNKNOWN

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            rgb_image = image.convert("RGB").resize((1, 1))
            pixel = rgb_image.getpixel((0, 0))
    except (UnidentifiedImageError, OSError, ValueError):
        return _FakeMarker.UNKNOWN

    if not isinstance(pixel, tuple) or len(pixel) < 3:
        return _FakeMarker.UNKNOWN
    red, green, blue = int(pixel[0]), int(pixel[1]), int(pixel[2])

    if red > green and red > blue:
        return _FakeMarker.CAT
    if blue > red and blue > green:
        return _FakeMarker.DOG
    if green > red and green > blue:
        return _FakeMarker.FROG
    return _FakeMarker.UNKNOWN


def _fake_vector_for_marker(
    marker: _FakeMarker,
    *,
    dimensions: int,
    stable_input: bytes,
) -> tuple[float, ...]:
    vector = [0.0] * dimensions
    bucket = _fake_bucket_for_marker(marker, dimensions=dimensions, stable_input=stable_input)
    vector[bucket] = 1.0
    return tuple(vector)


def _fake_bucket_for_marker(
    marker: _FakeMarker,
    *,
    dimensions: int,
    stable_input: bytes,
) -> int:
    if marker is _FakeMarker.CAT:
        return 0
    if marker is _FakeMarker.DOG:
        return min(1, dimensions - 1)
    if marker is _FakeMarker.FROG:
        return min(2, dimensions - 1)

    digest = hashlib.sha256(stable_input).digest()
    return int.from_bytes(digest[:8], byteorder="big") % dimensions


def _extract_voyage_vector(payload: object) -> tuple[float, ...]:
    if not isinstance(payload, dict):
        raise VoyageMalformedResponseError("Voyage response body is not a JSON object.")

    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise VoyageMalformedResponseError("Voyage response did not contain any embeddings.")

    first_entry = data[0]
    if not isinstance(first_entry, dict):
        raise VoyageMalformedResponseError("Voyage response entry is not a JSON object.")

    raw_embedding = first_entry.get("embedding")
    if not isinstance(raw_embedding, list) or not raw_embedding:
        raise VoyageMalformedResponseError("Voyage response entry is missing the embedding vector.")

    coerced_values: list[float] = []
    for item in raw_embedding:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise VoyageMalformedResponseError("Voyage embedding entry contains a non-numeric value.")
        coerced_values.append(float(item))
    return tuple(coerced_values)


__all__ = [
    "FakeVoyageClient",
    "PipelineVoyageClient",
    "VoyageClientProtocol",
    "VoyageEmbeddingError",
    "VoyageEmbeddingResult",
    "VoyageMalformedResponseError",
    "VoyageProviderUnavailableError",
    "VoyageTimeoutError",
    "build_pipeline_voyage_client",
    "build_voyage_input_hash",
    "build_voyage_text_input_hash",
    "decode_embedding_bytes",
]
