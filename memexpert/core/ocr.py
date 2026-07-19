"""OCR adapter boundary for the heavy content pipeline."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import tempfile
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from memexpert.core.config import Settings, get_settings
from memexpert.models.enums import ContentLanguage

if TYPE_CHECKING:
    from memexpert.media.contracts import PipelineMediaProcessorProtocol

PADDLE_OCR_CPU_THREADS = 1
_SUBPROCESS_REAP_TIMEOUT_SECONDS = 5.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OCRExtractionResult:
    """Durable OCR output persisted for one meme file."""

    engine: str
    fallback_engine: str | None
    fallback_used: bool
    low_confidence: bool
    confidence: float | None
    language: ContentLanguage
    extracted_text: str | None
    source_object_key: str


@dataclass(frozen=True, slots=True)
class _OCRCandidate:
    extracted_text: str | None
    confidence: float | None
    language: ContentLanguage | None


class OCRProcessingError(RuntimeError):
    """Base error raised when OCR processing cannot complete."""


class OCRTimeoutError(OCRProcessingError):
    """Raised when OCR provider execution exceeds the configured timeout."""


class OCRProviderUnavailableError(OCRProcessingError):
    """Raised when neither the primary nor fallback OCR engine is available."""


class OCRMalformedOutputError(OCRProcessingError):
    """Raised when a provider returns malformed OCR output."""


class OCRProcessorProtocol(Protocol):
    """Typed OCR boundary used by the worker runtime."""

    async def extract_text(
        self,
        *,
        filename: str,
        mime_type: str,
        media_bytes: bytes,
        source_object_key: str,
    ) -> OCRExtractionResult: ...


class PipelineOCRProcessor:
    """OCR adapter that prefers PaddleOCR and can use command boundaries."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        media_processor: PipelineMediaProcessorProtocol | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        if media_processor is None:
            from memexpert.media.inspect import PipelineMediaProcessor

            media_processor = PipelineMediaProcessor(settings=self._settings)
        self._media_processor = media_processor
        self._paddle_ocr: Any | None = None

    async def extract_text(
        self,
        *,
        filename: str,
        mime_type: str,
        media_bytes: bytes,
        source_object_key: str,
    ) -> OCRExtractionResult:
        preview_frame_bytes = await self._media_processor.extract_preview_frame(
            filename=filename,
            content_type=mime_type,
            media_bytes=media_bytes,
        )

        primary_candidate: _OCRCandidate | None = None
        try:
            primary_candidate = await self._run_primary(preview_frame_bytes)
        except OCRProviderUnavailableError:
            primary_candidate = None
        except OCRMalformedOutputError:
            primary_candidate = _OCRCandidate(extracted_text=None, confidence=0.0, language=None)

        primary_is_usable = _candidate_has_text(primary_candidate)
        primary_is_low_confidence = _candidate_is_low_confidence(
            primary_candidate,
            threshold=self._settings.pipeline_ocr_low_confidence_threshold,
        )
        should_try_fallback = (
            self._settings.pipeline_ocr_fallback_command is not None
            and (primary_candidate is None or primary_is_low_confidence or not primary_is_usable)
        )

        fallback_candidate: _OCRCandidate | None = None
        if should_try_fallback:
            fallback_candidate = await self._run_fallback(preview_frame_bytes)

        final_candidate = primary_candidate
        fallback_used = False
        if fallback_candidate is not None and (
            final_candidate is None or _candidate_score(fallback_candidate) >= _candidate_score(primary_candidate)
        ):
            final_candidate = fallback_candidate
            fallback_used = True

        if final_candidate is None:
            raise OCRProviderUnavailableError(
                "No OCR engine produced a usable result, and no fallback command is configured."
            )

        normalized_text = _normalize_ocr_text(final_candidate.extracted_text)
        confidence = _normalize_confidence(final_candidate.confidence)
        low_confidence = (
            not normalized_text
            or confidence is None
            or confidence < self._settings.pipeline_ocr_low_confidence_threshold
        )
        language = final_candidate.language or _detect_language(normalized_text)

        return OCRExtractionResult(
            engine=self._settings.pipeline_ocr_primary_engine,
            fallback_engine=self._settings.pipeline_ocr_fallback_engine,
            fallback_used=fallback_used,
            low_confidence=low_confidence,
            confidence=confidence,
            language=language,
            extracted_text=normalized_text,
            source_object_key=source_object_key,
        )

    async def _run_primary(self, preview_frame_bytes: bytes) -> _OCRCandidate:
        if self._settings.pipeline_ocr_primary_engine.strip().lower() != "paddleocr":
            raise OCRProviderUnavailableError(
                f"Primary OCR engine {self._settings.pipeline_ocr_primary_engine!r} is not supported."
            )

        with tempfile.TemporaryDirectory(prefix="memexpert-ocr-primary-") as temp_dir:
            image_path = Path(temp_dir) / "preview.png"
            image_path.write_bytes(preview_frame_bytes)
            try:
                return await asyncio.to_thread(self._predict_with_paddleocr, image_path)
            except OCRProviderUnavailableError:
                return await self._run_paddle_command(image_path)

    def _predict_with_paddleocr(self, image_path: Path) -> _OCRCandidate:
        try:
            paddleocr_module = import_module("paddleocr")
            paddle_ocr_cls = getattr(paddleocr_module, "PaddleOCR", None)
        except ImportError as exc:
            raise OCRProviderUnavailableError(
                "PaddleOCR is not installed in this Python runtime."
            ) from exc

        if paddle_ocr_cls is None:
            raise OCRProviderUnavailableError("PaddleOCR could not be imported from the installed package.")

        paddle_ocr = self._paddle_ocr
        if paddle_ocr is None:
            paddle_ocr = paddle_ocr_cls(
                lang="ru",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                cpu_threads=PADDLE_OCR_CPU_THREADS,
            )
            self._paddle_ocr = paddle_ocr

        try:
            result = paddle_ocr.predict(input=str(image_path))
        except Exception as exc:
            raise OCRProcessingError(f"PaddleOCR failed to process {image_path.name}: {exc}") from exc

        return _parse_paddle_result(result)

    async def _run_paddle_command(self, image_path: Path) -> _OCRCandidate:
        paddle_command = self._settings.pipeline_ocr_paddle_command
        if paddle_command is None:
            raise OCRProviderUnavailableError(
                "PaddleOCR is not installed in this Python runtime, and no PaddleOCR command is configured."
            )

        return await self._run_ocr_command(
            command=paddle_command,
            image_path=image_path,
            purpose="PaddleOCR command",
        )

    async def _run_fallback(self, preview_frame_bytes: bytes) -> _OCRCandidate:
        fallback_command = self._settings.pipeline_ocr_fallback_command
        if fallback_command is None:
            raise OCRProviderUnavailableError("No OCR fallback command is configured.")

        with tempfile.TemporaryDirectory(prefix="memexpert-ocr-fallback-") as temp_dir:
            image_path = Path(temp_dir) / "preview.png"
            image_path.write_bytes(preview_frame_bytes)
            return await self._run_ocr_command(
                command=fallback_command,
                image_path=image_path,
                purpose="OCR fallback command",
            )

    async def _run_ocr_command(self, *, command: str, image_path: Path, purpose: str) -> _OCRCandidate:
        command_args = _render_ocr_command(command, image_path, purpose=purpose)
        process = await asyncio.create_subprocess_exec(
            *command_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(self._settings.pipeline_ocr_timeout_seconds):
                stdout, stderr = await process.communicate()
        except TimeoutError as exc:
            await _kill_and_reap_process(process)
            raise OCRTimeoutError(
                f"Timed out after {self._settings.pipeline_ocr_timeout_seconds:.2f}s while running {purpose}."
            ) from exc
        except BaseException:
            await _kill_and_reap_process(process)
            raise

        if process.returncode != 0:
            rendered_stderr = stderr.decode("utf-8", errors="replace").strip()
            if rendered_stderr:
                raise OCRProcessingError(f"{purpose} failed: {rendered_stderr}")
            raise OCRProcessingError(f"{purpose} failed.")

        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OCRMalformedOutputError(f"{purpose} returned malformed JSON.") from exc

        return _parse_command_payload(payload, purpose=purpose)


async def _kill_and_reap_process(process: asyncio.subprocess.Process) -> None:
    """Kill a command child and bound reaping so shutdown cannot hang forever."""

    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()

    try:
        async with asyncio.timeout(_SUBPROCESS_REAP_TIMEOUT_SECONDS):
            await process.wait()
    except TimeoutError:
        logger.error(
            "ocr_subprocess_reap_timed_out",
            extra={"event": "ocr_subprocess_reap_timed_out"},
        )
    except asyncio.CancelledError:
        logger.warning(
            "ocr_subprocess_reap_cancelled",
            extra={"event": "ocr_subprocess_reap_cancelled"},
        )
    except Exception:
        logger.exception(
            "ocr_subprocess_reap_failed",
            extra={"event": "ocr_subprocess_reap_failed"},
        )


class FakeOCRProcessor:
    """Deterministic OCR provider used by containerized local E2E runs."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def extract_text(
        self,
        *,
        filename: str,
        mime_type: str,
        media_bytes: bytes,
        source_object_key: str,
    ) -> OCRExtractionResult:
        _ = (filename, mime_type, media_bytes)
        return OCRExtractionResult(
            engine="fake",
            fallback_engine=None,
            fallback_used=False,
            low_confidence=False,
            confidence=1.0,
            language=ContentLanguage.EN,
            extracted_text=self._settings.pipeline_fake_ocr_text,
            source_object_key=source_object_key,
        )


def build_pipeline_ocr_processor(
    *,
    settings: Settings | None = None,
    media_processor: PipelineMediaProcessorProtocol | None = None,
) -> OCRProcessorProtocol:
    """Return the configured OCR provider without changing live defaults."""

    resolved_settings = settings or get_settings()
    if resolved_settings.pipeline_ocr_provider_mode == "fake":
        return FakeOCRProcessor(settings=resolved_settings)
    return PipelineOCRProcessor(settings=resolved_settings, media_processor=media_processor)


def _parse_paddle_result(payload: object) -> _OCRCandidate:
    if not isinstance(payload, list) or not payload:
        raise OCRMalformedOutputError("PaddleOCR returned an empty result set.")

    first_result = payload[0]
    if isinstance(first_result, dict):
        resolved = first_result.get("res", first_result)
    else:
        resolved = getattr(first_result, "res", first_result)

    if not isinstance(resolved, dict):
        raise OCRMalformedOutputError("PaddleOCR returned an unexpected result shape.")

    rec_text = resolved.get("rec_text")
    if isinstance(rec_text, str):
        confidence = _normalize_confidence(resolved.get("rec_score"))
        return _OCRCandidate(extracted_text=rec_text, confidence=confidence, language=None)

    rec_texts = resolved.get("rec_texts")
    rec_scores = resolved.get("rec_scores")
    if not isinstance(rec_texts, list):
        raise OCRMalformedOutputError("PaddleOCR result is missing recognized text lines.")

    text_lines = tuple(str(item).strip() for item in rec_texts if str(item).strip())
    score_values = _coerce_score_list(rec_scores)
    average_score = (sum(score_values) / len(score_values)) if score_values else None
    return _OCRCandidate(
        extracted_text="\n".join(text_lines) if text_lines else None,
        confidence=average_score,
        language=None,
    )


def _parse_command_payload(payload: object, *, purpose: str) -> _OCRCandidate:
    if not isinstance(payload, dict):
        raise OCRMalformedOutputError(f"{purpose} returned malformed JSON.")

    if isinstance(payload.get("text"), str) or (payload.get("text") is None and "lines" not in payload):
        return _OCRCandidate(
            extracted_text=cast("str | None", payload.get("text")),
            confidence=_normalize_confidence(payload.get("confidence")),
            language=_coerce_language(payload.get("language")),
        )

    lines = payload.get("lines")
    if not isinstance(lines, list):
        raise OCRMalformedOutputError(f"{purpose} did not return text or lines.")

    extracted_lines: list[str] = []
    confidence_values: list[float] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        raw_text = line.get("text")
        if isinstance(raw_text, str) and raw_text.strip():
            extracted_lines.append(raw_text.strip())
        normalized_confidence = _normalize_confidence(line.get("confidence"))
        if normalized_confidence is not None:
            confidence_values.append(normalized_confidence)

    average_confidence = (sum(confidence_values) / len(confidence_values)) if confidence_values else None
    return _OCRCandidate(
        extracted_text="\n".join(extracted_lines) if extracted_lines else None,
        confidence=average_confidence,
        language=_coerce_language(payload.get("language")),
    )


def _render_ocr_command(command: str, image_path: Path, *, purpose: str) -> tuple[str, ...]:
    parts = shlex.split(command)
    if not parts:
        raise OCRProviderUnavailableError(f"{purpose} must not be blank.")
    rendered = [part.format(input=str(image_path)) for part in parts]
    if all("{input}" not in part for part in parts):
        rendered.append(str(image_path))
    return tuple(rendered)


def _coerce_score_list(scores: object) -> tuple[float, ...]:
    if not isinstance(scores, list):
        return ()
    normalized_scores: list[float] = []
    for score in scores:
        normalized_score = _normalize_confidence(score)
        if normalized_score is not None:
            normalized_scores.append(normalized_score)
    return tuple(normalized_scores)


def _normalize_confidence(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, int | float | str):
        return None
    try:
        resolved = float(value)
    except ValueError:
        return None
    return max(0.0, min(resolved, 1.0))


def _normalize_ocr_text(value: str | None) -> str | None:
    if value is None:
        return None
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return None
    return "\n".join(lines)


def _candidate_has_text(candidate: _OCRCandidate | None) -> bool:
    return candidate is not None and bool(_normalize_ocr_text(candidate.extracted_text))


def _candidate_is_low_confidence(candidate: _OCRCandidate | None, *, threshold: float) -> bool:
    if candidate is None:
        return True
    normalized_text = _normalize_ocr_text(candidate.extracted_text)
    if not normalized_text:
        return True
    if candidate.confidence is None:
        return True
    return candidate.confidence < threshold


def _candidate_score(candidate: _OCRCandidate | None) -> float:
    if candidate is None:
        return -1.0
    if not _normalize_ocr_text(candidate.extracted_text):
        return -1.0
    if candidate.confidence is None:
        return 0.0
    return candidate.confidence


def _coerce_language(value: object) -> ContentLanguage | None:
    if value is None:
        return None
    if isinstance(value, ContentLanguage):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in {"en", "eng", "english"}:
        return ContentLanguage.EN
    if normalized in {"ru", "rus", "russian"}:
        return ContentLanguage.RU
    if normalized in {"mixed", "multilingual"}:
        return ContentLanguage.MIXED
    if normalized in {"none", "unknown"}:
        return ContentLanguage.NONE
    return None


def _detect_language(text: str | None) -> ContentLanguage:
    if not text:
        return ContentLanguage.NONE

    latin_letters = sum(1 for char in text if "a" <= char.lower() <= "z")
    cyrillic_letters = sum(1 for char in text if "а" <= char.lower() <= "я" or char.lower() == "ё")
    if latin_letters and cyrillic_letters:
        return ContentLanguage.MIXED
    if cyrillic_letters:
        return ContentLanguage.RU
    if latin_letters:
        return ContentLanguage.EN
    return ContentLanguage.NONE


__all__ = [
    "FakeOCRProcessor",
    "OCRExtractionResult",
    "OCRMalformedOutputError",
    "OCRProcessingError",
    "OCRProcessorProtocol",
    "OCRProviderUnavailableError",
    "OCRTimeoutError",
    "PipelineOCRProcessor",
    "build_pipeline_ocr_processor",
]
