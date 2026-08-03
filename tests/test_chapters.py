"""Phase 6 tests: chapters.py worksheet round-trip and YouTube timecode rendering.

The chapter worksheet mirrors the translation worksheet contract — the CLI never calls an
LLM; it exports a worksheet from canonical captions, the agent fills titles/breakpoints, and
the CLI validates + imports. Driven with no ffmpeg/ML: we reach CAPTION_VALIDATION through the
manual transcript/translate paths, which is all chapters export needs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import (  # noqa: E402
    approvals,
    chapters,
    langid,
    project,
    state,
    transcript,
    transcript_qa,
    translate,
)
from video_translation_house import segments as segments_mod  # noqa: E402
from video_translation_house.engines import asr  # noqa: E402
from video_translation_house.errors import ConfigurationError, DistributionError  # noqa: E402

VID = "yt-vid00000006"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    import shutil
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "CLAUDE.md").write_text("test repo\n")
    shutil.copytree(REPO / ".claude" / "schemas", root / ".claude" / "schemas")
    shutil.copytree(REPO / ".claude" / "config", root / ".claude" / "config")
    (root / "projects").mkdir()
    return root


def _drive_to_caption_validation(root: Path, lang: str = "en") -> None:
    """Init a project and drive it to CAPTION_VALIDATION through the no-engine paths.

    The transcript has cues with a large gap so the worksheet's candidate-boundary heuristic
    has something to surface."""
    project.init_project(root, VID, url="https://youtube.com/watch?v=vid00000006",
                         target_languages=[lang], audio_languages=[lang])
    paths = root / "projects" / VID
    (paths / "source").mkdir(parents=True, exist_ok=True)
    (paths / "source" / "metadata.json").write_text(json.dumps({
        "title": "Test speech", "channel": "Test channel",
        "url": "https://youtube.com/watch?v=vid00000006", "duration_seconds": 20.0,
    }))
    state.transition(root, VID, "LANGUAGE_ID", "agent")
    langid.set_language(root, VID, "fa", source="manual", confidence=0.9)
    state.transition(root, VID, "TRANSCRIPTION", "agent")
    result = asr.ASRResult(
        provider="manual", model=None, language="fa",
        cues=[
            asr.TranscriptCue(0, 0, 3000, "جمله اول.", confidence=-0.2),
            asr.TranscriptCue(1, 3000, 6000, "جمله دوم.", confidence=-0.2),
            # A 10s gap before the next cue -> a candidate chapter boundary.
            asr.TranscriptCue(2, 16000, 20000, "بخش دوم.", confidence=-0.3),
        ],
        has_word_timing=False, duration_seconds=20.0,
    )
    doc = transcript.build_transcript_doc(VID, "fa", result, actor="agent")
    transcript.write_transcript(root, VID, doc, actor="agent")
    state.transition(root, VID, "TRANSCRIPT_QA_GATE", "agent")
    transcript_qa.run_transcript_qa(root, VID)
    manifest = json.loads((root / "projects" / VID / "artifacts" / "manifest.json").read_text())
    src_hash = next(a["sha256"] for a in manifest["artifacts"] if a["type"] == "transcript")
    approvals.grant_approval(root, VID, "transcript_qa", "human", [src_hash],
                             scope="transcript", language=None)
    state.transition(root, VID, "SEGMENT_RESOLUTION", "human")
    segments_mod.run_resolve(root, VID, advance=True)

    translate.export_worksheet(root, VID, lang)
    ws_path = root / "projects" / VID / "captions" / f"{lang}.worksheet.json"
    ws = json.loads(ws_path.read_text())
    for cue in ws["cues"]:
        cue["target_text"] = f"English line {cue['id']}."
    ws_path.write_text(json.dumps(ws, ensure_ascii=False))
    out = translate.import_worksheet(root, VID, lang, advance=True)
    approvals.grant_approval(root, VID, "translation_qa", "human",
                             [out["artifact"]["sha256"]], scope="captions", language=lang)
    translate.run_translation_qa(root, VID)
    state.transition(root, VID, "CAPTION_TIMING", "human")
    translate.build_captions(root, VID, lang)
    translate.run_caption_validation(root, VID)
    state.transition(root, VID, "CAPTION_VALIDATION", "agent")


# --- export ------------------------------------------------------------------

def test_export_worksheet_has_cues_and_candidates(repo: Path):
    _drive_to_caption_validation(repo)
    out = chapters.export_chapter_worksheet(repo, VID, "en")
    assert out["cues"] == 3
    ws = json.loads((repo / "projects" / VID / "chapters" / "en.chapters-worksheet.json").read_text())
    assert ws["chapters"] == []  # empty for the agent to fill
    assert ws["duration_ms"] == 20000
    starts = [c["start_ms"] for c in ws["candidate_boundaries"]]
    assert starts[0] == 0  # always includes the 0 boundary
    assert 16000 in starts  # the 10s gap surfaced as a candidate


def test_export_guarded_before_caption_validation(repo: Path):
    project.init_project(repo, VID, url="https://youtube.com/watch?v=vid00000006",
                         target_languages=["en"], audio_languages=["en"])
    with pytest.raises(ConfigurationError):
        chapters.export_chapter_worksheet(repo, VID, "en")


# --- import + validation -----------------------------------------------------

def _fill_and_write(repo: Path, lang: str, rows: list[dict]) -> Path:
    ws_path = repo / "projects" / VID / "chapters" / f"{lang}.chapters-worksheet.json"
    ws = json.loads(ws_path.read_text())
    ws["chapters"] = rows
    ws_path.write_text(json.dumps(ws, ensure_ascii=False))
    return ws_path


def test_import_round_trip_registers_artifact(repo: Path):
    _drive_to_caption_validation(repo)
    chapters.export_chapter_worksheet(repo, VID, "en")
    _fill_and_write(repo, "en", [
        {"start_ms": 0, "title": "Introduction"},
        {"start_ms": 16000, "title": "Part Two"},
    ])
    out = chapters.import_chapter_worksheet(repo, VID, "en")
    assert out["count"] == 2
    assert out["artifact"]["type"] == "chapters"
    assert out["artifact"]["language"] == "en"
    doc = chapters.load_chapters(repo, VID, "en")
    assert [c["title"] for c in doc["chapters"]] == ["Introduction", "Part Two"]


def test_import_rejects_first_not_at_zero(repo: Path):
    _drive_to_caption_validation(repo)
    chapters.export_chapter_worksheet(repo, VID, "en")
    _fill_and_write(repo, "en", [{"start_ms": 3000, "title": "Late start"}])
    with pytest.raises(DistributionError):
        chapters.import_chapter_worksheet(repo, VID, "en")


def test_import_rejects_non_monotonic(repo: Path):
    _drive_to_caption_validation(repo)
    chapters.export_chapter_worksheet(repo, VID, "en")
    _fill_and_write(repo, "en", [
        {"start_ms": 0, "title": "A"},
        {"start_ms": 5000, "title": "B"},
        {"start_ms": 5000, "title": "C dup"},  # not strictly increasing
    ])
    with pytest.raises(DistributionError):
        chapters.import_chapter_worksheet(repo, VID, "en")


def test_import_rejects_beyond_duration(repo: Path):
    _drive_to_caption_validation(repo)
    chapters.export_chapter_worksheet(repo, VID, "en")
    _fill_and_write(repo, "en", [
        {"start_ms": 0, "title": "A"},
        {"start_ms": 999999, "title": "Past the end"},  # >= duration_ms (20000)
    ])
    with pytest.raises(DistributionError):
        chapters.import_chapter_worksheet(repo, VID, "en")


def test_import_rejects_empty_title(repo: Path):
    _drive_to_caption_validation(repo)
    chapters.export_chapter_worksheet(repo, VID, "en")
    _fill_and_write(repo, "en", [{"start_ms": 0, "title": "   "}])
    with pytest.raises(DistributionError):
        chapters.import_chapter_worksheet(repo, VID, "en")


def test_reimport_supersedes_and_invalidates_approval(repo: Path):
    _drive_to_caption_validation(repo)
    chapters.export_chapter_worksheet(repo, VID, "en")
    _fill_and_write(repo, "en", [{"start_ms": 0, "title": "First"}])
    first = chapters.import_chapter_worksheet(repo, VID, "en")
    first_hash = first["artifact"]["sha256"]
    # A human approval bound to the first chapters hash.
    approvals.grant_approval(repo, VID, "final_qa", "human", [first_hash],
                             scope="chapters", language="en")
    assert approvals.has_valid_approval(repo, VID, "final_qa", "en")

    # Re-import different chapters -> new hash supersedes the old artifact.
    _fill_and_write(repo, "en", [
        {"start_ms": 0, "title": "First"},
        {"start_ms": 16000, "title": "Second"},
    ])
    second = chapters.import_chapter_worksheet(repo, VID, "en")
    assert second["artifact"]["sha256"] != first_hash
    # The approval bound to the superseded hash no longer validates.
    assert approvals.has_valid_approval(repo, VID, "final_qa", "en") is False


# --- YouTube timecode rendering ----------------------------------------------

def test_render_youtube_timecodes_first_line_is_zero():
    doc = {"chapters": [
        {"start_ms": 0, "title": "Intro"},
        {"start_ms": 65000, "title": "Middle"},
        {"start_ms": 3_725_000, "title": "Deep"},  # 1h 02m 05s
    ]}
    block = chapters.render_youtube_description_timecodes(doc)
    lines = block.splitlines()
    assert lines[0] == "0:00 Intro"
    assert lines[1] == "1:05 Middle"
    assert lines[2] == "1:02:05 Deep"  # H:MM:SS at/above an hour


def test_render_empty_when_no_chapters():
    assert chapters.render_youtube_description_timecodes({"chapters": []}) == ""
    assert chapters.render_youtube_description_timecodes(None) == ""
