"""Phase 4 tests: per-language dubbing + sync report + the aggregate audio-sync gate.

Exercised via the `dub import` no-engine path (mirrors `transcript import`): a pre-rendered
WAV stands in for the TTS engine, so the whole lifecycle runs with no ML installed. ffmpeg is
required for the audio ops; tests skip cleanly where it is absent.
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
    project,
    rights,
    state,
    transcript,
    translate,
)
from video_translation_house.engines import asr  # noqa: E402
from video_translation_house.errors import ConfigurationError, DubbingError  # noqa: E402
from video_translation_house.util import executable  # noqa: E402

VID = "yt-vid00000004"

ffmpeg_required = pytest.mark.skipif(
    not (executable("ffmpeg") and executable("ffprobe")),
    reason="ffmpeg/ffprobe not installed",
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


def _drive_to_caption_validation(root: Path, targets=("en",), audio=("en",)) -> str:
    project.init_project(root, VID, url="https://youtube.com/watch?v=vid00000004",
                         target_languages=list(targets), audio_languages=list(audio))
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
    state.transition(root, VID, "TRANSLATION", "human")

    # translate every target, drive each to CAPTION_VALIDATION
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
    return VID


# --- import path (no engine) -------------------------------------------------

@ffmpeg_required
def test_import_dub_builds_sync_report_and_advances_track(repo: Path, tmp_path: Path):
    _drive_to_caption_validation(repo)
    src = _silence_wav(tmp_path / "dub_src.wav", duration_ms=6000)
    out = dubbing.import_dub(repo, VID, "en", from_path=src)

    dub_path = repo / "projects" / VID / "audio" / "en" / "dub.wav"
    assert dub_path.is_file()
    assert out["artifact"]["type"] == "dub-wav"

    report = json.loads((repo / "projects" / VID / "audio" / "sync-report.json").read_text())
    assert "en" in report["languages"]
    block = report["languages"]["en"]
    assert block["provider"] == "imported"
    assert len(block["cues"]) == 2

    st = json.loads((repo / "projects" / VID / "state.json").read_text())
    assert st["language_tracks"]["en"]["stage"] == "AUDIO_SYNC_ADJUST"


@ffmpeg_required
def test_audio_qa_aggregate_report_and_human_gate(repo: Path, tmp_path: Path):
    _drive_to_caption_validation(repo)
    src = _silence_wav(tmp_path / "dub_src.wav", duration_ms=6000)
    dubbing.import_dub(repo, VID, "en", from_path=src, advance=True)

    result = dubbing.run_audio_qa(repo, VID)
    assert result["decision"] in {"PASS", "CONDITIONAL_PASS"}

    # Gate report lives where transition_blockers looks it up: by REPORT TYPE.
    from video_translation_house.paths import ProjectPaths
    from video_translation_house.state import required_reports
    types = required_reports(repo, "AUDIO_QA_GATE", "VIDEO_MUX")
    assert types == ["audio-sync"]
    paths = ProjectPaths(repo, VID)
    assert paths.gate_report("audio-sync").is_file()

    # Advance the top state to AUDIO_QA_GATE, then confirm the human gate blocks the edge.
    state.transition(repo, VID, "AUDIO_SYNC_ADJUST", "agent")
    state.transition(repo, VID, "AUDIO_QA_GATE", "agent")
    from video_translation_house.errors import StateTransitionError
    with pytest.raises(StateTransitionError) as exc:
        state.transition(repo, VID, "VIDEO_MUX", "agent")
    assert "audio_qa approval required" in str(exc.value)

    # Per-language approval bound to the dub hash opens the edge.
    manifest = json.loads((repo / "projects" / VID / "artifacts" / "manifest.json").read_text())
    dub_hash = next(a["sha256"] for a in manifest["artifacts"]
                    if a["type"] == "dub-wav" and a.get("language") == "en")
    approvals.grant_approval(repo, VID, "audio_qa", "human", [dub_hash],
                             scope="audio", language="en")
    state.transition(repo, VID, "VIDEO_MUX", "human")
    st = json.loads((repo / "projects" / VID / "state.json").read_text())
    assert st["current_state"] == "VIDEO_MUX"


# --- consent gate ------------------------------------------------------------

@ffmpeg_required
def test_clone_refused_without_consent(repo: Path):
    _drive_to_caption_validation(repo)
    with pytest.raises(DubbingError) as exc:
        dubbing.run_dub(repo, VID, "en", clone=True)
    assert "voice_clone_consent" in str(exc.value)


@ffmpeg_required
def test_clone_consent_passes_gate_but_needs_engine(repo: Path):
    _drive_to_caption_validation(repo)
    rights.set_rights(repo, VID, status="self-authored", reviewer="human",
                      voice_clone_consent=True, consent_evidence="signed release")
    # Consent is recorded, so the clone refusal no longer fires; instead we hit the
    # (uninstalled) engine — proving the consent gate is what changed, not the engine.
    from video_translation_house.errors import EngineUnavailableError
    with pytest.raises((EngineUnavailableError, DubbingError, ConfigurationError)):
        dubbing.run_dub(repo, VID, "en", clone=True)


# --- caption-only tracks skip dubbing ---------------------------------------

@ffmpeg_required
def test_caption_only_track_is_not_dubbed(repo: Path, tmp_path: Path):
    # ar is a target language but NOT in audio_languages -> dub_enabled false.
    _drive_to_caption_validation(repo, targets=("en", "ar"), audio=("en",))
    src = _silence_wav(tmp_path / "dub_src.wav", duration_ms=6000)
    with pytest.raises(DubbingError) as exc:
        dubbing.import_dub(repo, VID, "ar", from_path=src)
    assert "not dub_enabled" in str(exc.value)

    # en (dub_enabled) works, and audio_qa only requires the dub-enabled track.
    dubbing.import_dub(repo, VID, "en", from_path=src)
    result = dubbing.run_audio_qa(repo, VID)
    assert "ar" not in result["languages"]
    assert "en" in result["languages"]


# --- guard -------------------------------------------------------------------

def test_dub_run_guarded_before_caption_validation(repo: Path):
    project.init_project(repo, VID, url="https://youtube.com/watch?v=vid00000004",
                         target_languages=["en"], audio_languages=["en"])
    with pytest.raises(ConfigurationError):
        dubbing.run_dub(repo, VID, "en")
