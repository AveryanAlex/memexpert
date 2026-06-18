"""Focused import-boundary tests for API-safe ingest startup."""

from __future__ import annotations

import importlib
import sys

import pytest


def test_fastapi_app_imports_without_worker_media_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    _forget_modules(monkeypatch, ("memexpert.api", "memexpert.core", "memexpert.media", "memexpert.services"))
    _block_worker_media_imports(monkeypatch)

    app_module = importlib.import_module("memexpert.api.app")
    app = app_module.create_app()

    assert app.title == "MemeXpert API"


def test_core_media_contract_shim_imports_without_worker_media_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    _forget_modules(monkeypatch, ("memexpert.core", "memexpert.media"))
    _block_worker_media_imports(monkeypatch)

    media_module = importlib.import_module("memexpert.core.media")

    assert media_module.UploadMediaDetails.__name__ == "UploadMediaDetails"
    assert media_module.NormalizedMediaResult.__name__ == "NormalizedMediaResult"


@pytest.mark.asyncio
async def test_fake_text_voyage_embedding_does_not_require_pillow(monkeypatch: pytest.MonkeyPatch) -> None:
    _forget_modules(monkeypatch, ("memexpert.core",))
    _block_worker_media_imports(monkeypatch)

    config_module = importlib.import_module("memexpert.core.config")
    voyage_module = importlib.import_module("memexpert.core.voyage")
    client = voyage_module.FakeVoyageClient(
        settings=config_module.Settings(
            pipeline_voyage_provider_mode="fake",
            pipeline_voyage_output_dimensions=4,
        )
    )

    result = await client.embed_text(text="cat query")

    assert result.vector == (1.0, 0.0, 0.0, 0.0)


def test_worker_media_implementation_imports_with_installed_dependencies() -> None:
    inspect_module = importlib.import_module("memexpert.media.inspect")

    assert inspect_module.PipelineMediaProcessor.__name__ == "PipelineMediaProcessor"


def _forget_modules(monkeypatch: pytest.MonkeyPatch, prefixes: tuple[str, ...]) -> None:
    normalized_prefixes = tuple(prefixes)
    for module_name in tuple(sys.modules):
        if any(module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in normalized_prefixes):
            monkeypatch.delitem(sys.modules, module_name, raising=False)


def _block_worker_media_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    _forget_modules(monkeypatch, ("PIL", "imagehash"))
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    monkeypatch.setitem(sys.modules, "imagehash", None)
