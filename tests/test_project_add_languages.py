"""Tests for project.add_languages — the sanctioned way to widen an in-progress project's
language set without discarding existing work.

Mirrors the per-file repo(tmp_path) fixture style used by test_project_reset.py. No engine
required: a project is created via init_project and languages are added straight on top.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import langid, project  # noqa: E402
from video_translation_house.errors import ConfigurationError  # noqa: E402
from video_translation_house.events import read_events  # noqa: E402
from video_translation_house.paths import ProjectPaths  # noqa: E402
from video_translation_house.util import load_json, load_yaml  # noqa: E402


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


VID = "yt-addlang00099"
URL = "https://youtube.com/watch?v=addlang00099"


def _init(root: Path, targets, audio=None) -> None:
    project.init_project(root, VID, url=URL, target_languages=targets, audio_languages=audio)


def _state(root: Path) -> dict:
    return load_json(ProjectPaths(root, VID).state)


def test_adds_new_tracks_and_preserves_existing(repo: Path):
    _init(repo, ["en", "fa"], audio=["en"])
    before = _state(repo)
    en_before = dict(before["language_tracks"]["en"])
    fa_before = dict(before["language_tracks"]["fa"])

    result = project.add_languages(repo, VID, target_languages=["zh", "fr"])

    assert result["added"] == ["zh", "fr"]
    st = _state(repo)
    assert st["target_languages"] == ["en", "fa", "zh", "fr"]
    for lang in ("zh", "fr"):
        t = st["language_tracks"][lang]
        assert t["stage"] == "TRANSLATION"
        assert t["status"] == "pending"
        assert t["dub_enabled"] is True  # default: dub every added language
    # existing tracks untouched
    assert st["language_tracks"]["en"] == en_before
    assert st["language_tracks"]["fa"] == fa_before


def test_dub_subset_honored(repo: Path):
    _init(repo, ["en"])
    project.add_languages(repo, VID, target_languages=["zh", "fr"], audio_languages=["zh"])
    st = _state(repo)
    assert st["language_tracks"]["zh"]["dub_enabled"] is True
    assert st["language_tracks"]["fr"]["dub_enabled"] is False


def test_idempotent_noop_when_already_present(repo: Path):
    _init(repo, ["en", "fa"])
    events_before = len(read_events(ProjectPaths(repo, VID).events))
    state_before = _state(repo)

    result = project.add_languages(repo, VID, target_languages=["en", "fa"])

    assert result["added"] == []
    assert _state(repo) == state_before  # no mutation
    assert len(read_events(ProjectPaths(repo, VID).events)) == events_before  # no event


def test_partial_add_only_adds_the_new_ones(repo: Path):
    _init(repo, ["en"])
    result = project.add_languages(repo, VID, target_languages=["en", "zh"])
    assert result["added"] == ["zh"]
    assert _state(repo)["target_languages"] == ["en", "zh"]


def test_unknown_code_rejected_and_state_unchanged(repo: Path):
    # normalize_language accepts any short bare ISO-like code (<=3 alpha) but rejects free
    # text — mirror that boundary so add-languages is no looser than langid's contract.
    _init(repo, ["en"])
    state_before = _state(repo)
    with pytest.raises(ConfigurationError):
        project.add_languages(repo, VID, target_languages=["klingon"])
    assert _state(repo) == state_before


def test_audio_not_subset_of_added_rejected(repo: Path):
    _init(repo, ["en"])
    with pytest.raises(ConfigurationError):
        # dubbing 'en' (already present, not being added) is not allowed via --audio here
        project.add_languages(repo, VID, target_languages=["zh"], audio_languages=["en"])


def test_source_language_add_is_marked_skip_translation(repo: Path):
    _init(repo, ["en"])
    # confirm a source language (fa) not yet a target
    langid.set_language(repo, VID, "fa", source="manual", confidence=0.9)
    project.add_languages(repo, VID, target_languages=["fa", "zh"])
    st = _state(repo)
    assert st["language_tracks"]["fa"]["skip_translation"] is True  # rule 7
    assert "skip_translation" not in st["language_tracks"]["zh"]


def test_config_stays_consistent_and_valid(repo: Path):
    _init(repo, ["en"], audio=["en"])
    project.add_languages(repo, VID, target_languages=["zh", "fr"], audio_languages=["zh"])
    cfg = load_yaml(ProjectPaths(repo, VID).config)
    assert cfg["target_languages"] == ["en", "zh", "fr"]
    assert cfg["audio_languages"] == ["en", "zh"]
    # whole project still validates against the schemas
    assert project.validate_project(repo, VID)["valid"] is True


def test_languages_added_event_appended(repo: Path):
    _init(repo, ["en"])
    project.add_languages(repo, VID, target_languages=["zh", "fr"], actor="Qaiser Abbas")
    events = read_events(ProjectPaths(repo, VID).events)
    added = [e for e in events if e["event"] == "LANGUAGES_ADDED"]
    assert len(added) == 1
    assert added[0]["actor"] == "Qaiser Abbas"
    assert added[0]["details"]["added"] == ["zh", "fr"]
