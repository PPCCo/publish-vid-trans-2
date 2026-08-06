"""Tests for the sanctioned teardown path: project.reset_project / delete_project and
catalog.remove_entry.

Mirrors the per-file `repo(tmp_path)` fixture + `_project_at_<state>` helper style used by
test_transcript.py. No engine required — the pipeline is driven via write_transcript + the
state machine, and source media files are faked on disk (bytes only) so the reset's
preserve/wipe behavior can be asserted.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import (  # noqa: E402
    artifacts,
    catalog,
    langid,
    project,
    state,
    transcript,
    transcript_qa,
)
from video_translation_house.engines import asr  # noqa: E402
from video_translation_house.errors import ConfigurationError, ProjectNotFoundError  # noqa: E402
from video_translation_house.events import read_events  # noqa: E402
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


VID = "yt-vid00000099"


def _fake_source_media(root: Path) -> None:
    """Drop a fake video + audio.wav under source/ and register them, like ingest would."""
    paths = ProjectPaths(root, VID)
    (paths.source_dir / f"{VID}.mp4").write_bytes(b"\x00\x00fake video")
    (paths.source_dir / "audio.wav").write_bytes(b"\x00\x00fake audio")
    artifacts.register_artifact(root, VID, paths.source_dir / f"{VID}.mp4",
                                "source-video", "INGEST", "agent", active=True)
    artifacts.register_artifact(root, VID, paths.source_dir / "audio.wav",
                                "source-audio", "INGEST", "agent", active=True)


def _import_good_transcript(root: Path) -> dict:
    result = asr.ASRResult(
        provider="manual", model=None, language="fa",
        cues=[
            asr.TranscriptCue(0, 0, 2000, "سلام علیکم.", confidence=-0.2, no_speech_prob=0.01),
            asr.TranscriptCue(1, 2000, 4000, "این یک سخنرانی است.", confidence=-0.3),
        ],
        has_word_timing=False, duration_seconds=4.0,
    )
    doc = transcript.build_transcript_doc(VID, "fa", result, actor="agent")
    return transcript.write_transcript(root, VID, doc, actor="agent")


def _project_at_transcript_gate(root: Path) -> str:
    """Whole pipeline up to TRANSCRIPT_QA_GATE with source media, transcript, gloss + QA report."""
    project.init_project(root, VID, url="https://youtube.com/watch?v=vid00000099",
                         target_languages=["en", "ar", "fa"], audio_languages=["en"])
    # Register a catalog entry the way ingest would, so delete-tests have something to purge.
    catalog.upsert_entry(root, {
        "video_id": VID, "url": "https://youtube.com/watch?v=vid00000099",
        "project_id": VID, "language": "fa",
    }, actor="agent")
    state.transition(root, VID, "LANGUAGE_ID", "agent")
    _fake_source_media(root)
    langid.set_language(root, VID, "fa", source="manual", confidence=0.9)
    state.transition(root, VID, "TRANSCRIPTION", "agent")
    _import_good_transcript(root)
    state.transition(root, VID, "TRANSCRIPT_QA_GATE", "agent")
    # Produce the gloss worksheet + a QA report so the reset has downstream work to wipe.
    from video_translation_house import english_gloss
    english_gloss.export_gloss_worksheet(root, VID, actor="agent")
    transcript_qa.run_transcript_qa(root, VID, actor="agent")
    return VID


# --- catalog.remove_entry ----------------------------------------------------

def test_remove_entry_idempotent_when_missing(repo: Path):
    result = catalog.remove_entry(repo, "yt-notthere0001", actor="agent")
    assert result == {"video_id": "yt-notthere0001", "removed": False}


def test_remove_entry_removes_and_logs(repo: Path):
    catalog.upsert_entry(repo, {"video_id": VID, "url": "https://x/y", "project_id": VID}, actor="agent")
    assert catalog.get_entry(repo, VID) is not None
    result = catalog.remove_entry(repo, VID, actor="agent")
    assert result["removed"] is True
    assert catalog.get_entry(repo, VID) is None
    events = [e["event"] for e in read_events(catalog.catalog_events_path(repo))]
    assert "CATALOG_ENTRY_REMOVED" in events


# --- reset_project -----------------------------------------------------------

def test_reset_default_keeps_source_and_transcript_rewinds_to_transcription(repo: Path):
    _project_at_transcript_gate(repo)
    paths = ProjectPaths(repo, VID)
    # Preconditions: gloss worksheet + qa report exist before reset.
    assert (paths.transcript_dir / "english-gloss.worksheet.json").exists()
    assert (paths.transcript_dir / "qa-report.json").exists()
    assert (paths.gate_report("transcript-qa")).exists()

    result = project.reset_project(repo, VID, actor="agent")

    assert result["current_state"] == "TRANSCRIPTION"
    # Source media + source transcript preserved.
    assert (paths.source_dir / f"{VID}.mp4").exists()
    assert (paths.source_dir / "audio.wav").exists()
    assert (paths.transcript_dir / "source.fa.json").exists()
    # Downstream work wiped.
    assert not (paths.transcript_dir / "english-gloss.worksheet.json").exists()
    assert not (paths.transcript_dir / "qa-report.json").exists()
    assert not (paths.gate_report("transcript-qa")).exists()

    st = load_json(paths.state)
    assert st["current_state"] == "TRANSCRIPTION"
    assert st["previous_state"] is None
    assert st["source_language"] == "fa"
    # language_tracks re-baselined to the init seed.
    assert all(t["stage"] == "TRANSLATION" and t["status"] == "pending"
               for t in st["language_tracks"].values())
    # active_artifacts references only preserved artifacts (source + transcript), no gloss.
    assert "transcript@fa" in st["active_artifacts"]
    assert "source-video" in st["active_artifacts"]
    assert "captions@en" not in st["active_artifacts"]
    # A PROJECT_RESET event was appended (history preserved, not erased).
    events = [e["event"] for e in read_events(paths.events)]
    assert "PROJECT_RESET" in events
    assert "PROJECT_CREATED" in events  # prior history still present


def test_reset_full_drops_source_and_transcript_rewinds_to_ingest(repo: Path):
    _project_at_transcript_gate(repo)
    paths = ProjectPaths(repo, VID)
    result = project.reset_project(repo, VID, keep_source=False, keep_transcript=False, actor="agent")
    assert result["current_state"] == "INGEST"
    assert not (paths.source_dir / f"{VID}.mp4").exists()
    assert not (paths.transcript_dir / "source.fa.json").exists()
    st = load_json(paths.state)
    assert st["current_state"] == "INGEST"
    assert st["source_language"] is None
    assert st["active_artifacts"] == {}
    # The directory skeleton is still intact (empty source/ + transcript/ dirs remain).
    assert paths.source_dir.is_dir()
    assert paths.transcript_dir.is_dir()


def test_reset_rejects_unknown_state(repo: Path):
    _project_at_transcript_gate(repo)
    with pytest.raises(ConfigurationError):
        project.reset_project(repo, VID, to_state="NOT_A_STATE", actor="agent")


def test_reset_manifest_reduced_to_preserved_artifacts(repo: Path):
    _project_at_transcript_gate(repo)
    paths = ProjectPaths(repo, VID)
    project.reset_project(repo, VID, actor="agent")
    manifest = load_json(paths.artifact_manifest)
    types = sorted({a["type"] for a in manifest["artifacts"]})
    # Only the re-registered source + transcript artifacts remain; no QA-report/gloss rows.
    assert types == ["source-audio", "source-video", "transcript"]


def test_reset_then_qa_reports_no_gloss(repo: Path):
    """Regression: after a default reset the preserved transcript still QAs cleanly, gloss absent."""
    _project_at_transcript_gate(repo)
    project.reset_project(repo, VID, actor="agent")
    # Advance back to the gate to run QA (transcript preserved).
    state.transition(repo, VID, "TRANSCRIPT_QA_GATE", "agent")
    report = transcript_qa.run_transcript_qa(repo, VID, actor="agent")
    assert report["metrics"]["gloss_present"] is False


# --- delete_project ----------------------------------------------------------

def test_delete_removes_dir_and_catalog_entry(repo: Path):
    _project_at_transcript_gate(repo)
    paths = ProjectPaths(repo, VID)
    assert paths.directory.is_dir()
    assert catalog.get_entry(repo, VID) is not None

    result = project.delete_project(repo, VID, actor="agent")
    assert result["deleted"] is True
    assert result["catalog_removed"] is True
    assert not paths.directory.exists()
    assert catalog.get_entry(repo, VID) is None


def test_delete_keep_catalog_leaves_entry(repo: Path):
    _project_at_transcript_gate(repo)
    result = project.delete_project(repo, VID, purge_catalog=False, actor="agent")
    assert result["catalog_removed"] is False
    assert catalog.get_entry(repo, VID) is not None


def test_delete_missing_project_raises(repo: Path):
    with pytest.raises(ProjectNotFoundError):
        project.delete_project(repo, "yt-missing00001", actor="agent")
