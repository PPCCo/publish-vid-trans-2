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
    (root / "projects" / VID / "source" / "metadata.json").write_text(json.dumps({
        "title": "Test speech", "channel": "Test channel",
        "url": "https://youtube.com/watch?v=vid00000004", "duration_seconds": 6.0,
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
    # transcript_qa now guards TRANSCRIPT_QA_GATE -> SEGMENT_RESOLUTION; resolve the whole-video
    # (selection:null) segment set, which advances SEGMENT_RESOLUTION -> TRANSLATION.
    state.transition(root, VID, "SEGMENT_RESOLUTION", "human")
    from video_translation_house import segments as segments_mod
    segments_mod.run_resolve(root, VID, advance=True)

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
    # Company policy auto-records voice_clone_consent=true at init (rule-5 override: clone-on +
    # auto_consent_at_init). Explicitly clear consent so this test exercises the clone-refusal
    # path — a --clone dub must be refused before any source fetch when consent is not recorded.
    rights.set_rights(repo, VID, status="unreviewed", reviewer="human",
                      voice_clone_consent=False)
    with pytest.raises(DubbingError) as exc:
        dubbing.run_dub(repo, VID, "en", clone=True)
    assert "voice_clone_consent" in str(exc.value)


@ffmpeg_required
def test_clone_consent_passes_gate_but_needs_engine(repo: Path):
    _drive_to_caption_validation(repo)
    rights.set_rights(repo, VID, status="self-authored", reviewer="human",
                      voice_clone_consent=True, consent_evidence="signed release")
    # Consent is recorded, so the clone refusal no longer fires; instead we fail for a
    # DIFFERENT reason — proving the consent gate is what changed. With no source WAV on
    # disk (this fixture never downloaded one) a --clone dub now calls ensure_source_present,
    # which re-fetches; egress is off in tests so that surfaces as a clean FetchDisabled.
    # (If a source WAV were present we'd instead hit the uninstalled engine.)
    from video_translation_house.errors import EngineUnavailableError, FetchDisabled
    with pytest.raises((EngineUnavailableError, DubbingError, ConfigurationError, FetchDisabled)):
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


# --- constant audio speed (TASK 2 / rule 5) ----------------------------------

@ffmpeg_required
def test_normalize_wav_preserves_duration(tmp_path: Path):
    """The run_dub per-cue path (media.normalize_wav) NEVER time-stretches — a cue's audio is
    emitted at its natural length and the picture is re-timed around it. Format normalization
    only: duration must be identical (within a couple ms of container rounding)."""
    from video_translation_house import media

    src = _silence_wav(tmp_path / "raw.wav", duration_ms=3700)
    before = media.audio_duration_ms(src)
    out = media.normalize_wav(src, tmp_path / "fit.wav")
    after = media.audio_duration_ms(out)
    assert after == pytest.approx(before, abs=20)
    # And normalization to a different natural length is likewise preserved (no clamp to a slot).
    src2 = _silence_wav(tmp_path / "raw2.wav", duration_ms=1200)
    out2 = media.normalize_wav(src2, tmp_path / "fit2.wav")
    assert media.audio_duration_ms(out2) == pytest.approx(1200, abs=20)


# --- still-image dead-air fix (rule 15: no freeze plan -> compact lead silence) ----------------

def test_natural_pause_before_first_cue_is_always_true():
    cues = [{"id": 0, "start_ms": 0, "end_ms": 1000}]
    assert dubbing._natural_pause_before(cues, 0) is True


def test_natural_pause_before_detects_real_gap_vs_contiguous():
    cues = [
        {"id": 0, "start_ms": 0, "end_ms": 1000},
        {"id": 1, "start_ms": 1000, "end_ms": 2000},  # contiguous, no gap
        {"id": 2, "start_ms": 2800, "end_ms": 3500},  # 800ms real pause
    ]
    assert dubbing._natural_pause_before(cues, 1) is False
    assert dubbing._natural_pause_before(cues, 2) is True


def test_lead_silence_waits_for_caption_start_when_not_still_image():
    # Non-still-image tracks are unchanged: always wait for the nominal caption start regardless
    # of a prior cue's overrun -- the freeze plan absorbs the mismatch on the picture side.
    cues = [
        {"id": 0, "start_ms": 0, "end_ms": 1000},
        {"id": 1, "start_ms": 1000, "end_ms": 2000},  # mechanically contiguous
    ]
    # cue 0 overran its slot (rendered to timeline_ms=5000 instead of 1000)
    assert dubbing._lead_silence_ms(cues, 1, timeline_ms=5000, is_still_image=False) == 0
    assert dubbing._lead_silence_ms(cues, 1, timeline_ms=200, is_still_image=False) == 800


def test_lead_silence_skips_catchup_gap_for_contiguous_still_image_cue():
    # Regression: a still-image track has no freeze plan, so re-imposing a prior cue's overrun as
    # a catch-up silence gap before a mechanically-adjacent cue (no real source pause) invented
    # dead air that wasn't in the source. Contiguous cues must now play back-to-back instead.
    cues = [
        {"id": 0, "start_ms": 0, "end_ms": 1000},
        {"id": 1, "start_ms": 1000, "end_ms": 2000},  # contiguous with cue 0, no real pause
    ]
    # cue 0's TTS overran its 1000ms slot -> timeline_ms is already past cue 1's start
    assert dubbing._lead_silence_ms(cues, 1, timeline_ms=5000, is_still_image=True) == 0


def test_lead_silence_still_image_inserts_only_the_pauses_own_duration():
    # Regression: a still-image cue used to re-anchor to the ABSOLUTE caption start once a real
    # pause was detected, so a small genuine pause (e.g. 4000ms in the source) could reinsert an
    # entire accumulated drift as dead air (observed live: an 860ms real pause reinserted ~165s
    # because earlier cues had drifted that far behind). The inserted silence must always equal
    # the pause's OWN duration (next cue's start minus previous cue's end), never the absolute
    # gap to the running timeline -- regardless of how far timeline_ms has drifted.
    cues = [
        {"id": 0, "start_ms": 0, "end_ms": 1000},
        {"id": 1, "start_ms": 5000, "end_ms": 6000},  # genuine 4000ms pause in the source
    ]
    # timeline caught up exactly to the prior cue's end -> pause fully honored
    assert dubbing._lead_silence_ms(cues, 1, timeline_ms=1000, is_still_image=True) == 4000
    # timeline is already running FAR behind the caption schedule (massive prior overrun) ->
    # still only the pause's own 4000ms, never the absolute lead (5000 - 100000 would be negative
    # under the old model, but a naive re-anchor to start_ms would have inserted a huge gap here)
    assert dubbing._lead_silence_ms(cues, 1, timeline_ms=100000, is_still_image=True) == 4000
    # timeline is running ahead of the caption schedule -> still the pause's own 4000ms
    assert dubbing._lead_silence_ms(cues, 1, timeline_ms=-50000, is_still_image=True) == 4000


def test_lead_silence_never_negative():
    cues = [{"id": 0, "start_ms": 0, "end_ms": 1000}, {"id": 1, "start_ms": 1000, "end_ms": 2000}]
    assert dubbing._lead_silence_ms(cues, 1, timeline_ms=9000, is_still_image=False) == 0
    assert dubbing._lead_silence_ms(cues, 1, timeline_ms=9000, is_still_image=True) == 0
