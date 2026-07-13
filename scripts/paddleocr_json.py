#!/usr/bin/env python3
"""Run PaddleOCR for one image and emit a small JSON result.

This script is intentionally independent from the main ``memexpert`` package so
the worker image can run it from a Python 3.13 venv while the app stays on
Python 3.14.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any, cast

PaddleOCRFactory = Callable[..., Any]
DEFAULT_CPU_THREADS = 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PaddleOCR and emit JSON.")
    parser.add_argument("--input", required=True, help="Path to the image to OCR.")
    parser.add_argument(
        "--cpu-threads",
        type=_positive_int,
        default=DEFAULT_CPU_THREADS,
        help="Positive Paddle inference CPU thread limit (default: 1).",
    )
    args = parser.parse_args()

    image_path = Path(args.input)
    if not image_path.is_file():
        print(f"Input image does not exist: {image_path}", file=sys.stderr)
        return 2

    paddle_ocr_factory = _load_paddle_ocr_factory()
    if paddle_ocr_factory is None:
        return 3

    try:
        ocr = paddle_ocr_factory(
            lang="ru",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            cpu_threads=args.cpu_threads,
        )
        result = ocr.predict(input=str(image_path))
    except Exception as exc:
        print(f"PaddleOCR prediction failed: {exc}", file=sys.stderr)
        return 4

    json.dump(_to_payload(result), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _load_paddle_ocr_factory() -> PaddleOCRFactory | None:
    try:
        paddleocr_module = import_module("paddleocr")
    except ImportError as exc:
        print(f"PaddleOCR import failed: {exc}", file=sys.stderr)
        return None

    paddle_ocr_factory = getattr(paddleocr_module, "PaddleOCR", None)
    if paddle_ocr_factory is None:
        print("PaddleOCR import failed: module 'paddleocr' does not expose PaddleOCR.", file=sys.stderr)
        return None
    if not callable(paddle_ocr_factory):
        print("PaddleOCR import failed: paddleocr.PaddleOCR is not callable.", file=sys.stderr)
        return None
    return cast("PaddleOCRFactory", paddle_ocr_factory)


def _positive_int(value: str) -> int:
    try:
        resolved = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if resolved <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return resolved


def _to_payload(result: object) -> dict[str, object]:
    text_lines, scores = _extract_lines_and_scores(result)
    text = "\n".join(text_lines) if text_lines else None
    confidence = (sum(scores) / len(scores)) if scores else None
    return {
        "engine": "paddleocr",
        "language": _detect_language(text),
        "text": text,
        "confidence": confidence,
        "lines": [
            {"text": text_line, "confidence": scores[index] if index < len(scores) else None}
            for index, text_line in enumerate(text_lines)
        ],
        "scores": scores,
    }


def _extract_lines_and_scores(result: object) -> tuple[list[str], list[float]]:
    if not isinstance(result, list):
        return ([], [])

    text_lines: list[str] = []
    scores: list[float] = []
    for item in result:
        resolved = _resolve_result_item(item)
        if not isinstance(resolved, dict):
            continue

        rec_text = resolved.get("rec_text")
        if isinstance(rec_text, str) and rec_text.strip():
            text_lines.append(rec_text.strip())
            score = _coerce_score(resolved.get("rec_score"))
            if score is not None:
                scores.append(score)
            continue

        rec_texts = resolved.get("rec_texts")
        if isinstance(rec_texts, list):
            text_lines.extend(str(text).strip() for text in rec_texts if str(text).strip())
            scores.extend(_coerce_scores(resolved.get("rec_scores")))

    return (text_lines, scores)


def _resolve_result_item(item: object) -> object:
    if isinstance(item, dict):
        return item.get("res", item)
    return getattr(item, "res", item)


def _coerce_scores(value: object) -> list[float]:
    if not isinstance(value, list):
        return []
    scores: list[float] = []
    for item in value:
        score = _coerce_score(item)
        if score is not None:
            scores.append(score)
    return scores


def _coerce_score(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, int | float | str):
        return None
    try:
        score = float(value)
    except ValueError:
        return None
    return max(0.0, min(score, 1.0))


def _detect_language(text: str | None) -> str:
    if not text:
        return "none"
    latin_letters = sum(1 for char in text if "a" <= char.lower() <= "z")
    cyrillic_letters = sum(1 for char in text if "а" <= char.lower() <= "я" or char.lower() == "ё")
    if latin_letters and cyrillic_letters:
        return "mixed"
    if cyrillic_letters:
        return "ru"
    if latin_letters:
        return "en"
    return "none"


if __name__ == "__main__":
    raise SystemExit(main())
