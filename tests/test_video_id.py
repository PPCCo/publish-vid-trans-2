"""Video-id validation must accept real YouTube ids, which are case-sensitive base64url
(e.g. `YP0FDR7Wc-8`). Lowercasing them would break the mapping back to the source video.
"""
from __future__ import annotations

import pytest
from video_translation_house.paths import is_valid_video_id


@pytest.mark.parametrize("vid", [
    "yt-YP0FDR7Wc-8",   # real mixed-case YouTube id (the regression case)
    "yt-abc12345678",   # README-style lowercase
    "yt-dQw4w9WgXcQ",   # mixed case
    "Speech_2024",
    "a1",
])
def test_valid_ids(vid):
    assert is_valid_video_id(vid)


@pytest.mark.parametrize("vid", [
    "-leading-dash",    # must start alphanumeric
    ".hidden",
    "has space",
    "has/slash",
    "x",                # too short (min 2)
    "",
])
def test_invalid_ids(vid):
    assert not is_valid_video_id(vid)
