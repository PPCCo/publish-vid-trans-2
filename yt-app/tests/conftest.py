"""Shared pytest fixtures for yt-app tests.

Everything here is PURE-LOGIC oriented: no ffmpeg, no engines, no network. Where a function
under test reads company/tools config, we point it at a **synthetic** repo root built in a
tmp dir — never the real (gitignored) ``company.local.json``.

``sys.path`` is wired to ``<repo>/.claude/scripts`` (via ``lib.env.bootstrap``) and to
``yt-app/`` so ``import lib.*`` and ``import video_translation_house.*`` both work.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# yt-app/ (parent of tests/) on the path so `import lib.*` works.
_YT_APP = Path(__file__).resolve().parent.parent
if str(_YT_APP) not in sys.path:
    sys.path.insert(0, str(_YT_APP))

from lib import env as _env  # noqa: E402

# Wire <repo>/.claude/scripts onto sys.path for `import video_translation_house.*`.
_env.bootstrap()


# A synthetic company config mirroring the shape the code reads (dubbing.clone_languages,
# quality_bars.audio.*, dubbing.voices/default_voice_gender). NOT the real company file.
_SYNTHETIC_COMPANY = {
    "dubbing": {
        "default_voice_gender": "male",
        "clone_languages": ["en", "zh", "ar", "es", "ru", "fr", "pt"],
        "voices": {
            "en": {"male": "/voices/en-male.onnx"},
            "ur": {"male": "/voices/ur-male.onnx"},
            # fa intentionally absent → no staged piper voice (dub fails loud).
        },
    },
    "quality_bars": {
        "audio": {
            "target_lufs": -16.0,
            "per_cue_drift_tolerance_ms": 150,
            "max_freeze_ms_per_cue": 4000,
            "freeze_trim_languages": ["ar"],
            "silent_span_max_ms": 7000,
        },
    },
}

_SYNTHETIC_TOOLS = {
    "tts": {"xtts_worker": {"clone_ref_trim_seconds": 25}},
}


@pytest.fixture
def synth_root(tmp_path: Path) -> Path:
    """A synthetic repo root with the .claude/CLAUDE.md marker + config the code reads."""
    claude = tmp_path / ".claude"
    (claude / "config").mkdir(parents=True)
    (claude / "CLAUDE.md").write_text("# synthetic test root\n", encoding="utf-8")
    (claude / "config" / "company.default.json").write_text(
        json.dumps(_SYNTHETIC_COMPANY), encoding="utf-8")
    (claude / "config" / "tools.default.json").write_text(
        json.dumps(_SYNTHETIC_TOOLS), encoding="utf-8")
    return tmp_path


def make_caption_doc(language: str, cues: list[dict]) -> dict:
    """A minimal caption doc for tests."""
    return {"language": language, "cues": cues}
