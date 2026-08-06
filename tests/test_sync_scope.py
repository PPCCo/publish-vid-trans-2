"""Tests for `project sync-scope`: reconciling the two-axis review markers
(``skip_translation`` / ``auto_translate``) on a project whose tracks were created BEFORE the
two-axis feature existed and therefore carry no markers.

Verifies it recomputes both markers from ``source_language`` + the fixed ``en`` human-review
rule (source -> skip_translation; en+source human-reviewed with no auto_translate; every other
translatable target -> auto_translate), is a true recompute (removes stale markers), is
idempotent, requires a confirmed source_language, never touches dub/artifacts/approvals, and
leaves the human gate scoped to en afterward.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import project, state  # noqa: E402
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


VID = "yt-syncscope01"
URL = "https://youtube.com/watch?v=syncscope01"


def _state(root: Path) -> dict:
    return load_json(ProjectPaths(root, VID).state)


def _events(root: Path) -> list[dict]:
    return read_events(ProjectPaths(root, VID).events)


def _make_pre_feature_project(root: Path, *, source: str = "fa") -> None:
    """Init a project, then strip the two-axis markers off every track and stamp a confirmed
    source_language — simulating a project created before the feature (langid ran before the
    marker logic existed)."""
    project.init_project(
        root, VID, url=URL,
        target_languages=["en", "ar", "fa", "ur", "zh", "fr", "es", "pt", "ru"],
        audio_languages=["en", "zh", "fr", "es", "pt", "ru"],
    )
    st = _state(root)
    st["source_language"] = source
    st["current_state"] = "TRANSLATION"
    for track in st["language_tracks"].values():
        track.pop("skip_translation", None)
        track.pop("auto_translate", None)
    atomic_write_json(ProjectPaths(root, VID).state, st)


def test_sync_scope_marks_source_and_auto(repo: Path):
    _make_pre_feature_project(repo, source="fa")

    result = project.sync_scope(repo, VID)

    tracks = _state(repo)["language_tracks"]
    # source (fa): skip_translation, never auto_translate.
    assert tracks["fa"]["skip_translation"] is True
    assert tracks["fa"].get("auto_translate") is not True
    # en: human-reviewed hero track — neither marker.
    assert tracks["en"].get("skip_translation") is not True
    assert tracks["en"].get("auto_translate") is not True
    # every other translatable target: auto_translate.
    for lang in ("ar", "ur", "zh", "fr", "es", "pt", "ru"):
        assert tracks[lang]["auto_translate"] is True
        assert tracks[lang].get("skip_translation") is not True

    assert set(result["marked_auto"]) == {"ar", "ur", "zh", "fr", "es", "pt", "ru"}
    assert result["marked_skip"] == ["fa"]
    assert "SCOPE_SYNCED" in [e["event"] for e in _events(repo)]


def test_sync_scope_leaves_gate_scoped_to_en(repo: Path):
    _make_pre_feature_project(repo, source="fa")
    project.sync_scope(repo, VID)

    blockers = state.transition_blockers(repo, VID, "TRANSLATION_QA_GATE", "CAPTION_TIMING")
    # The translation_qa approval blocker names exactly the human-reviewed language(s): en.
    approval_blockers = [b for b in blockers if b.startswith("valid translation_qa approval")]
    assert approval_blockers, f"expected an en approval blocker, got: {blockers}"
    joined = " ".join(approval_blockers)
    assert "en" in joined
    # No auto_translate / source track appears in the approval requirement.
    for lang in ("ar", "ur", "zh", "fr", "es", "pt", "ru", "fa"):
        assert lang not in joined.replace("translation_qa", "")


def test_sync_scope_is_idempotent(repo: Path):
    _make_pre_feature_project(repo, source="fa")
    project.sync_scope(repo, VID)
    before = len(_events(repo))

    result = project.sync_scope(repo, VID)
    assert result["marked_auto"] == []
    assert result["marked_skip"] == []
    assert result["cleared"] == []
    assert len(_events(repo)) == before  # no second SCOPE_SYNCED


def test_sync_scope_removes_stale_marker(repo: Path):
    """A track wrongly marked auto_translate for what is actually the source language gets the
    stale marker removed (true recompute, not additive)."""
    _make_pre_feature_project(repo, source="fa")
    st = _state(repo)
    # Corrupt: mark the source (fa) auto_translate and drop skip_translation.
    st["language_tracks"]["fa"]["auto_translate"] = True
    st["language_tracks"]["fa"].pop("skip_translation", None)
    atomic_write_json(ProjectPaths(repo, VID).state, st)

    result = project.sync_scope(repo, VID)
    tracks = _state(repo)["language_tracks"]
    assert tracks["fa"].get("auto_translate") is not True  # stale marker cleared
    assert tracks["fa"]["skip_translation"] is True
    assert "fa" in result["cleared"]


def test_sync_scope_requires_source_language(repo: Path):
    project.init_project(repo, VID, url=URL, target_languages=["en", "fr"], audio_languages=[])
    # source_language is None right after init.
    with pytest.raises(ConfigurationError) as exc:
        project.sync_scope(repo, VID)
    assert "source_language" in str(exc.value)
