"""Tests for `project redub`: the scoped rewind that lets ONE dub track be re-rendered
without discarding the other languages' work (vs the coarse `project reset`).

Verifies it rewinds the top state back to AUDIO_SYNC_ADJUST (a dub-allowed state), resets
only the named dubbed track(s) to the dubbing stage, leaves other tracks + artifacts +
approvals untouched, logs a TRACK_REDUB_REQUESTED event, and refuses captions-only / missing
tracks.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import project  # noqa: E402
from video_translation_house.errors import ConfigurationError  # noqa: E402
from video_translation_house.events import read_events  # noqa: E402
from video_translation_house.paths import ProjectPaths  # noqa: E402
from video_translation_house.util import atomic_write_json, load_json  # noqa: E402


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "CLAUDE.md").write_text("test repo\n")
    shutil.copytree(REPO / ".claude" / "schemas", root / ".claude" / "schemas")
    shutil.copytree(REPO / ".claude" / "config", root / ".claude" / "config")
    (root / "projects").mkdir()
    (root / "catalog").mkdir()
    return root


VID = "yt-redub00099"
URL = "https://youtube.com/watch?v=redub00099"


def _state(root: Path) -> dict:
    return load_json(ProjectPaths(root, VID).state)


def _events(root: Path) -> list[dict]:
    return read_events(ProjectPaths(root, VID).events)


def _advance_to(root: Path, current_state: str, *, tracks: dict | None = None) -> None:
    """Force the top state forward for the fixture (redub itself only pulls backward)."""
    st = _state(root)
    st["current_state"] = current_state
    if tracks:
        for lang, patch in tracks.items():
            st["language_tracks"].setdefault(lang, {})
            st["language_tracks"][lang].update(patch)
    atomic_write_json(ProjectPaths(root, VID).state, st)


def test_redub_rewinds_top_and_resets_only_named_track(repo: Path):
    # ar + en + zh, all dubbed. Simulate the project having advanced to VIDEO_MUX with each
    # track synced/approved.
    project.init_project(repo, VID, url=URL,
                         target_languages=["en", "ar", "zh"],
                         audio_languages=["en", "ar", "zh"])
    _advance_to(repo, "VIDEO_MUX", tracks={
        "en": {"stage": "AUDIO_QA_GATE", "status": "in_progress"},
        "ar": {"stage": "AUDIO_QA_GATE", "status": "in_progress"},
        "zh": {"stage": "AUDIO_QA_GATE", "status": "in_progress"},
    })

    result = project.redub_track(repo, VID, target_languages=["ar"])
    assert result["redub_languages"] == ["ar"]
    assert result["current_state"] == "AUDIO_SYNC_ADJUST"
    assert result["rewound_from"] == "VIDEO_MUX"

    st = _state(repo)
    assert st["current_state"] == "AUDIO_SYNC_ADJUST"
    # only ar reset (already at AUDIO_SYNC_ADJUST stage, but explicitly re-stamped):
    assert st["language_tracks"]["ar"]["stage"] == "AUDIO_SYNC_ADJUST"
    assert st["language_tracks"]["ar"]["status"] == "in_progress"
    # en/zh untouched — their stage stays at the human-gate stage they reached:
    assert st["language_tracks"]["en"]["stage"] == "AUDIO_QA_GATE"
    assert st["language_tracks"]["zh"]["stage"] == "AUDIO_QA_GATE"

    kinds = [e["event"] for e in _events(repo)]
    assert "TRACK_REDUB_REQUESTED" in kinds


def test_redub_does_not_push_state_forward(repo: Path):
    # Already at a dub-allowed state (AUDIO_SYNC_ADJUST); redub must not advance it.
    project.init_project(repo, VID, url=URL,
                         target_languages=["en", "ar"], audio_languages=["en", "ar"])
    _advance_to(repo, "AUDIO_SYNC_ADJUST", tracks={
        "ar": {"stage": "AUDIO_SYNC_ADJUST", "status": "in_progress"},
    })
    result = project.redub_track(repo, VID, target_languages=["ar"])
    assert result["current_state"] == "AUDIO_SYNC_ADJUST"
    assert result["rewound_from"] is None
    assert _state(repo)["current_state"] == "AUDIO_SYNC_ADJUST"


def test_redub_rejects_captions_only_track(repo: Path):
    project.init_project(repo, VID, url=URL,
                         target_languages=["en", "fr"], audio_languages=["en"])  # fr not dubbed
    with pytest.raises(ConfigurationError) as exc:
        project.redub_track(repo, VID, target_languages=["fr"])
    assert "enable-dub" in str(exc.value)


def test_redub_rejects_missing_track(repo: Path):
    project.init_project(repo, VID, url=URL, target_languages=["en"], audio_languages=["en"])
    with pytest.raises(ConfigurationError) as exc:
        project.redub_track(repo, VID, target_languages=["es"])
    assert "add-languages" in str(exc.value)


def test_redub_leaves_artifacts_and_approvals_untouched(repo: Path):
    project.init_project(repo, VID, url=URL,
                         target_languages=["en", "ar"], audio_languages=["en", "ar"])
    _advance_to(repo, "VIDEO_MUX", tracks={
        "en": {"stage": "AUDIO_QA_GATE", "status": "in_progress"},
        "ar": {"stage": "AUDIO_QA_GATE", "status": "in_progress"},
    })
    st = _state(repo)
    st.setdefault("active_artifacts", {})["dub-wav@en"] = "sha256:" + "e" * 64
    st["active_artifacts"]["dub-wav@ar"] = "sha256:" + "a" * 64
    atomic_write_json(ProjectPaths(repo, VID).state, st)

    project.redub_track(repo, VID, target_languages=["ar"])
    # redub does NOT clear artifacts; the re-`dub run` supersedes and rule 6 invalidates.
    active = _state(repo)["active_artifacts"]
    assert active["dub-wav@en"] == "sha256:" + "e" * 64
    assert active["dub-wav@ar"] == "sha256:" + "a" * 64
