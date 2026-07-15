"""Tests for the one-time legacy RabbitMQ DLQ importer."""

from __future__ import annotations

from scripts.import_pipeline_dlq import _decode_body, build_parser, run


def test_import_pipeline_dlq_parser_uses_bounded_safe_default() -> None:
    args = build_parser().parse_args([])
    assert args.limit == 10_000
    assert args.queue is None


def test_import_pipeline_dlq_body_decoder_preserves_json_and_plain_text() -> None:
    assert _decode_body(b'{"event_type":"meme_ocr_done"}') == {"event_type": "meme_ocr_done"}
    assert _decode_body(b"not-json") == "not-json"


async def test_import_pipeline_dlq_rejects_non_positive_limit_before_connecting() -> None:
    assert await run(["--limit", "0"]) == 2
