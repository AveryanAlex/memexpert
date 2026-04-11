"""Pure blur-hash encoder used by the pipeline media processor.

Extracted out of :mod:`memexpert.core.media` so the encoder can be unit-tested
in isolation without pulling in FFmpeg/Pillow inspection helpers. The math is
the standard Wolt blur-hash algorithm, tuned here for the project defaults
(4x3 components, sRGB ↔ linear conversions). Nothing in this module touches
the filesystem, subprocess, or any pipeline state.
"""

from __future__ import annotations

import math
from typing import cast

from PIL import Image

_BLURHASH_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#$%*+,-.:;=?@[]^_{|}~"
_DEFAULT_BLURHASH_COMPONENTS_X = 4
_DEFAULT_BLURHASH_COMPONENTS_Y = 3


def encode_blur_hash(image: Image.Image) -> str | None:
    """Encode ``image`` into a Wolt blur-hash string (or ``None`` if invalid).

    The caller is expected to pass a reasonably small preview frame; this
    function resizes to 32x32 before running the discrete cosine transform
    but still spends O(32*32*components) per call, so it must be scheduled
    off the event loop (see ``asyncio.to_thread`` in the pipeline media
    processor). Returning ``None`` means the source image had a zero-sized
    dimension after resize — no blur hash is produced in that case.
    """

    resized = image.convert("RGB").resize((32, 32), Image.Resampling.LANCZOS)
    width, height = resized.size
    if width <= 0 or height <= 0:
        return None

    factors: list[tuple[float, float, float]] = []
    for component_y in range(_DEFAULT_BLURHASH_COMPONENTS_Y):
        for component_x in range(_DEFAULT_BLURHASH_COMPONENTS_X):
            normalization = 1.0 if component_x == 0 and component_y == 0 else 2.0
            red = 0.0
            green = 0.0
            blue = 0.0
            for y in range(height):
                for x in range(width):
                    basis = normalization * math.cos(math.pi * component_x * x / width) * math.cos(
                        math.pi * component_y * y / height
                    )
                    pixel_red, pixel_green, pixel_blue = cast(
                        "tuple[int, int, int]",
                        resized.getpixel((x, y)),
                    )
                    red += basis * _srgb_to_linear(pixel_red)
                    green += basis * _srgb_to_linear(pixel_green)
                    blue += basis * _srgb_to_linear(pixel_blue)
            scale = 1.0 / float(width * height)
            factors.append((red * scale, green * scale, blue * scale))

    dc = factors[0]
    ac = factors[1:]
    maximum_value = max((max(abs(r), abs(g), abs(b)) for r, g, b in ac), default=0.0)
    quantized_maximum_value = max(0, min(82, int(math.floor((maximum_value * 166.0) - 0.5))))
    actual_maximum_value = (quantized_maximum_value + 1) / 166.0

    encoded = [
        _encode_base83((_DEFAULT_BLURHASH_COMPONENTS_X - 1) + ((_DEFAULT_BLURHASH_COMPONENTS_Y - 1) * 9), 1),
        _encode_base83(quantized_maximum_value, 1),
        _encode_base83(_encode_dc(dc), 4),
    ]
    encoded.extend(_encode_base83(_encode_ac(factor, actual_maximum_value), 2) for factor in ac)
    return "".join(encoded)


def _encode_dc(value: tuple[float, float, float]) -> int:
    red = _linear_to_srgb(value[0])
    green = _linear_to_srgb(value[1])
    blue = _linear_to_srgb(value[2])
    return (red << 16) + (green << 8) + blue


def _encode_ac(value: tuple[float, float, float], maximum_value: float) -> int:
    if maximum_value <= 0.0:
        return 9 * 19 * 19 + 9 * 19 + 9
    quantized_red = int(max(0, min(18, math.floor((_sign_pow(value[0] / maximum_value, 0.5) * 9) + 9.5))))
    quantized_green = int(max(0, min(18, math.floor((_sign_pow(value[1] / maximum_value, 0.5) * 9) + 9.5))))
    quantized_blue = int(max(0, min(18, math.floor((_sign_pow(value[2] / maximum_value, 0.5) * 9) + 9.5))))
    return (quantized_red * 19 * 19) + (quantized_green * 19) + quantized_blue


def _encode_base83(value: int, length: int) -> str:
    encoded = []
    divisor = 1
    for _ in range(length - 1):
        divisor *= 83
    for _ in range(length):
        digit = (value // divisor) % 83
        encoded.append(_BLURHASH_ALPHABET[digit])
        divisor //= 83
    return "".join(encoded)


def _srgb_to_linear(value: int) -> float:
    normalized = value / 255.0
    if normalized <= 0.04045:
        return normalized / 12.92
    return float(((normalized + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(value: float) -> int:
    clamped = max(0.0, min(value, 1.0))
    if clamped <= 0.0031308:
        return int((clamped * 12.92 * 255.0) + 0.5)
    return int((((1.055 * (clamped ** (1.0 / 2.4))) - 0.055) * 255.0) + 0.5)


def _sign_pow(value: float, exponent: float) -> float:
    return math.copysign(abs(value) ** exponent, value)


__all__ = ["encode_blur_hash"]
