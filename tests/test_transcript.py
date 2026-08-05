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
from video_translation_house.paths import ProjectPaths  # noqa: E402


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
    # The transcript_qa gate now guards TRANSCRIPT_QA_GATE -> SEGMENT_RESOLUTION (a resolution
    # step slots between the QA'd full transcript and TRANSLATION).
    report_type = required_reports(repo, "TRANSCRIPT_QA_GATE", "SEGMENT_RESOLUTION")[0]
    assert report_type == "transcript-qa"
    assert paths.gate_report(report_type).is_file()
    assert json.loads(paths.gate_report(report_type).read_text())["decision"] == "PASS"

    state.transition(repo, VID, "TRANSCRIPT_QA_GATE", "agent")
    # PASS report satisfies the deterministic half, but the edge is HUMAN-gated:
    # with no approval yet the transition is still blocked, and specifically NOT for a
    # missing report (that would be the filename bug).
    with pytest.raises(StateTransitionError) as exc:
        state.transition(repo, VID, "SEGMENT_RESOLUTION", "agent")
    assert "gate report must be PASS" not in str(exc.value)
    assert "approval required" in str(exc.value)

    # Grant the human approval bound to the transcript artifact -> edge opens.
    approvals.grant_approval(
        repo, VID, "transcript_qa", "human",
        [written["artifact"]["sha256"]], scope="transcript", language=None,
    )
    state.transition(repo, VID, "SEGMENT_RESOLUTION", "human")
    assert project.project_status(repo, VID)["state"]["current_state"] == "SEGMENT_RESOLUTION"


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


# --- transcript quality validation (retry-ladder accept/reject signal) --------

def _doc(cues: list[dict], duration: float | None = None) -> dict:
    d = {"schema_version": "1.0", "project_id": VID, "language": "fa",
         "engine": {"provider": "manual"}, "created_at": "t", "cues": cues}
    if duration is not None:
        d["duration_seconds"] = duration
    return d


def test_validate_clean_transcript_has_no_problems():
    cues = [{"id": i, "start_ms": i * 5000, "end_ms": i * 5000 + 4000, "text": f"جمله {i}."}
            for i in range(6)]
    assert transcript.validate_transcript_quality(_doc(cues, duration=30.0)) == []


def test_validate_flags_repetition_run():
    cues = [{"id": i, "start_ms": i * 1000, "end_ms": i * 1000 + 900, "text": "نصیحت"}
            for i in range(5)]
    problems = transcript.validate_transcript_quality(_doc(cues, duration=6.0))
    assert any("repetition" in p for p in problems)


def test_validate_flags_too_few_cues():
    # 300s of audio but only 3 cues -> far below ~1 cue/10s (30 expected).
    cues = [{"id": i, "start_ms": i * 1000, "end_ms": i * 1000 + 500, "text": f"x{i}."}
            for i in range(3)]
    problems = transcript.validate_transcript_quality(_doc(cues, duration=300.0))
    assert any("too few cues" in p for p in problems)


def test_validate_flags_over_long_cue():
    cues = [{"id": 0, "start_ms": 0, "end_ms": 45000, "text": "very long merged cue."}]
    problems = transcript.validate_transcript_quality(_doc(cues, duration=45.0))
    assert any("spans" in p for p in problems)


def test_qa_repetition_run_is_blocker_fail():
    cues = [{"id": i, "start_ms": i * 1000, "end_ms": i * 1000 + 900, "text": "نصیحت"}
            for i in range(4)]
    analysis = transcript_qa.analyze_transcript(_doc(cues))
    assert analysis["decision"] == "FAIL"
    assert any(f["category"] == "repetition" and f["severity"] == "blocker"
               for f in analysis["findings"])


def test_qa_too_coarse_is_conditional_pass():
    # 3 clean-timed cues over 300s of audio -> granularity MAJOR (CONDITIONAL_PASS), no blocker.
    cues = [{"id": i, "start_ms": i * 90000, "end_ms": i * 90000 + 80000, "text": f"segment {i}."}
            for i in range(3)]
    analysis = transcript_qa.analyze_transcript(_doc(cues, duration=300.0))
    assert analysis["decision"] == "CONDITIONAL_PASS"
    cats = {(f["category"], f["severity"]) for f in analysis["findings"]}
    assert ("granularity", "major") in cats
    # over-long cues are notes, not blockers
    assert not any(f["severity"] == "blocker" for f in analysis["findings"])


# --- ASR command building: new decode flags, per-provider naming --------------

def _fake_run_capturing(captured: list[list[str]], out_stem_json: dict):
    """Return a fake asr._run that records the command and drops the expected JSON output so
    the adapter's glob-and-parse step succeeds."""
    import subprocess as _sp

    def _run(command, *, timeout):
        captured.append(list(command))
        # command[1] is the audio path; the adapter globs out_dir for <stem>*.json
        audio = Path(command[1])
        # --output-dir / --output_dir follows in the command
        for i, tok in enumerate(command):
            if tok in ("--output-dir", "--output_dir"):
                out_dir = Path(command[i + 1])
                (out_dir / f"{audio.stem}.json").write_text(json.dumps(out_stem_json))
                break
        return _sp.CompletedProcess(command, 0, stdout="", stderr="")
    return _run


def test_mlx_command_includes_decode_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(asr, "executable", lambda name: "/usr/bin/" + name)
    captured: list[list[str]] = []
    monkeypatch.setattr(asr, "_run", _fake_run_capturing(captured, _whisper_json([])))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"\x00")
    asr.transcribe(audio, tmp_path / "engine", provider="mlx-whisper", language="fa",
                   condition_on_previous_text=False, hallucination_silence_threshold=2.0,
                   temperature=0.0)
    cmd = " ".join(captured[0])
    assert "--condition-on-previous-text False" in cmd            # hyphenated (mlx)
    assert "--hallucination-silence-threshold 2.0" in cmd
    assert "--temperature 0.0" in cmd


def test_faster_command_uses_underscored_flags(tmp_path, monkeypatch):
    # Only faster-whisper "installed" so it's the resolved provider.
    monkeypatch.setattr(asr, "executable",
                        lambda name: "/usr/bin/" + name if name == "faster-whisper" else None)
    captured: list[list[str]] = []
    monkeypatch.setattr(asr, "_run", _fake_run_capturing(captured, _whisper_json([])))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"\x00")
    asr.transcribe(audio, tmp_path / "engine", provider="faster-whisper", language="fa",
                   condition_on_previous_text=False)
    cmd = " ".join(captured[0])
    assert "--condition_on_previous_text False" in cmd            # underscored (faster)


# --- source-language translation skip ----------------------------------------

def _project_at_translation_multi(root: Path) -> str:
    """A project with fa (source) + en/ar targets, driven to TRANSLATION with a transcript."""
    project.init_project(root, VID, url="https://youtube.com/watch?v=vid00000002",
                         target_languages=["en", "ar", "fa"], audio_languages=["en"])
    state.transition(root, VID, "LANGUAGE_ID", "agent")
    langid.set_language(root, VID, "fa", source="manual", confidence=0.9)
    state.transition(root, VID, "TRANSCRIPTION", "agent")
    result = asr.ASRResult(
        provider="manual", model=None, language="fa",
        cues=[asr.TranscriptCue(0, 0, 2000, "سلام علیکم.", confidence=-0.2, no_speech_prob=0.01),
              asr.TranscriptCue(1, 2000, 4000, "این یک سخنرانی است.", confidence=-0.3)],
        has_word_timing=False, duration_seconds=4.0,
    )
    doc = transcript.build_transcript_doc(VID, "fa", result, actor="agent")
    transcript.write_transcript(root, VID, doc, actor="agent")
    # segments resolve reads source/metadata.json for the source duration; ingest would have
    # written it. Provide a minimal one (no engine/ingest run in tests).
    src_dir = ProjectPaths(root, VID).source_dir
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "metadata.json").write_text(json.dumps({"duration_seconds": 4.0}))
    # Advance through the QA gate to TRANSLATION (approval + report), reusing the qa path.
    from video_translation_house import approvals
    written_hash = state.load_json(ProjectPaths(root, VID).state)["active_artifacts"]["transcript@fa"]
    transcript_qa.run_transcript_qa(root, VID)
    state.transition(root, VID, "TRANSCRIPT_QA_GATE", "agent")
    approvals.grant_approval(root, VID, "transcript_qa", "human", [written_hash],
                             scope="transcript", language=None)
    state.transition(root, VID, "SEGMENT_RESOLUTION", "human")
    from video_translation_house import segments as segments_mod
    segments_mod.run_resolve(root, VID, advance=True)
    return VID


def test_langid_marks_source_track_skip_translation(repo: Path):
    _project_at_transcription(repo)
    st = state.load_json(ProjectPaths(repo, VID).state)
    # _project_at_transcription only has target en, source fa is NOT a target -> no track marked.
    assert "fa" not in st["language_tracks"]


def test_source_language_export_writes_verbatim_captions(repo: Path):
    from video_translation_house import translate
    _project_at_translation_multi(repo)
    st = state.load_json(ProjectPaths(repo, VID).state)
    assert st["language_tracks"]["fa"].get("skip_translation") is True

    # Exporting the source language writes verbatim captions, NOT an empty worksheet.
    res = translate.export_worksheet(repo, VID, "fa", actor="agent")
    assert res.get("source_language_skip") is True
    cap_path = repo / "projects" / VID / "captions" / "captions.fa.json"
    ws_path = repo / "projects" / VID / "captions" / "fa.worksheet.json"
    assert cap_path.is_file()
    assert not ws_path.is_file()
    cap = json.loads(cap_path.read_text())
    assert all(c["target_text"] == c["source_text"] for c in cap["cues"])

    # A non-source target still gets a normal worksheet with empty target slots.
    en_res = translate.export_worksheet(repo, VID, "en", actor="agent")
    assert not en_res.get("source_language_skip")
    en_ws = json.loads((repo / "projects" / VID / "captions" / "en.worksheet.json").read_text())
    assert all(c["target_text"] == "" for c in en_ws["cues"])


def test_translation_quorum_ignores_source_track(repo: Path):
    from video_translation_house import translate
    _project_at_translation_multi(repo)
    st = state.load_json(ProjectPaths(repo, VID).state)
    translatable = set(translate._translatable_track_langs(st))
    assert "fa" not in translatable
    assert {"en", "ar"} <= translatable


# --- English review-gloss + AI context verification at TRANSCRIPT_QA_GATE ------

def _gloss_doc(cues: list[dict]) -> dict:
    """A canonical english-gloss doc (captions.schema shape) for analyze_gloss_context tests."""
    return {
        "schema_version": "1.0", "project_id": VID, "language": "en", "source_language": "fa",
        "script_class": "latin", "engine": {"provider": "agent"}, "has_word_timing": False,
        "cues": cues, "created_at": "t",
    }


def _project_at_transcript_gate(root: Path) -> str:
    """Drive a single-target project to TRANSCRIPT_QA_GATE with a source transcript in place."""
    _project_at_transcription(root)
    _import_good_transcript(root)
    state.transition(root, VID, "TRANSCRIPT_QA_GATE", "agent")
    return VID


def test_analyze_gloss_context_untranslated_suspect_is_note():
    from video_translation_house import english_gloss
    src = {"language": "fa", "cues": [{"id": 0, "start_ms": 0, "end_ms": 2000, "text": "این یک جمله است"}]}
    # English identical to a non-trivial source string => untranslated-suspect note.
    gloss = _gloss_doc([{"id": 0, "start_ms": 0, "end_ms": 2000,
                         "source_text": "این یک جمله است", "target_text": "این یک جمله است"}])
    findings = english_gloss.analyze_gloss_context(src, gloss)
    cats = {f["category"] for f in findings}
    assert "english-context/untranslated-suspect" in cats
    assert all(f["severity"] in ("note", "major") for f in findings)


def test_analyze_gloss_context_length_outlier_is_note():
    from video_translation_house import english_gloss
    src = {"language": "fa", "cues": []}
    gloss = _gloss_doc([{"id": 0, "start_ms": 0, "end_ms": 2000,
                         "source_text": "این یک سخنرانی طولانی درباره موضوعات مهم است",
                         "target_text": "Yes."}])  # far shorter than the source
    findings = english_gloss.analyze_gloss_context(src, gloss)
    assert any(f["category"] == "english-context/length-outlier" for f in findings)


def test_analyze_gloss_context_context_mismatch_is_major():
    from video_translation_house import english_gloss
    src = {"language": "fa", "cues": []}
    gloss = _gloss_doc([{"id": 3, "start_ms": 0, "end_ms": 2000,
                         "source_text": "نصیحت نصیحت نصیحت", "target_text": "[unclear — likely ASR error]",
                         "flags": ["context-mismatch"], "context_note": "repeated token, not real speech"}])
    findings = english_gloss.analyze_gloss_context(src, gloss)
    mismatch = [f for f in findings if f["category"] == "english-context/context-mismatch"]
    assert mismatch and mismatch[0]["severity"] == "major"


def test_analyze_gloss_context_clean_has_no_blocking_findings():
    from video_translation_house import english_gloss
    src = {"language": "fa", "cues": []}
    gloss = _gloss_doc([{"id": 0, "start_ms": 0, "end_ms": 2000,
                         "source_text": "سلام علیکم", "target_text": "Peace be upon you."}])
    findings = english_gloss.analyze_gloss_context(src, gloss)
    assert not any(f["severity"] in ("blocker", "major") for f in findings)


def test_export_gloss_worksheet_mirrors_source_and_is_idempotent(repo: Path):
    from video_translation_house import english_gloss
    _project_at_transcript_gate(repo)
    english_gloss.export_gloss_worksheet(repo, VID)
    ws_path = repo / "projects" / VID / "transcript" / "english-gloss.worksheet.json"
    assert ws_path.is_file()
    ws = json.loads(ws_path.read_text())
    assert ws["language"] == "en" and ws["source_language"] == "fa"
    assert [c["id"] for c in ws["cues"]] == [0, 1]
    assert all(c["target_text"] == "" for c in ws["cues"])

    # Idempotent: a second export does not clobber a (partially) filled worksheet.
    ws["cues"][0]["target_text"] = "Peace be upon you."
    ws_path.write_text(json.dumps(ws))
    again = english_gloss.export_gloss_worksheet(repo, VID)
    assert again.get("already_exists") is True
    assert json.loads(ws_path.read_text())["cues"][0]["target_text"] == "Peace be upon you."


def test_export_gloss_requires_gate_state(repo: Path):
    from video_translation_house import english_gloss
    _project_at_transcription(repo)  # still at TRANSCRIPTION, not the gate
    _import_good_transcript(repo)
    with pytest.raises(ConfigurationError):
        english_gloss.export_gloss_worksheet(repo, VID)


def test_import_gloss_rejects_empty_target(repo: Path):
    from video_translation_house import english_gloss
    from video_translation_house.errors import TranslationError
    _project_at_transcript_gate(repo)
    english_gloss.export_gloss_worksheet(repo, VID)
    ws_path = repo / "projects" / VID / "transcript" / "english-gloss.worksheet.json"
    ws = json.loads(ws_path.read_text())
    ws["cues"][0]["target_text"] = "Peace be upon you."  # leave cue 1 empty
    ws_path.write_text(json.dumps(ws))
    with pytest.raises(TranslationError):
        english_gloss.import_gloss_worksheet(repo, VID)


def test_import_gloss_writes_doc_registers_artifact_no_track_change(repo: Path):
    from video_translation_house import english_gloss
    _project_at_transcript_gate(repo)
    english_gloss.export_gloss_worksheet(repo, VID)
    ws_path = repo / "projects" / VID / "transcript" / "english-gloss.worksheet.json"
    ws = json.loads(ws_path.read_text())
    for c in ws["cues"]:
        c["target_text"] = f"gloss {c['id']}"
    ws_path.write_text(json.dumps(ws))

    before = state.load_json(ProjectPaths(repo, VID).state)
    res = english_gloss.import_gloss_worksheet(repo, VID)
    doc_path = repo / "projects" / VID / "transcript" / "english-gloss.json"
    assert doc_path.is_file()
    assert res["artifact"]["type"] == "english-gloss"
    assert res["artifact"]["language"] == "en"

    # Registered as english-gloss (NOT captions-json), and no language track / top state moved.
    after = state.load_json(ProjectPaths(repo, VID).state)
    assert after["current_state"] == "TRANSCRIPT_QA_GATE" == before["current_state"]
    assert after.get("language_tracks") == before.get("language_tracks")
    types = {a["type"] for a in artifacts.list_artifacts(repo, VID)}
    assert "english-gloss" in types
    assert "captions-json" not in types


def test_run_transcript_qa_folds_in_gloss_context_findings(repo: Path):
    from video_translation_house import english_gloss
    _project_at_transcript_gate(repo)
    # Baseline (no gloss): the good transcript PASSes and marks gloss_present=False.
    base = transcript_qa.run_transcript_qa(repo, VID)
    assert base["decision"] == "PASS"
    assert base["metrics"]["gloss_present"] is False

    # Produce a gloss whose context pass flagged a source problem (context-mismatch -> major).
    english_gloss.export_gloss_worksheet(repo, VID)
    ws_path = repo / "projects" / VID / "transcript" / "english-gloss.worksheet.json"
    ws = json.loads(ws_path.read_text())
    ws["cues"][0]["target_text"] = "Peace be upon you."
    ws["cues"][1]["target_text"] = "[unclear — likely ASR error]"
    ws["cues"][1]["flags"] = ["context-mismatch"]
    ws["cues"][1]["context_note"] = "does not make sense in context"
    ws_path.write_text(json.dumps(ws))
    english_gloss.import_gloss_worksheet(repo, VID)

    folded = transcript_qa.run_transcript_qa(repo, VID)
    assert folded["metrics"]["gloss_present"] is True
    assert folded["metrics"]["gloss_context_findings"] >= 1
    assert folded["decision"] == "CONDITIONAL_PASS"
    assert any(f["category"] == "english-context/context-mismatch" for f in folded["findings"])
