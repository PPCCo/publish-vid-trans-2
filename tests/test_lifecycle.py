"""End-to-end tests for the publish-vid-trans determinism spine.

These exercise the CLI package directly against a temp repo, verifying: project init,
state machine transitions, gate blocking, artifact registration + supersession +
approval auto-invalidation, per-language approvals, and rights gating.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import approvals, artifacts, project, rights, state  # noqa: E402
from video_translation_house.errors import StateTransitionError  # noqa: E402


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A minimal repo: real .claude config/schemas, empty projects dir."""
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "CLAUDE.md").write_text("test repo\n")
    shutil.copytree(REPO / ".claude" / "schemas", root / ".claude" / "schemas")
    shutil.copytree(REPO / ".claude" / "config", root / ".claude" / "config")
    (root / "projects").mkdir()
    return root


def _mkproject(root: Path):
    return project.init_project(
        root, "yt-testvideo01", url="https://youtube.com/watch?v=testvideo01",
        target_languages=["en", "ar"], audio_languages=["en"],
    )


def test_framework_validate(repo: Path):
    from video_translation_house.validation import validate_framework
    result = validate_framework(repo)
    assert result["valid"], [c for c in result["checks"] if c["status"] != "pass"]


def test_init_and_status(repo: Path):
    _mkproject(repo)
    st = project.project_status(repo, "yt-testvideo01")
    assert st["state"]["current_state"] == "INGEST"
    assert set(st["state"]["language_tracks"]) == {"en", "ar"}
    assert st["state"]["language_tracks"]["en"]["dub_enabled"] is True
    assert st["state"]["language_tracks"]["ar"]["dub_enabled"] is False


def test_invalid_transition_rejected(repo: Path):
    _mkproject(repo)
    with pytest.raises(StateTransitionError):
        state.transition(repo, "yt-testvideo01", "TRANSLATION", "agent")


def test_walk_to_transcript_gate(repo: Path):
    _mkproject(repo)
    state.transition(repo, "yt-testvideo01", "LANGUAGE_ID", "agent")
    state.transition(repo, "yt-testvideo01", "TRANSCRIPTION", "agent")
    state.transition(repo, "yt-testvideo01", "TRANSCRIPT_QA_GATE", "agent")
    # The gate edge to TRANSLATION is blocked until an approval exists.
    plan = state.plan(repo, "yt-testvideo01")
    assert plan["autonomy_action"] == "STOP_AT_GATE"
    assert plan["required_gate"] == "transcript_qa"
    with pytest.raises(StateTransitionError):
        state.transition(repo, "yt-testvideo01", "TRANSLATION", "agent")


def test_artifact_supersede_invalidates_approval(repo: Path):
    _mkproject(repo)
    paths_dir = repo / "projects" / "yt-testvideo01"
    f = paths_dir / "transcript" / "source.fa.json"
    f.write_text('{"v":1}')
    a1 = artifacts.register_artifact(repo, "yt-testvideo01", f, "transcript", "TRANSCRIPTION", "agent")
    appr = approvals.grant_approval(
        repo, "yt-testvideo01", "transcript_qa", "jane", [a1["sha256"]], "approved transcript",
    )
    assert approvals.approval_is_current(appr)
    # Edit the file and re-register: old approval must auto-invalidate.
    f.write_text('{"v":2}')
    a2 = artifacts.register_artifact(repo, "yt-testvideo01", f, "transcript", "TRANSCRIPTION", "agent")
    assert a2["supersedes"] == a1["artifact_id"]
    refreshed = approvals.list_approvals(repo, "yt-testvideo01")[0]
    assert refreshed["invalidated_at"] is not None


def test_per_language_approval_binding(repo: Path):
    _mkproject(repo)
    d = repo / "projects" / "yt-testvideo01" / "captions"
    (d).mkdir(parents=True, exist_ok=True)
    en = d / "captions.en.json"; en.write_text("{}")
    ar = d / "captions.ar.json"; ar.write_text("{}")
    a_en = artifacts.register_artifact(repo, "yt-testvideo01", en, "captions-json", "TRANSLATION", "agent", language="en")
    a_ar = artifacts.register_artifact(repo, "yt-testvideo01", ar, "captions-json", "TRANSLATION", "agent", language="ar")
    approvals.grant_approval(repo, "yt-testvideo01", "translation_qa", "jane", [a_en["sha256"]], "en ok", language="en")
    assert approvals.has_valid_approval(repo, "yt-testvideo01", "translation_qa", "en")
    assert not approvals.has_valid_approval(repo, "yt-testvideo01", "translation_qa", "ar")
    approvals.grant_approval(repo, "yt-testvideo01", "translation_qa", "jane", [a_ar["sha256"]], "ar ok", language="ar")
    assert approvals.has_valid_approval(repo, "yt-testvideo01", "translation_qa", "ar")


def test_rights_gate_blocks_packaging(repo: Path):
    _mkproject(repo)
    # Rights unreviewed by default → PACKAGE->READY_FOR_REVIEW blocked.
    blockers = state.transition_blockers(repo, "yt-testvideo01", "PACKAGE", "READY_FOR_REVIEW")
    assert any("rights_status" in b for b in blockers)
    rights.set_rights(repo, "yt-testvideo01", status="licensed", reviewer="jane")
    blockers2 = state.transition_blockers(repo, "yt-testvideo01", "PACKAGE", "READY_FOR_REVIEW")
    assert not any("rights_status" in b for b in blockers2)
