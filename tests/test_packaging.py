"""Phase 5 tests: per-language video mux, the aggregate final gate, packaging, and the
terminal rights gate on PACKAGE -> READY_FOR_REVIEW.

Driven through the no-engine paths (`dub import`) so the whole lifecycle runs with no ML
installed. ffmpeg is required for the mux/probe ops; tests skip cleanly where it is absent.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import (  # noqa: E402
    approvals,
    dubbing,
    langid,
    packaging,
    project,
    rights,
    state,
    transcript,
    translate,
)
from video_translation_house.engines import asr  # noqa: E402
from video_translation_house.errors import ConfigurationError, MuxError, StateTransitionError  # noqa: E402
from video_translation_house.util import executable  # noqa: E402

VID = "yt-vid00000005"

ffmpeg_required = pytest.mark.skipif(
    not (executable("ffmpeg") and executable("ffprobe")),
    reason="ffmpeg/ffprobe not installed",
)


def _subtitles_filter_available() -> bool:
    if not executable("ffmpeg"):
        return False
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    return " subtitles " in proc.stdout


burned_in_required = pytest.mark.skipif(
    not _subtitles_filter_available(),
    reason="ffmpeg build lacks the subtitles (libass) filter",
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


def _silence_wav(dest: Path, *, duration_ms: int) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
         "-t", f"{duration_ms / 1000:.3f}", "-ac", "1", "-ar", "24000",
         "-c:a", "pcm_s16le", str(dest)],
        capture_output=True, check=True,
    )
    return dest


def _fake_source_video(dest: Path, *, duration_ms: int) -> Path:
    """A tiny synthetic mp4 (color bars + silent audio) standing in for the source video."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    secs = f"{duration_ms / 1000:.3f}"
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15:duration={secs}",
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={secs}",
         "-c:v", "libx264", "-t", secs, "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(dest)],
        capture_output=True, check=True,
    )
    return dest


def _drive_to_audio_qa_gate(root: Path, targets=("en",), audio=("en",)) -> str:
    """Init a project and drive it to VIDEO_MUX-ready: through CAPTION_VALIDATION, dub import,
    audio_qa approval, top state at AUDIO_QA_GATE."""
    project.init_project(root, VID, url="https://youtube.com/watch?v=vid00000005",
                         target_languages=list(targets), audio_languages=list(audio))
    # Put a synthetic source video in place for the mux step.
    paths = root / "projects" / VID
    _fake_source_video(paths / "source" / "video.mp4", duration_ms=6000)
    (paths / "source" / "metadata.json").write_text(json.dumps({
        "title": "Test speech", "channel": "Test channel",
        "url": "https://youtube.com/watch?v=vid00000005", "duration_seconds": 6.0,
    }))

    state.transition(root, VID, "LANGUAGE_ID", "agent")
    langid.set_language(root, VID, "fa", source="manual", confidence=0.9)
    state.transition(root, VID, "TRANSCRIPTION", "agent")
    result = asr.ASRResult(
        provider="manual", model=None, language="fa",
        cues=[
            asr.TranscriptCue(0, 0, 2500, "جمله اول.", confidence=-0.2),
            asr.TranscriptCue(1, 2500, 6000, "جمله دوم اینجاست.", confidence=-0.3),
        ],
        has_word_timing=False, duration_seconds=6.0,
    )
    doc = transcript.build_transcript_doc(VID, "fa", result, actor="agent")
    transcript.write_transcript(root, VID, doc, actor="agent")
    state.transition(root, VID, "TRANSCRIPT_QA_GATE", "agent")
    from video_translation_house import transcript_qa
    transcript_qa.run_transcript_qa(root, VID)
    manifest = json.loads((root / "projects" / VID / "artifacts" / "manifest.json").read_text())
    src_hash = next(a["sha256"] for a in manifest["artifacts"] if a["type"] == "transcript")
    approvals.grant_approval(root, VID, "transcript_qa", "human", [src_hash],
                             scope="transcript", language=None)
    # transcript_qa now guards TRANSCRIPT_QA_GATE -> SEGMENT_RESOLUTION; resolve the (whole-video,
    # selection:null) segment set there, which advances SEGMENT_RESOLUTION -> TRANSLATION.
    state.transition(root, VID, "SEGMENT_RESOLUTION", "human")
    from video_translation_house import segments as segments_mod
    segments_mod.run_resolve(root, VID, advance=True)

    for lang in targets:
        translate.export_worksheet(root, VID, lang)
        ws_path = root / "projects" / VID / "captions" / f"{lang}.worksheet.json"
        ws = json.loads(ws_path.read_text())
        for cue in ws["cues"]:
            cue["target_text"] = f"line {cue['id']} in {lang}."
        ws_path.write_text(json.dumps(ws, ensure_ascii=False))
    for lang in targets:
        out = translate.import_worksheet(root, VID, lang, advance=(lang == targets[-1]))
        approvals.grant_approval(root, VID, "translation_qa", "human",
                                 [out["artifact"]["sha256"]], scope="captions", language=lang)
    translate.run_translation_qa(root, VID)
    state.transition(root, VID, "CAPTION_TIMING", "human")
    for lang in targets:
        translate.build_captions(root, VID, lang)
    translate.run_caption_validation(root, VID)
    state.transition(root, VID, "CAPTION_VALIDATION", "agent")

    # Dub every dub-enabled track (import path), advance top to DUBBING then to AUDIO_QA_GATE.
    src = _silence_wav(root / "projects" / VID / "tmp" / "dub_src.wav", duration_ms=6000)
    dub_langs = [lang for lang in targets if lang in audio]
    for i, lang in enumerate(dub_langs):
        dubbing.import_dub(root, VID, lang, from_path=src, advance=(i == len(dub_langs) - 1))
    dubbing.run_audio_qa(root, VID)
    st = json.loads((root / "projects" / VID / "state.json").read_text())
    assert st["current_state"] == "DUBBING"
    state.transition(root, VID, "AUDIO_SYNC_ADJUST", "agent")
    state.transition(root, VID, "AUDIO_QA_GATE", "agent")
    # Per-language audio_qa approval bound to each dub hash.
    manifest = json.loads((root / "projects" / VID / "artifacts" / "manifest.json").read_text())
    for lang in dub_langs:
        dub_hash = next(a["sha256"] for a in manifest["artifacts"]
                        if a["type"] == "dub-wav" and a.get("language") == lang)
        approvals.grant_approval(root, VID, "audio_qa", "human", [dub_hash],
                                 scope="audio", language=lang)
    state.transition(root, VID, "VIDEO_MUX", "human")
    return VID


# --- mux ---------------------------------------------------------------------

@ffmpeg_required
def test_mux_builds_dubbed_video_and_advances(repo: Path):
    _drive_to_audio_qa_gate(repo)
    out = packaging.run_mux(repo, VID, "en", advance=True)
    dubbed = repo / "projects" / VID / "video" / "en" / "dubbed.mp4"
    assert dubbed.is_file()
    assert out["artifact"]["type"] == "dubbed-video"

    from video_translation_house.media import probe_summary
    summary = probe_summary(dubbed)
    assert summary["video"].get("codec")
    assert summary["audio"].get("codec")

    st = json.loads((repo / "projects" / VID / "state.json").read_text())
    assert st["language_tracks"]["en"]["stage"] == "FINAL_QA_GATE"
    assert st["current_state"] == "VIDEO_MUX"


@ffmpeg_required
@burned_in_required
def test_mux_burned_in_renders_video_and_reencodes(repo: Path):
    _drive_to_audio_qa_gate(repo)
    out = packaging.run_mux(repo, VID, "en", mode="burned-in", advance=True)
    assert out["mode"] == "burned-in"
    dubbed = repo / "projects" / VID / "video" / "en" / "dubbed.mp4"
    assert dubbed.is_file()

    from video_translation_house.media import probe_summary
    summary = probe_summary(dubbed)
    assert summary["video"].get("codec") == "h264"  # re-encoded, not the source's stream copy
    assert summary["audio"].get("codec")


@ffmpeg_required
def test_mux_burned_in_requires_captions(repo: Path):
    _drive_to_audio_qa_gate(repo)
    vtt = repo / "projects" / VID / "captions" / "captions.en.vtt"
    vtt.unlink()
    with pytest.raises(MuxError):
        packaging.run_mux(repo, VID, "en", mode="burned-in")


@ffmpeg_required
def test_mux_guarded_before_audio_qa_gate(repo: Path):
    # A fresh project not yet at AUDIO_QA_GATE.
    project.init_project(repo, VID, url="https://youtube.com/watch?v=vid00000005",
                         target_languages=["en"], audio_languages=["en"])
    with pytest.raises(ConfigurationError):
        packaging.run_mux(repo, VID, "en")


# --- final QA + human gate ---------------------------------------------------

@ffmpeg_required
def test_final_qa_report_and_human_gate(repo: Path):
    _drive_to_audio_qa_gate(repo)
    packaging.run_mux(repo, VID, "en", advance=True)
    state.transition(repo, VID, "FINAL_QA_GATE", "agent")

    result = packaging.run_final_qa(repo, VID)
    assert result["decision"] == "PASS"

    # Gate report lives where transition_blockers looks it up: by REPORT TYPE.
    from video_translation_house.paths import ProjectPaths
    from video_translation_house.state import required_reports
    assert required_reports(repo, "FINAL_QA_GATE", "PACKAGE") == ["final"]
    paths = ProjectPaths(repo, VID)
    assert paths.gate_report("final").is_file()

    # final_qa is a single project-level human gate: blocks the edge until approved.
    with pytest.raises(StateTransitionError) as exc:
        state.transition(repo, VID, "PACKAGE", "agent")
    assert "final_qa approval required" in str(exc.value)

    manifest = json.loads((repo / "projects" / VID / "artifacts" / "manifest.json").read_text())
    vid_hash = next(a["sha256"] for a in manifest["artifacts"]
                    if a["type"] == "dubbed-video" and a.get("language") == "en")
    approvals.grant_approval(repo, VID, "final_qa", "human", [vid_hash],
                             scope="video", language=None)
    state.transition(repo, VID, "PACKAGE", "human")
    st = json.loads((repo / "projects" / VID / "state.json").read_text())
    assert st["current_state"] == "PACKAGE"


# --- package + rights gate ---------------------------------------------------

@ffmpeg_required
def test_package_manifest_and_rights_gate(repo: Path):
    _drive_to_audio_qa_gate(repo)
    packaging.run_mux(repo, VID, "en", advance=True)
    state.transition(repo, VID, "FINAL_QA_GATE", "agent")
    packaging.run_final_qa(repo, VID)
    manifest = json.loads((repo / "projects" / VID / "artifacts" / "manifest.json").read_text())
    vid_hash = next(a["sha256"] for a in manifest["artifacts"]
                    if a["type"] == "dubbed-video" and a.get("language") == "en")
    approvals.grant_approval(repo, VID, "final_qa", "human", [vid_hash],
                             scope="video", language=None)
    state.transition(repo, VID, "PACKAGE", "human")

    out = packaging.run_package(repo, VID)
    assert out["distributable"] is False
    assert out["rights_status"] == "unreviewed"

    pm = json.loads((repo / "projects" / VID / "packages" / "package-manifest.json").read_text())
    assert pm["packages"][0]["language"] == "en"
    types = {d["type"] for d in pm["packages"][0]["deliverables"]}
    assert {"dubbed-video", "captions-srt", "captions-vtt", "readme"} <= types

    readme = (repo / "projects" / VID / "packages" / "en" / "README.md").read_text()
    assert "NOT CLEARED FOR DISTRIBUTION" in readme

    # Rights gate: PACKAGE -> READY_FOR_REVIEW is blocked while unreviewed.
    with pytest.raises(StateTransitionError) as exc:
        state.transition(repo, VID, "READY_FOR_REVIEW", "agent")
    assert "rights_status" in str(exc.value)

    # A human sets distributable rights -> the terminal transition opens.
    rights.set_rights(repo, VID, status="self-authored", reviewer="human")
    state.transition(repo, VID, "READY_FOR_REVIEW", "human")
    st = json.loads((repo / "projects" / VID / "state.json").read_text())
    assert st["current_state"] == "READY_FOR_REVIEW"


# --- caption-only track ------------------------------------------------------

@ffmpeg_required
def test_caption_only_track_packages_without_video(repo: Path):
    # ar is a target but NOT dub-enabled -> no dubbed video; en is dubbed.
    _drive_to_audio_qa_gate(repo, targets=("en", "ar"), audio=("en",))
    packaging.run_mux(repo, VID, "en", advance=True)
    with pytest.raises(MuxError):
        packaging.run_mux(repo, VID, "ar")  # caption-only: nothing to mux
    state.transition(repo, VID, "FINAL_QA_GATE", "agent")
    result = packaging.run_final_qa(repo, VID)
    assert "ar" not in result["languages"]  # final QA only measures dub tracks
    assert "en" in result["languages"]

    manifest = json.loads((repo / "projects" / VID / "artifacts" / "manifest.json").read_text())
    vid_hash = next(a["sha256"] for a in manifest["artifacts"]
                    if a["type"] == "dubbed-video" and a.get("language") == "en")
    approvals.grant_approval(repo, VID, "final_qa", "human", [vid_hash],
                             scope="video", language=None)
    state.transition(repo, VID, "PACKAGE", "human")
    out = packaging.run_package(repo, VID)
    by_lang = {p["language"]: p for p in out["packages"]}
    assert by_lang["ar"]["dub_enabled"] is False
    ar_types = {d["type"] for d in by_lang["ar"]["deliverables"]}
    assert "dubbed-video" not in ar_types
    assert {"captions-srt", "captions-vtt", "readme"} <= ar_types
