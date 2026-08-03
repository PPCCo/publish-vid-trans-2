"""Phase 3 tests: translation worksheet export/import, glossary hard-gate, per-language
track advance, and the aggregate translation_qa gate wiring (report + per-language approval).

No LLM or ML engine is involved: translation is exercised by filling a worksheet in-test,
exactly as the translation skill would. Everything runs anywhere.
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
    approvals,
    glossary,
    langid,
    project,
    state,
    transcript,
    translate,
)
from video_translation_house.engines import asr  # noqa: E402
from video_translation_house.errors import ConfigurationError, TranslationError  # noqa: E402
from video_translation_house.paths import ProjectPaths  # noqa: E402
from video_translation_house.state import required_reports  # noqa: E402

VID = "yt-vid00000003"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "CLAUDE.md").write_text("test repo\n")
    shutil.copytree(REPO / ".claude" / "schemas", root / ".claude" / "schemas")
    shutil.copytree(REPO / ".claude" / "config", root / ".claude" / "config")
    (root / "projects").mkdir()
    return root


def _project_at_translation(root: Path, targets=("en",)) -> str:
    project.init_project(root, VID, url="https://youtube.com/watch?v=vid00000003",
                         target_languages=list(targets), audio_languages=["en"])
    (root / "projects" / VID / "source" / "metadata.json").write_text(json.dumps({
        "title": "Test speech", "channel": "Test channel",
        "url": "https://youtube.com/watch?v=vid00000003", "duration_seconds": 6.0,
    }))
    state.transition(root, VID, "LANGUAGE_ID", "agent")
    langid.set_language(root, VID, "fa", source="manual", confidence=0.9)
    state.transition(root, VID, "TRANSCRIPTION", "agent")
    result = asr.ASRResult(
        provider="manual", model=None, language="fa",
        cues=[
            asr.TranscriptCue(0, 0, 2500, "سلام، من درباره انقلاب صحبت می‌کنم.", confidence=-0.2),
            asr.TranscriptCue(1, 2500, 6000, "این یک جمله دوم است.", confidence=-0.3),
        ],
        has_word_timing=False, duration_seconds=6.0,
    )
    doc = transcript.build_transcript_doc(VID, "fa", result, actor="agent")
    transcript.write_transcript(root, VID, doc, actor="agent")
    state.transition(root, VID, "TRANSCRIPT_QA_GATE", "agent")
    # deterministic QA report must be PASS for the edge, then the human gate opens it
    from video_translation_house import transcript_qa
    transcript_qa.run_transcript_qa(root, VID)
    tpath = root / "projects" / VID / "transcript" / "source.fa.json"
    thash = json.loads((root / "projects" / VID / "artifacts" / "manifest.json").read_text())
    src_hash = next(a["sha256"] for a in thash["artifacts"] if a["type"] == "transcript")
    approvals.grant_approval(root, VID, "transcript_qa", "human", [src_hash],
                             scope="transcript", language=None)
    # transcript_qa now guards TRANSCRIPT_QA_GATE -> SEGMENT_RESOLUTION; resolve the whole-video
    # (selection:null) segment set, which advances SEGMENT_RESOLUTION -> TRANSLATION.
    state.transition(root, VID, "SEGMENT_RESOLUTION", "human")
    from video_translation_house import segments as segments_mod
    segments_mod.run_resolve(root, VID, advance=True)
    assert tpath.is_file()
    return VID


def _fill_worksheet(root: Path, language: str, targets: dict[int, str]) -> Path:
    ws_path = root / "projects" / VID / "captions" / f"{language}.worksheet.json"
    ws = json.loads(ws_path.read_text())
    for cue in ws["cues"]:
        cue["target_text"] = targets[cue["id"]]
    ws_path.write_text(json.dumps(ws, ensure_ascii=False))
    return ws_path


# --- worksheet export/import -------------------------------------------------

def test_export_worksheet_mirrors_transcript_timing(repo: Path):
    _project_at_translation(repo)
    out = translate.export_worksheet(repo, VID, "en")
    ws = json.loads((repo / "projects" / VID / out["worksheet"]).read_text())
    assert ws["language"] == "en" and ws["source_language"] == "fa"
    assert [c["id"] for c in ws["cues"]] == [0, 1]
    assert ws["cues"][0]["start_ms"] == 0 and ws["cues"][0]["end_ms"] == 2500
    assert all(c["target_text"] == "" for c in ws["cues"])
    # cue 0 references "انقلاب" (revolution) -> editorial-political flag steers to a stronger model
    assert "editorial-political" in ws["cues"][0]["flags"]


def test_import_requires_all_cues_translated(repo: Path):
    _project_at_translation(repo)
    translate.export_worksheet(repo, VID, "en")
    _fill_worksheet(repo, "en", {0: "Hello, I speak about the revolution.", 1: ""})
    with pytest.raises(TranslationError):
        translate.import_worksheet(repo, VID, "en")


def test_import_builds_canonical_captions_and_advances_track(repo: Path):
    _project_at_translation(repo)
    translate.export_worksheet(repo, VID, "en")
    _fill_worksheet(repo, "en", {0: "Hello, I speak about the revolution.",
                                 1: "This is a second sentence."})
    out = translate.import_worksheet(repo, VID, "en", advance=True)
    cap = json.loads((repo / "projects" / VID / out["captions"]).read_text())
    assert cap["language"] == "en" and cap["script_class"] == "latin"
    assert len(cap["cues"]) == 2
    # single-track project -> top state advances to TRANSLATION_QA_GATE
    st = json.loads((repo / "projects" / VID / "state.json").read_text())
    assert st["current_state"] == "TRANSLATION_QA_GATE"
    assert st["language_tracks"]["en"]["stage"] == "TRANSLATION_QA_GATE"


# --- glossary hard-gate ------------------------------------------------------

def _write_glossary(root: Path, must=True) -> None:
    gdir = root / "glossary"
    gdir.mkdir(exist_ok=True)
    (gdir / "chan-x.json").write_text(json.dumps({
        "schema_version": "1.0", "glossary_id": "chan-x", "source_language": "fa",
        "terms": [{"source": "انقلاب", "targets": {"en": {"rendering": "Revolution"}},
                   "must_appear": must}],
    }, ensure_ascii=False))
    cfg_path = root / "projects" / VID / "project.yaml"
    import yaml
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["glossary_id"] = "chan-x"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))


def test_glossary_must_appear_miss_fails_gate(repo: Path):
    _project_at_translation(repo)
    _write_glossary(repo, must=True)
    translate.export_worksheet(repo, VID, "en")
    # cue 0 uses the source term but the translation omits the required rendering
    _fill_worksheet(repo, "en", {0: "Hello, I speak about the uprising.",
                                 1: "This is a second sentence."})
    translate.import_worksheet(repo, VID, "en")
    result = translate.run_translation_qa(repo, VID)
    assert result["glossary"]["decision"] == "FAIL"
    paths = ProjectPaths(repo, VID)
    assert json.loads(paths.gate_report("glossary").read_text())["decision"] == "FAIL"


def test_glossary_hit_passes_gate(repo: Path):
    _project_at_translation(repo)
    _write_glossary(repo, must=True)
    translate.export_worksheet(repo, VID, "en")
    _fill_worksheet(repo, "en", {0: "Hello, I speak about the Revolution.",
                                 1: "This is a second sentence."})
    translate.import_worksheet(repo, VID, "en")
    result = translate.run_translation_qa(repo, VID)
    assert result["glossary"]["decision"] == "PASS"


def test_no_glossary_configured_is_pass(repo: Path):
    _project_at_translation(repo)
    translate.export_worksheet(repo, VID, "en")
    _fill_worksheet(repo, "en", {0: "Hello.", 1: "Second."})
    translate.import_worksheet(repo, VID, "en")
    result = translate.run_translation_qa(repo, VID)
    assert result["glossary"]["decision"] == "PASS"


def test_glossary_check_unit_hit_and_miss():
    gloss = {"schema_version": "1.0", "glossary_id": "g", "source_language": "fa",
             "terms": [{"source": "x", "targets": {"en": {"rendering": "Ex", "aliases": ["EX"]}},
                        "must_appear": True}]}
    hit = {"language": "en", "cues": [{"id": 0, "source_text": "x here", "target_text": "EX ok"}]}
    miss = {"language": "en", "cues": [{"id": 0, "source_text": "x here", "target_text": "nope"}]}
    f_hit, m_hit = glossary.check_captions(gloss, hit)
    f_miss, m_miss = glossary.check_captions(gloss, miss)
    assert not f_hit and m_hit["required_hit_rate"] == 1.0
    assert any(f["severity"] == "blocker" for f in f_miss)


# --- aggregate gate report + per-language human approval ---------------------

def test_translation_qa_gate_report_read_by_blocker_and_human_gated(repo: Path):
    _project_at_translation(repo)
    _write_glossary(repo, must=True)
    translate.export_worksheet(repo, VID, "en")
    out = _import_clean(repo)
    result = translate.run_translation_qa(repo, VID)
    assert result["translation_qa"]["decision"] == "PASS"
    assert result["glossary"]["decision"] == "PASS"

    # Gate reports live where transition_blockers looks them up: by REPORT TYPE.
    paths = ProjectPaths(repo, VID)
    types = required_reports(repo, "TRANSLATION_QA_GATE", "CAPTION_TIMING")
    assert types == ["translation-qa", "glossary"]
    for t in types:
        assert paths.gate_report(t).is_file()
        assert json.loads(paths.gate_report(t).read_text())["decision"] == "PASS"

    # Reports PASS, but the edge is per-language HUMAN-gated: still blocked, and NOT for a
    # missing report.
    from video_translation_house.errors import StateTransitionError
    with pytest.raises(StateTransitionError) as exc:
        state.transition(repo, VID, "CAPTION_TIMING", "agent")
    assert "gate report must be PASS" not in str(exc.value)
    assert "translation_qa approval required" in str(exc.value)

    # Grant the per-language approval bound to the captions artifact -> edge opens.
    approvals.grant_approval(repo, VID, "translation_qa", "human",
                             [out["artifact"]["sha256"]], scope="captions", language="en")
    state.transition(repo, VID, "CAPTION_TIMING", "human")
    st = json.loads((repo / "projects" / VID / "state.json").read_text())
    assert st["current_state"] == "CAPTION_TIMING"


def _import_clean(repo: Path):
    _fill_worksheet(repo, "en", {0: "Hello, I speak about the Revolution.",
                                 1: "This is a second sentence."})
    return translate.import_worksheet(repo, VID, "en", advance=True)


def test_export_requires_target_language(repo: Path):
    _project_at_translation(repo, targets=("en",))
    with pytest.raises(ConfigurationError):
        translate.export_worksheet(repo, VID, "de")  # not a declared target


def test_multitrack_top_state_waits_for_slowest(repo: Path):
    _project_at_translation(repo, targets=("en", "fr"))
    for lang in ("en", "fr"):
        translate.export_worksheet(repo, VID, lang)
    # Only translate `en` and try to advance -> top state must stay at TRANSLATION.
    _fill_worksheet(repo, "en", {0: "Hello.", 1: "Second."})
    translate.import_worksheet(repo, VID, "en", advance=True)
    st = json.loads((repo / "projects" / VID / "state.json").read_text())
    assert st["current_state"] == "TRANSLATION"
    assert st["language_tracks"]["en"]["stage"] == "TRANSLATION_QA_GATE"
    assert st["language_tracks"]["fr"]["stage"] == "TRANSLATION"
