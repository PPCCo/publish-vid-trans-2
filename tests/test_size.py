"""`size` verb: compute full 16:9 (or given aspect) YouTube dimensions from a single given
width or height. Even integers (H.264 needs even dims). Pure math — no ffmpeg."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import size  # noqa: E402
from video_translation_house.errors import VideoTranslationHouseError as VThError  # noqa: E402


def test_width_gives_full_169():
    out = size.compute_dimensions("w", 1920)
    assert (out["width"], out["height"]) == (1920, 1080)
    assert out["label"] == "1920x1080"
    assert out["aspect"] == "16:9"


def test_height_gives_full_169():
    out = size.compute_dimensions("h", 1080)
    assert (out["width"], out["height"]) == (1920, 1080)


def test_width_alias_and_even_rounding():
    # 1042 wide @16:9 => 586.125 -> even-rounded to 586.
    out = size.compute_dimensions("width", 1042)
    assert out["width"] == 1042
    assert out["height"] % 2 == 0
    assert out["height"] == 586


def test_height_odd_input_snaps_even():
    out = size.compute_dimensions("h", 583)
    assert out["height"] % 2 == 0
    assert out["width"] % 2 == 0


def test_custom_aspect_square():
    out = size.compute_dimensions("w", 1080, aspect="1:1")
    assert (out["width"], out["height"]) == (1080, 1080)


def test_rejects_nonpositive():
    with pytest.raises(VThError):
        size.compute_dimensions("w", 0)


def test_rejects_bad_axis():
    with pytest.raises(VThError):
        size.compute_dimensions("x", 100)


def test_rejects_bad_aspect():
    with pytest.raises(VThError):
        size.compute_dimensions("w", 100, aspect="16-9")
