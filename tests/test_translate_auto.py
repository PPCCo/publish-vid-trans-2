"""Tests for the two-axis translate/dub scope: only the SOURCE language and English get a
human-filled worksheet + a per-language translation_qa human gate; every other translatable
target is AI-auto-translated (marked ``auto_translate``) with deterministic QA only and no
human gate.

Verifies the ``auto_translate`` marker is set at track creation (init + add-languages) and that
state.transition_blockers demands a translation_qa approval ONLY for source+en tracks.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import langid, project, state  # noqa: E402
from video_translation_house.paths import ProjectPaths  # noqa: E402
from video_translation_house.util import load_json  # noqa: E402


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


VID = "yt-autotrans099"
URL = "https://youtube.com/watch?v=autotrans099"


def _state(root: Path) -> dict:
    return load_json(ProjectPaths(root, VID).state)


def test_init_marks_non_en_targets_auto_translate(repo: Path):
    project.init_project(repo, VID, url=URL, target_languages=["en", "fr", "es"])
    tracks = _state(repo)["language_tracks"]
    # English is human-reviewed — no auto_translate marker.
    assert "auto_translate" not in tracks["en"]
    # Every other target is AI-auto-translated.
    assert tracks["fr"]["auto_translate"] is True
    assert tracks["es"]["auto_translate"] is True


def test_source_language_is_skip_not_auto(repo: Path):
    project.init_project(repo, VID, url=URL, target_languages=["en", "fa", "fr"])
    # Confirm fa as the source language after init (as langid would at LANGUAGE_ID).
    langid.set_language(repo, VID, "fa", source="manual", confidence=0.9)
    tracks = _state(repo)["language_tracks"]
    assert tracks["fa"]["skip_translation"] is True
    # The source track is never "auto_translated" — it skips translation entirely (rule 7).
    assert tracks["fa"].get("auto_translate") is not True
    assert tracks["en"].get("auto_translate") is not True
    assert tracks["fr"]["auto_translate"] is True


def test_add_languages_marks_auto_translate(repo: Path):
    project.init_project(repo, VID, url=URL, target_languages=["en"])
    langid.set_language(repo, VID, "fa", source="manual", confidence=0.9)
    project.add_languages(repo, VID, target_languages=["fr", "es", "fa"])
    tracks = _state(repo)["language_tracks"]
    assert tracks["fr"]["auto_translate"] is True
    assert tracks["es"]["auto_translate"] is True
    # fa added == source language → skip_translation, not auto_translate.
    assert tracks["fa"]["skip_translation"] is True
    assert tracks["fa"].get("auto_translate") is not True


def _reach_translation_qa_gate(root: Path) -> None:
    """Put the project at TRANSLATION_QA_GATE with every track past the translation stage so the
    only remaining question is which languages demand a HUMAN approval. We drive state directly
    through the sanctioned load->mutate->write path used by the CLI (no hand-authored files):
    mark each translatable track TRANSLATION_QA_GATE and set current_state accordingly."""
    from video_translation_house.util import atomic_write_json, utc_now
    paths = ProjectPaths(root, VID)
    st = load_json(paths.state)
    st["previous_state"] = "TRANSLATION"
    st["current_state"] = "TRANSLATION_QA_GATE"
    for _lang, t in st["language_tracks"].items():
        if not t.get("skip_translation"):
            t["stage"] = "TRANSLATION_QA_GATE"
            t["status"] = "in_progress"
            t["updated_at"] = utc_now()
    atomic_write_json(paths.state, st)


def test_gate_demands_approval_only_for_source_and_en(repo: Path):
    project.init_project(repo, VID, url=URL, target_languages=["en", "fr", "es"])
    langid.set_language(repo, VID, "fa", source="manual", confidence=0.9)
    _reach_translation_qa_gate(repo)

    blockers = state.transition_blockers(repo, VID, "TRANSLATION_QA_GATE", "CAPTION_TIMING")
    approval_blockers = [b for b in blockers if "translation_qa approval required" in b]
    assert len(approval_blockers) == 1, approval_blockers
    # Only en is demanded; fr/es (auto) and fa (skip) are NOT.
    assert "en" in approval_blockers[0]
    assert "fr" not in approval_blockers[0]
    assert "es" not in approval_blockers[0]
    assert "fa" not in approval_blockers[0]
