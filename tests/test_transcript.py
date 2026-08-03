"""Phase 2 tests: ASR normalization, cue re-segmentation, transcription orchestration,
deterministic QA, and the transcript_qa gate wiring.

No ASR engine is required — the transcription path is exercised via write_transcript /
transcript import, so these run anywhere. The engine-unavailable case is asserted
directly.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import (  # noqa: E402
    artifacts,
    langid,
    project,
    state,
    transcript,
    transcript_qa,
)
from video_translation_house.engines import asr  # noqa: E402
from video_translation_house.errors import (  # noqa: E402
    ConfigurationError,
    EngineUnavailableError,
    StateTransitionError,
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "CLAUDE.md").write_text("test repo\n")
    shutil.copytree(REPO / ".claude" / "schemas", root / ".claude" / "schemas")
    shutil.copytree(REPO / ".claude" / "config", root / ".claude" / "config")
    (root / "projects").mkdir()
    return root


VID = "yt-vid00000002"


def _project_at_transcription(root: Path) -> str:
    project.init_project(root, VID, url="https://youtube.com/watch?v=vid00000002",
                         target_languages=["en"], audio_languages=["en"])
    state.transition(root, VID, "LANGUAGE_ID", "agent")
    langid.set_language(root, VID, "fa", source="manual", confidence=0.9)
    state.transition(root, VID, "TRANSCRIPTION", "agent")
    return VID


def _whisper_json(segments: list[dict]) -> dict:
    return {"language": "fa", "duration": 30.0, "segments": segments}


# --- ASR normalization -------------------------------------------------------

def test_normalize_whisper_json_segments_and_words():
    raw = _whisper_json([
        {"start": 0.0, "end": 2.5, "text": " Hello", "avg_logprob": -0.3, "no_speech_prob": 0.01,
         "words": [{"word": "Hello", "start": 0.1, "end": 0.6, "probability": 0.98}]},
        {"start": 2.5, "end": 4.0, "text": "world.", "avg_logprob": -0.4},
    ])
    cues, has_words = asr._normalize_whisper_json(raw)
    assert has_words is True
    assert cues[0].start_ms == 0 and cues[0].end_ms == 2500
    assert cues[0].text == "Hello"
    assert cues[0].words[0]["word"] == "Hello" and cues[0].words[0]["start_ms"] == 100


def test_available_providers_and_unavailable_raises(monkeypatch):
    # Force no engine binaries on PATH -> transcribe must raise EngineUnavailableError.
    monkeypatch.setattr(asr, "executable", lambda name: None)
    assert asr.available_asr_providers() == []
    with pytest.raises(EngineUnavailableError):
        asr._resolve_provider(None)
    with pytest.raises(EngineUnavailableError):
        asr._resolve_provider("mlx-whisper")


def test_unknown_provider_rejected(monkeypatch):
    from video_translation_house.errors import VideoTranslationHouseError
    monkeypatch.setattr(asr, "executable", lambda name: "/usr/bin/" + name)
    with pytest.raises(VideoTranslationHouseError):
        asr._resolve_provider("nonsense-engine")


# --- cue re-segmentation -----------------------------------------------------

def test_resegment_merges_unterminated_fragments():
    cues = [
        asr.TranscriptCue(0, 0, 1000, "This is a"),
        asr.TranscriptCue(1, 1100, 2000, "single sentence."),
        asr.TranscriptCue(2, 2100, 3000, "Next one."),
    ]
    merged = transcript._resegment_cues(cues)
    assert len(merged) == 2
    assert merged[0].text == "This is a single sentence."
    assert merged[0].end_ms == 2000
    assert [c.id for c in merged] == [0, 1]


def test_resegment_does_not_cross_long_pause():
    cues = [
        asr.TranscriptCue(0, 0, 1000, "Unfinished"),
        asr.TranscriptCue(1, 3000, 4000, "after a long gap."),  # 2000ms gap > 800ms
    ]
    merged = transcript._resegment_cues(cues)
    assert len(merged) == 2


# --- transcription orchestration + gate wiring -------------------------------

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


def test_write_transcript_registers_artifact_and_validates(repo: Path):
    _project_at_transcription(repo)
    written = _import_good_transcript(repo)
    assert written["cues"] == 2
    tpath = repo / "projects" / VID / "transcript" / "source.fa.json"
    assert tpath.is_file()
    doc = json.loads(tpath.read_text())
    assert doc["language"] == "fa" and doc["engine"]["provider"] == "manual"
    # Artifact registered + tracked active.
    arts = artifacts.list_artifacts(repo, VID)
    assert any(a["type"] == "transcript" and a["language"] == "fa" for a in arts)


def test_run_transcription_requires_state(repo: Path):
    project.init_project(repo, VID, url="u", target_languages=["en"])
    # Still at INGEST -> transcription must refuse.
    with pytest.raises(ConfigurationError):
        transcript.run_transcription(repo, VID)


def test_qa_pass_writes_gate_report_read_by_blocker(repo: Path):
    from video_translation_house import approvals

    _project_at_transcription(repo)
    written = _import_good_transcript(repo)
    result = transcript_qa.run_transcript_qa(repo, VID)
    assert result["decision"] == "PASS"

    # The gate report MUST live where transition_blockers looks it up: by REPORT TYPE
    # ("transcript-qa"), not by gate name ("transcript_qa"). This guards the filename
    # mismatch that would otherwise make the blocker report the report as "missing".
    from video_translation_house.paths import ProjectPaths
    from video_translation_house.state import required_reports
    paths = ProjectPaths(repo, VID)
    report_type = required_reports(repo, "TRANSCRIPT_QA_GATE", "TRANSLATION")[0]
    assert report_type == "transcript-qa"
    assert paths.gate_report(report_type).is_file()
    assert json.loads(paths.gate_report(report_type).read_text())["decision"] == "PASS"

    state.transition(repo, VID, "TRANSCRIPT_QA_GATE", "agent")
    # PASS report satisfies the deterministic half, but the edge is HUMAN-gated:
    # with no approval yet the transition is still blocked, and specifically NOT for a
    # missing report (that would be the filename bug).
    with pytest.raises(StateTransitionError) as exc:
        state.transition(repo, VID, "TRANSLATION", "agent")
    assert "gate report must be PASS" not in str(exc.value)
    assert "approval required" in str(exc.value)

    # Grant the human approval bound to the transcript artifact -> edge opens.
    approvals.grant_approval(
        repo, VID, "transcript_qa", "human",
        [written["artifact"]["sha256"]], scope="transcript", language=None,
    )
    state.transition(repo, VID, "TRANSLATION", "human")
    assert project.project_status(repo, VID)["state"]["current_state"] == "TRANSLATION"


def test_qa_fail_on_corrupt_timing_blocks_gate_report():
    doc = {
        "schema_version": "1.0", "project_id": VID, "language": "fa",
        "engine": {"provider": "manual"}, "created_at": "2026-01-01T00:00:00Z",
        "cues": [{"id": 0, "start_ms": 5000, "end_ms": 1000, "text": "backwards"}],
    }
    analysis = transcript_qa.analyze_transcript(doc)
    assert analysis["decision"] == "FAIL"
    assert any(f["severity"] == "blocker" for f in analysis["findings"])


def test_qa_empty_transcript_fails():
    analysis = transcript_qa.analyze_transcript(
        {"schema_version": "1.0", "project_id": VID, "language": "fa",
         "engine": {"provider": "manual"}, "created_at": "t", "cues": []})
    assert analysis["decision"] == "FAIL"


def test_qa_flags_sensitive_terms_as_notes_not_blockers():
    doc = {
        "schema_version": "1.0", "project_id": VID, "language": "en",
        "engine": {"provider": "manual"}, "created_at": "t",
        "cues": [
            {"id": 0, "start_ms": 0, "end_ms": 2000, "text": "Praise be to Allah the merciful."},
            {"id": 1, "start_ms": 2000, "end_ms": 4000, "text": "The revolution continues."},
        ],
    }
    analysis = transcript_qa.analyze_transcript(doc)
    cats = {f["category"] for f in analysis["findings"]}
    assert "sensitive-religious" in cats
    assert "sensitive-political" in cats
    # Sensitive terms must never auto-block; with clean timing/confidence this is a PASS.
    assert analysis["decision"] == "PASS"
    assert all(f["severity"] == "note" for f in analysis["findings"])


def test_qa_high_low_confidence_share_is_conditional():
    cues = [{"id": i, "start_ms": i * 1000, "end_ms": i * 1000 + 900,
             "text": f"cue {i}", "confidence": -2.0} for i in range(5)]
    analysis = transcript_qa.analyze_transcript(
        {"schema_version": "1.0", "project_id": VID, "language": "fa",
         "engine": {"provider": "manual"}, "created_at": "t", "cues": cues})
    assert analysis["decision"] == "CONDITIONAL_PASS"
    assert analysis["metrics"]["low_confidence_share"] == 1.0
