"""Tests for live OCR adapter boundaries."""

from __future__ import annotations

import os
import shlex
import sys
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import memexpert.core.ocr as ocr_module
from memexpert.core.config import Settings
from memexpert.core.ocr import OCRTimeoutError, PipelineOCRProcessor
from memexpert.models.enums import ContentLanguage

if TYPE_CHECKING:
    import uuid

    from memexpert.core.media import NormalizedMediaResult, UploadMediaDetails

ROOT = Path(__file__).resolve().parents[1]
LIVE_OCR_FIXTURE = ROOT / "tests" / "fixtures" / "ocr" / "ocr-russian-office-cat-meme.png"
EXPECTED_RUSSIAN_PHRASES = (
    "ЧЕМ В СВОБОДНОЕ ВРЕМЯ ЗАНИМАЕШЬСЯ",
    "Я ВСЁ ВРЕМЯ РАБОТАЮ",
    "ЗНАЧИТ У ТЕБЯ МНОГО ДЕНЕГ",
    "ПЛАЧЕТ",
)


class StaticPreviewMediaProcessor:
    def __init__(self, preview_frame_bytes: bytes) -> None:
        self._preview_frame_bytes = preview_frame_bytes

    async def inspect_upload(self, *, filename: str, content_type: str, media_bytes: bytes) -> UploadMediaDetails:
        _ = (filename, content_type, media_bytes)
        raise AssertionError("inspect_upload should not be called by OCR tests")

    async def normalize_for_web(
        self,
        *,
        meme_file_id: uuid.UUID,
        filename: str,
        content_type: str,
        media_bytes: bytes,
    ) -> NormalizedMediaResult:
        _ = (meme_file_id, filename, content_type, media_bytes)
        raise AssertionError("normalize_for_web should not be called by OCR tests")

    async def extract_preview_frame(self, *, filename: str, content_type: str, media_bytes: bytes) -> bytes:
        _ = (filename, content_type, media_bytes)
        return self._preview_frame_bytes


@pytest.mark.asyncio
async def test_paddle_command_is_primary_when_in_process_paddleocr_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper_script = tmp_path / "fake_paddleocr.py"
    helper_script.write_text(
        """
from __future__ import annotations

import json
import pathlib
import sys

image_path = pathlib.Path(sys.argv[sys.argv.index("--input") + 1])
image_path.read_bytes()
print(json.dumps({
    "text": "Чем занимаешься\\nПлачет",
    "confidence": 0.93,
    "language": "ru",
    "lines": [
        {"text": "Чем занимаешься", "confidence": 0.94},
        {"text": "Плачет", "confidence": 0.92},
    ],
}, ensure_ascii=False))
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(ocr_module, "import_module", _raise_import_error)
    paddle_command = f"{shlex.quote(sys.executable)} {shlex.quote(str(helper_script))} --input {{input}}"
    settings = Settings(
        pipeline_ocr_paddle_command=paddle_command,
        pipeline_ocr_timeout_seconds=5.0,
    )
    processor = PipelineOCRProcessor(
        settings=settings,
        media_processor=StaticPreviewMediaProcessor(b"fake-preview-bytes"),
    )

    result = await processor.extract_text(
        filename="meme.png",
        mime_type="image/png",
        media_bytes=b"original-bytes",
        source_object_key="pipeline/originals/meme.png",
    )

    assert result.engine == "paddleocr"
    assert result.fallback_engine is None
    assert result.fallback_used is False
    assert result.low_confidence is False
    assert result.confidence == pytest.approx(0.93)
    assert result.language is ContentLanguage.RU
    assert result.extracted_text == "Чем занимаешься\nПлачет"


@pytest.mark.asyncio
async def test_paddle_command_timeout_is_reported_as_ocr_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper_script = tmp_path / "slow_paddleocr.py"
    helper_script.write_text("import time\ntime.sleep(1)\n", encoding="utf-8")
    monkeypatch.setattr(ocr_module, "import_module", _raise_import_error)
    paddle_command = f"{shlex.quote(sys.executable)} {shlex.quote(str(helper_script))} --input {{input}}"
    settings = Settings(
        pipeline_ocr_paddle_command=paddle_command,
        pipeline_ocr_timeout_seconds=0.01,
    )
    processor = PipelineOCRProcessor(
        settings=settings,
        media_processor=StaticPreviewMediaProcessor(b"fake-preview-bytes"),
    )

    with pytest.raises(OCRTimeoutError):
        await processor.extract_text(
            filename="meme.png",
            mime_type="image/png",
            media_bytes=b"original-bytes",
            source_object_key="pipeline/originals/meme.png",
        )


@pytest.mark.asyncio
async def test_live_paddleocr_helper_recognizes_russian_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("MEMEXPERT_RUN_LIVE_OCR") != "1" and os.environ.get("MEMEXPERT_WORKER_IMAGE") != "1":
        pytest.skip("Set MEMEXPERT_RUN_LIVE_OCR=1 or run inside the worker image to execute live PaddleOCR.")
    if not LIVE_OCR_FIXTURE.is_file():
        pytest.skip(f"Live OCR fixture is missing: {LIVE_OCR_FIXTURE}")

    command = os.environ.get("PIPELINE_OCR_PADDLE_COMMAND")
    if command is None:
        helper_script = ROOT / "scripts" / "paddleocr_json.py"
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(helper_script))} --input {{input}}"

    monkeypatch.setattr(ocr_module, "import_module", _raise_import_error)
    settings = Settings(
        pipeline_ocr_paddle_command=command,
        pipeline_ocr_timeout_seconds=240.0,
        pipeline_ocr_low_confidence_threshold=0.0,
    )
    fixture_bytes = LIVE_OCR_FIXTURE.read_bytes()
    processor = PipelineOCRProcessor(
        settings=settings,
        media_processor=StaticPreviewMediaProcessor(fixture_bytes),
    )

    result = await processor.extract_text(
        filename=LIVE_OCR_FIXTURE.name,
        mime_type="image/png",
        media_bytes=fixture_bytes,
        source_object_key="tests/fixtures/ocr/ocr-russian-office-cat-meme.png",
    )

    assert result.fallback_used is False
    assert result.language in {ContentLanguage.RU, ContentLanguage.MIXED}
    normalized_text = _ocr_phrase_key(result.extracted_text or "")
    for phrase in EXPECTED_RUSSIAN_PHRASES:
        assert _ocr_phrase_key(phrase) in normalized_text


def _raise_import_error(_name: str) -> object:
    raise ImportError("PaddleOCR intentionally unavailable in this test")


def _ocr_phrase_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    chars = [char if char.isalnum() else " " for char in normalized]
    return " ".join("".join(chars).split())
