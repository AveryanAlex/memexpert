"""Tests for live OCR adapter boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

import memexpert.core.ocr as ocr_module
from memexpert.core.config import Settings
from memexpert.core.ocr import OCRTimeoutError, PipelineOCRProcessor
from memexpert.models.enums import ContentLanguage
from scripts import paddleocr_json

if TYPE_CHECKING:
    import uuid

    from memexpert.media.contracts import NormalizedMediaResult, UploadMediaDetails

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
        generation_id: uuid.UUID | None = None,
    ) -> NormalizedMediaResult:
        _ = (meme_file_id, filename, content_type, media_bytes, generation_id)
        raise AssertionError("normalize_for_web should not be called by OCR tests")

    async def extract_preview_frame(self, *, filename: str, content_type: str, media_bytes: bytes) -> bytes:
        _ = (filename, content_type, media_bytes)
        return self._preview_frame_bytes


class PendingSubprocess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.communicate_started = asyncio.Event()
        self._communicate_release = asyncio.Event()
        self.kill_calls = 0
        self.wait_calls = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_started.set()
        await self._communicate_release.wait()
        return b"", b""

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.wait_calls += 1
        self.returncode = -9
        return self.returncode


class UnreapableSubprocess(PendingSubprocess):
    async def wait(self) -> int:
        self.wait_calls += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_in_process_paddleocr_caps_inference_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_kwargs: dict[str, object] = {}

    class FakePaddleOCR:
        def __init__(self, **kwargs: object) -> None:
            constructor_kwargs.update(kwargs)

        def predict(self, *, input: str) -> list[dict[str, object]]:
            _ = input
            return [{"rec_texts": ["Thread safe"], "rec_scores": [0.95]}]

    monkeypatch.setattr(
        ocr_module,
        "import_module",
        lambda _name: SimpleNamespace(PaddleOCR=FakePaddleOCR),
    )
    processor = PipelineOCRProcessor(
        settings=Settings(),
        media_processor=StaticPreviewMediaProcessor(b"fake-preview-bytes"),
    )

    result = await processor.extract_text(
        filename="meme.png",
        mime_type="image/png",
        media_bytes=b"original-bytes",
        source_object_key="pipeline/originals/meme.png",
    )

    assert constructor_kwargs["cpu_threads"] == 1
    assert result.extracted_text == "Thread safe"


@pytest.mark.parametrize(
    ("cpu_thread_args", "expected_cpu_threads"),
    [
        ((), 1),
        (("--cpu-threads", "3"), 3),
    ],
)
def test_paddleocr_helper_caps_and_allows_overriding_inference_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cpu_thread_args: tuple[str, ...],
    expected_cpu_threads: int,
) -> None:
    image_path = tmp_path / "preview.png"
    image_path.write_bytes(b"fake-preview-bytes")
    constructor_kwargs: dict[str, object] = {}

    class FakePaddleOCR:
        def predict(self, *, input: str) -> list[dict[str, object]]:
            assert input == str(image_path)
            return [{"rec_texts": ["Helper safe"], "rec_scores": [0.9]}]

    def build_fake_paddleocr(**kwargs: object) -> FakePaddleOCR:
        constructor_kwargs.update(kwargs)
        return FakePaddleOCR()

    monkeypatch.setattr(paddleocr_json, "_load_paddle_ocr_factory", lambda: build_fake_paddleocr)
    monkeypatch.setattr(
        sys,
        "argv",
        ["paddleocr_json.py", "--input", str(image_path), *cpu_thread_args],
    )

    assert paddleocr_json.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert constructor_kwargs["cpu_threads"] == expected_cpu_threads
    assert payload["text"] == "Helper safe"


@pytest.mark.asyncio
async def test_paddle_command_is_preferred_when_in_process_paddleocr_is_unavailable(
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
async def test_ocr_command_timeout_kills_and_reaps_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = PendingSubprocess()

    async def create_subprocess(*_args: object, **_kwargs: object) -> PendingSubprocess:
        return process

    monkeypatch.setattr(ocr_module.asyncio, "create_subprocess_exec", create_subprocess)
    processor = PipelineOCRProcessor(
        settings=Settings(pipeline_ocr_timeout_seconds=0.01),
        media_processor=StaticPreviewMediaProcessor(b"fake-preview-bytes"),
    )

    with pytest.raises(OCRTimeoutError):
        await processor._run_ocr_command(
            command="fake-ocr {input}",
            image_path=tmp_path / "preview.png",
            purpose="test OCR command",
        )

    assert process.kill_calls == 1
    assert process.wait_calls == 1
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_ocr_command_cancellation_kills_and_reaps_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = PendingSubprocess()

    async def create_subprocess(*_args: object, **_kwargs: object) -> PendingSubprocess:
        return process

    monkeypatch.setattr(ocr_module.asyncio, "create_subprocess_exec", create_subprocess)
    processor = PipelineOCRProcessor(
        settings=Settings(pipeline_ocr_timeout_seconds=5.0),
        media_processor=StaticPreviewMediaProcessor(b"fake-preview-bytes"),
    )
    command_task = asyncio.create_task(
        processor._run_ocr_command(
            command="fake-ocr {input}",
            image_path=tmp_path / "preview.png",
            purpose="test OCR command",
        )
    )
    await asyncio.wait_for(process.communicate_started.wait(), timeout=1.0)

    command_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command_task

    assert process.kill_calls == 1
    assert process.wait_calls == 1
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_ocr_command_cancellation_bounds_reaping_an_unresponsive_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = UnreapableSubprocess()

    async def create_subprocess(*_args: object, **_kwargs: object) -> UnreapableSubprocess:
        return process

    monkeypatch.setattr(ocr_module.asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(ocr_module, "_SUBPROCESS_REAP_TIMEOUT_SECONDS", 0.01)
    processor = PipelineOCRProcessor(
        settings=Settings(pipeline_ocr_timeout_seconds=5.0),
        media_processor=StaticPreviewMediaProcessor(b"fake-preview-bytes"),
    )
    command_task = asyncio.create_task(
        processor._run_ocr_command(
            command="fake-ocr {input}",
            image_path=tmp_path / "preview.png",
            purpose="test OCR command",
        )
    )
    await asyncio.wait_for(process.communicate_started.wait(), timeout=1.0)

    command_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(command_task, timeout=0.2)

    assert process.kill_calls == 1
    assert process.wait_calls == 1


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
