"""Tests for `project enable-dub`: turning on dubbing for tracks that already exist
(translate-only -> translate+dub) without re-translating.

Verifies it flips ``dub_enabled`` on existing tracks, extends config ``audio_languages``,
logs a ``DUB_ENABLED`` event, is idempotent, and refuses languages that have no track yet
(directing the caller to ``add-languages`` first).
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
from video_translation_house.util import atomic_write_json, load_json, load_yaml  # noqa: E402


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


VID = "yt-enabledub099"
URL = "https://youtube.com/watch?v=enabledub099"


def _state(root: Path) -> dict:
    return load_json(ProjectPaths(root, VID).state)


def _events(root: Path) -> list[dict]:
    return read_events(ProjectPaths(root, VID).events)


def test_enable_dub_flips_tracks_and_config(repo: Path):
    # en+fr translate-only (no audio) — dubbing off everywhere.
    project.init_project(repo, VID, url=URL, target_languages=["en", "fr"], audio_languages=[])
    assert _state(repo)["language_tracks"]["fr"].get("dub_enabled") is not True

    result = project.enable_dub(repo, VID, target_languages=["fr"])
    assert result["enabled"] == ["fr"]

    tracks = _state(repo)["language_tracks"]
    assert tracks["fr"]["dub_enabled"] is True
    assert tracks["en"].get("dub_enabled") is not True  # untouched

    cfg = load_yaml(ProjectPaths(repo, VID).config)
    assert "fr" in cfg["audio_languages"]

    kinds = [e["event"] for e in _events(repo)]
    assert "DUB_ENABLED" in kinds


def test_enable_dub_is_idempotent(repo: Path):
    project.init_project(repo, VID, url=URL, target_languages=["en", "fr"], audio_languages=["fr"])
    # fr already dub-enabled at init -> enabling it again is a no-op (no event, empty enabled).
    before = len(_events(repo))
    result = project.enable_dub(repo, VID, target_languages=["fr"])
    assert result["enabled"] == []
    assert len(_events(repo)) == before  # no DUB_ENABLED appended


def test_enable_dub_rejects_missing_track(repo: Path):
    project.init_project(repo, VID, url=URL, target_languages=["en"], audio_languages=[])
    with pytest.raises(ConfigurationError) as exc:
        project.enable_dub(repo, VID, target_languages=["es"])
    assert "add-languages" in str(exc.value)


def test_disable_dub_flips_off_and_config(repo: Path):
    project.init_project(repo, VID, url=URL, target_languages=["en", "fr"], audio_languages=["en", "fr"])
    assert _state(repo)["language_tracks"]["fr"]["dub_enabled"] is True

    result = project.enable_dub(repo, VID, target_languages=["fr"], disable=True)
    assert result["disabled"] == ["fr"]
    assert result["enabled"] is None

    tracks = _state(repo)["language_tracks"]
    assert tracks["fr"]["dub_enabled"] is False
    assert tracks["en"]["dub_enabled"] is True  # untouched

    cfg = load_yaml(ProjectPaths(repo, VID).config)
    assert "fr" not in cfg["audio_languages"]
    assert "en" in cfg["audio_languages"]

    assert "DUB_DISABLED" in [e["event"] for e in _events(repo)]


def test_disable_dub_is_idempotent(repo: Path):
    project.init_project(repo, VID, url=URL, target_languages=["en", "fr"], audio_languages=["en"])
    # fr already dub-disabled at init -> disabling it again is a no-op.
    before = len(_events(repo))
    result = project.enable_dub(repo, VID, target_languages=["fr"], disable=True)
    assert result["disabled"] == []
    assert len(_events(repo)) == before


def test_enable_then_disable_round_trips_config(repo: Path):
    project.init_project(repo, VID, url=URL, target_languages=["en", "fr"], audio_languages=[])
    project.enable_dub(repo, VID, target_languages=["fr"])
    assert "fr" in load_yaml(ProjectPaths(repo, VID).config)["audio_languages"]
    project.enable_dub(repo, VID, target_languages=["fr"], disable=True)
    assert "fr" not in load_yaml(ProjectPaths(repo, VID).config)["audio_languages"]


def test_disable_dub_refuses_stranding_artifact(repo: Path):
    project.init_project(repo, VID, url=URL, target_languages=["en", "fr"], audio_languages=["fr"])
    # Simulate a produced dub for fr: an active dub-wav@fr artifact in state.
    st = _state(repo)
    st.setdefault("active_artifacts", {})["dub-wav@fr"] = "sha256:" + "a" * 64
    atomic_write_json(ProjectPaths(repo, VID).state, st)

    with pytest.raises(ConfigurationError) as exc:
        project.enable_dub(repo, VID, target_languages=["fr"], disable=True)
    assert "force" in str(exc.value)
    # still enabled — refused before mutating.
    assert _state(repo)["language_tracks"]["fr"]["dub_enabled"] is True

    # --force overrides.
    result = project.enable_dub(repo, VID, target_languages=["fr"], disable=True, force=True)
    assert result["disabled"] == ["fr"]
    assert _state(repo)["language_tracks"]["fr"]["dub_enabled"] is False
