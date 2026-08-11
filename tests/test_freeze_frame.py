"""Freeze-frame dubbing tests: when a dubbed cue's neutral-TTS audio runs longer than its caption
slot beyond the gentle stretch cap, the picture is frozen for the overflow and downstream picture
is shifted (audio never over-speeds or lags).

Two layers:
  * Pure functions (no ffmpeg): `_plan_freezes`, `_build_sync_report` honesty, `analyze_sync`
    note/major findings, and `_freeze_windows` retiming math.
  * ffmpeg-backed: `freeze_segment`, `run_mux`'s retimed picture + retimed subs, `run_final_qa`,
    the default-OFF regression, and the `import_dub` scope limit.

The freeze plan is a real-TTS (`run_dub`) product; `import_dub` never produces one. To exercise
mux/package without a TTS engine we hand-register a freeze-plan artifact through the CLI-owned
`dubbing._write_freeze_plan` (the same path run_dub uses) after driving the project with `dub import`.
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
    state,
    transcript,
    translate,
)
from video_translation_house.engines import asr  # noqa: E402
from video_translation_house.util import executable  # noqa: E402

VID = "yt-vid00000006"

ffmpeg_required = pytest.mark.skipif(
    not (executable("ffmpeg") and executable("ffprobe")),
    reason="ffmpeg/ffprobe not installed",
)


def _write_local_config(root: Path, *, freeze: bool) -> None:
    """Overwrite the copied company.local.json with a fixture-local override so tests don't
    couple to the real project's config. `freeze` toggles the freeze-frame policy bar."""
    local = root / ".claude" / "config" / "company.local.json"
    doc = {"quality_bars": {"audio": {"freeze_frame_enabled": bool(freeze)}}}
    local.write_text(json.dumps(doc))


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "CLAUDE.md").write_text("test repo\n")
    shutil.copytree(REPO / ".claude" / "schemas", root / ".claude" / "schemas")
    shutil.copytree(REPO / ".claude" / "config", root / ".claude" / "config")
    _write_local_config(root, freeze=True)
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


# --- pure-function fixtures --------------------------------------------------

def _bars(freeze: bool = True, trim_langs: list[str] | None = None) -> dict:
    return {
        "target_lufs": -16.0,
        "max_time_stretch": 1.3,
        "per_cue_drift_tolerance_ms": 150,
        "cumulative_drift_ceiling_ms": 500,
        "freeze_frame_enabled": freeze,
        "freeze_stretch_cap": 1.15,
        "max_freeze_ms_per_cue": 4000,
        "freeze_trim_languages": list(trim_langs or []),
        "silent_span_max_ms": 7000,
    }


def _measures_back_to_back(cues: list[tuple[int, int, int, int]]) -> list[dict]:
    """Build a run-of-cues' measures the way run_dub lays audio **back-to-back**: each cue's
    rendered audio (fit to the gentle 1.15x cap) starts at the audio playhead, plus lead silence
    only when the audio would otherwise start EARLY (playhead < caption_start). So a long cue
    pushes every following cue's rendered_start later — the propagation the running-gap planner
    must absorb. `cues` are (id, caption_start, caption_end, natural_ms)."""
    out: list[dict] = []
    playhead = 0
    for cid, start, end, natural_ms in cues:
        slot = max(1, end - start)
        stretch = natural_ms / slot
        applied = min(stretch, 1.15)
        rendered = round(natural_ms / applied) if applied > 0 else natural_ms
        rstart = max(playhead, start)  # lead silence only when audio is early
        out.append({
            "id": cid, "caption_start_ms": start, "caption_end_ms": end,
            "rendered_start_ms": rstart, "rendered_end_ms": rstart + rendered,
            "natural_ms": natural_ms, "stretch_factor": round(stretch, 4),
            "over_stretch_cap": stretch > 1.15,
        })
        playhead = rstart + rendered
    return out


# --- 1. _plan_freezes computes the running-gap holds (and trims) -------------

def test_plan_freezes_absorbs_propagated_backlog():
    # cue0 (slot 2000, natural 3000) overruns to 2609ms; run_dub lays cue1 back-to-back at 2609,
    # so the picture is 609ms behind the audio at cue1's boundary -> hold 609ms there. The hold
    # is planned at the *following* cue's start (== a cue boundary), on the running gap.
    measures = _measures_back_to_back([(0, 0, 2000, 3000), (1, 2000, 4000, 1800)])
    plan = dubbing._plan_freezes(measures, _bars())
    assert plan["trims"] == []
    assert [f["cue_id"] for f in plan["freezes"]] == [1]
    fz = plan["freezes"][0]
    assert fz["at_ms"] == 2000  # insertion point == the corrected cue's caption_start_ms
    assert fz["slot_ms"] == 2000
    assert fz["natural_ms"] == 1800
    # running gap at cue1 = rendered_start(2609) - (caption_start(2000) + cum_freeze(0)) = 609
    assert fz["freeze_ms"] == 609


def test_plan_freezes_records_even_excessive_freeze():
    # A very long natural drives the running gap on the next cue past max_freeze_ms_per_cue (4000).
    measures = _measures_back_to_back([(0, 0, 1000, 12000), (1, 1000, 3000, 500)])
    plan = dubbing._plan_freezes(measures, _bars())
    assert len(plan["freezes"]) == 1
    # cue0 renders round(12000/1.15)=10435; cue1's gap = 10435 - 1000 = 9435 > 4000 cap.
    assert plan["freezes"][0]["freeze_ms"] > 4000


def test_plan_freezes_trim_mode_cancels_underrun_after_a_gap():
    # cue0 overruns (hold 609 planned at cue1); then a big natural GAP before cue2 lets the audio
    # playhead fall 609ms behind the *already-held* picture. Hold mode leaves that as an honest
    # residual; trim mode trims 609ms of picture at cue2 so residual -> 0.
    measures = _measures_back_to_back(
        [(0, 0, 2000, 3000), (1, 2000, 4000, 1800), (2, 8000, 10000, 2000)]
    )
    hold = dubbing._plan_freezes(measures, _bars())
    assert [f["cue_id"] for f in hold["freezes"]] == [1]
    assert hold["trims"] == []

    trim = dubbing._plan_freezes(measures, _bars(trim_langs=["ar"]) | {}, trim=True)
    assert [f["cue_id"] for f in trim["freezes"]] == [1]
    assert [t["cue_id"] for t in trim["trims"]] == [2]
    tr = trim["trims"][0]
    assert tr["at_ms"] == 8000  # the corrected cue's caption_start_ms
    assert tr["trim_ms"] == 609


# --- 1b. tail reconciliation: picture END == audio END ----------------------

def test_plan_freezes_tail_trim_shortens_overlong_picture():
    # Two short cues; the source video runs well PAST the last cue (an untrimmed tail). Without tail
    # reconciliation the picture would end at source_duration while the audio ended at the last cue.
    # Trim mode drops the surplus tail so picture end == audio end.
    measures = _measures_back_to_back([(0, 0, 2000, 1800), (1, 2000, 4000, 1800)])
    # audio ends at last rendered_end; source video is 3000ms longer than that.
    audio_end = measures[-1]["rendered_end_ms"]
    src_dur = audio_end + 3000
    plan = dubbing._plan_freezes(measures, _bars(trim_langs=["ar"]), trim=True,
                                 source_duration_ms=src_dur)
    tails = [t for t in plan["trims"] if t.get("tail")]
    assert len(tails) == 1
    t = tails[0]
    assert t["trim_ms"] == 3000
    assert t["at_ms"] == src_dur - 3000  # drops the real source tail [src-3000, src)
    # picture_end after all freezes/trims == audio_end
    net = sum(f["freeze_ms"] for f in plan["freezes"]) - sum(x["trim_ms"] for x in plan["trims"])
    assert src_dur + net == audio_end


def test_plan_freezes_tail_freeze_extends_short_picture():
    # Audio runs LONGER than the source picture (e.g. the dub overran and the source is short):
    # a tail freeze holds the final frame so the picture never ends before the audio. Emitted in
    # BOTH modes (hold mode too) — the picture must never run out under live audio.
    measures = _measures_back_to_back([(0, 0, 2000, 3000), (1, 2000, 4000, 3000)])
    audio_end = measures[-1]["rendered_end_ms"]
    src_dur = audio_end - 2500  # source ends 2500ms before the audio does
    for bars, trim in ((_bars(), False), (_bars(trim_langs=["ar"]), True)):
        plan = dubbing._plan_freezes(measures, bars, trim=trim, source_duration_ms=src_dur)
        tails = [f for f in plan["freezes"] if f.get("tail")]
        assert len(tails) == 1, f"trim={trim}"
        assert tails[0]["at_ms"] == src_dur
        net = sum(f["freeze_ms"] for f in plan["freezes"]) - sum(x["trim_ms"] for x in plan["trims"])
        assert src_dur + net == audio_end


def test_plan_freezes_tail_excluded_from_sync_report(repo: Path):
    # The tail entry reuses the last cue's id but must NOT double-adjust that cue's residual in the
    # sync report (it's a picture-length event keyed by at_ms for the mux, not a cue-start event).
    _drive_to_audio_qa_gate(repo, freeze=False)
    measures = _measures_back_to_back([(0, 0, 2000, 1800), (1, 2000, 4000, 1800)])
    audio_end = measures[-1]["rendered_end_ms"]
    src_dur = audio_end + 3000
    plan = dubbing._plan_freezes(measures, _bars(trim_langs=["ar"]), trim=True,
                                 source_duration_ms=src_dur)
    report = dubbing._build_sync_report(
        repo, VID, "ar", measures, _bars(trim_langs=["ar"]),
        provider="test", model=None, dub_sha256=None, actor="agent", freeze_plan=plan,
    )
    block = report["languages"]["ar"]
    last = next(c for c in block["cues"] if c["id"] == 1)
    # last cue's per-cue residual is unaffected by the tail trim (which shares its id):
    assert last["trim_planned"] is False
    assert last["drift_ms"] == 0
    # total_trim_ms DOES include the tail (the mux applies it):
    assert block["total_trim_ms"] == 3000


# --- 2. _build_sync_report makes freeze-planned cues honest ------------------

def test_build_sync_report_freeze_cue_is_honest(repo: Path):
    # Drive just far enough to have a project dir + captions for the language.
    _drive_to_audio_qa_gate(repo, freeze=False)  # config state irrelevant to this pure check
    measures = _measures_back_to_back([(0, 0, 2000, 3000), (1, 2000, 4000, 1800)])
    plan = dubbing._plan_freezes(measures, _bars())
    report = dubbing._build_sync_report(
        repo, VID, "en", measures, _bars(),
        provider="test", model=None, dub_sha256=None, actor="agent",
        freeze_plan=plan,
    )
    block = report["languages"]["en"]
    # cue1's 609ms hold cancels the propagated backlog: post-freeze residual is 0 by construction.
    frozen = next(c for c in block["cues"] if c["id"] == 1)
    assert frozen["freeze_planned"] is True
    assert frozen["freeze_ms"] == 609
    assert frozen["drift_ms"] == 0
    assert frozen["over_stretch_cap"] is False
    assert frozen["over_tolerance"] is False
    assert block["cues_over_stretch_cap"] == []
    assert block["cues_over_tolerance"] == []
    assert block["freeze_frame_enabled"] is True
    assert block["total_freeze_ms"] == 609
    assert block["total_trim_ms"] == 0


def test_build_sync_report_hold_mode_leaves_honest_residual(repo: Path):
    # Hold-only (Model B): after a big gap the audio underruns the held picture; the residual is
    # surfaced honestly as signed drift, NOT hidden. With trim mode it would be 0 instead.
    _drive_to_audio_qa_gate(repo, freeze=False)
    measures = _measures_back_to_back(
        [(0, 0, 2000, 3000), (1, 2000, 4000, 1800), (2, 8000, 10000, 2000)]
    )
    plan = dubbing._plan_freezes(measures, _bars())  # hold mode: no trims
    report = dubbing._build_sync_report(
        repo, VID, "en", measures, _bars() | {"per_cue_drift_tolerance_ms": 100},
        provider="test", model=None, dub_sha256=None, actor="agent",
        freeze_plan=plan,
    )
    block = report["languages"]["en"]
    tail = next(c for c in block["cues"] if c["id"] == 2)
    # residual = rendered_start(8000) - (caption_start(8000) + cum_freeze(609) - cum_trim(0)) = -609
    assert tail["drift_ms"] == -609
    assert tail["trim_planned"] is False
    assert block["max_abs_drift_ms"] == 609
    assert 2 in block["cues_over_tolerance"]  # honestly flagged at a 100ms tolerance
    assert block["total_trim_ms"] == 0


def test_build_sync_report_trim_mode_drives_residual_to_zero(repo: Path):
    # Trim mode (Model A): the same underrun is removed by trimming the picture -> residual 0.
    _drive_to_audio_qa_gate(repo, freeze=False)
    measures = _measures_back_to_back(
        [(0, 0, 2000, 3000), (1, 2000, 4000, 1800), (2, 8000, 10000, 2000)]
    )
    plan = dubbing._plan_freezes(measures, _bars(trim_langs=["ar"]), trim=True)
    report = dubbing._build_sync_report(
        repo, VID, "ar", measures, _bars(trim_langs=["ar"]) | {"per_cue_drift_tolerance_ms": 100},
        provider="test", model=None, dub_sha256=None, actor="agent",
        freeze_plan=plan,
    )
    block = report["languages"]["ar"]
    assert block["max_abs_drift_ms"] == 0
    assert block["cues_over_tolerance"] == []
    assert block["total_freeze_ms"] == 609
    assert block["total_trim_ms"] == 609
    trimmed = next(c for c in block["cues"] if c["id"] == 2)
    assert trimmed["trim_planned"] is True
    assert trimmed["trim_ms"] == 609
    assert trimmed["drift_ms"] == 0


def test_build_sync_report_without_plan_keeps_over_cap(repo: Path):
    _drive_to_audio_qa_gate(repo, freeze=False)
    # A cue whose natural far exceeds slot*max_time_stretch stays over-cap when freeze is off.
    measures = [{
        "id": 0, "caption_start_ms": 0, "caption_end_ms": 2000,
        "rendered_start_ms": 0, "rendered_end_ms": 4000, "natural_ms": 5000,
        "stretch_factor": 2.5, "over_stretch_cap": True,
    }]
    report = dubbing._build_sync_report(
        repo, VID, "en", measures, _bars(freeze=False),
        provider="test", model=None, dub_sha256=None, actor="agent",
        freeze_plan=None,
    )
    block = report["languages"]["en"]
    assert block["cues_over_stretch_cap"] == [0]
    assert block["freeze_frame_enabled"] is False
    assert block["total_freeze_ms"] == 0


# --- 3. analyze_sync findings ------------------------------------------------

def test_analyze_sync_freeze_only_track_passes_with_notes():
    block = {
        "language": "en", "max_abs_drift_ms": 0, "cumulative_offset_ms": 0,
        "cues_over_tolerance": [], "cues_over_stretch_cap": [],
        "freeze_frame_enabled": True, "total_freeze_ms": 609,
        "cues": [
            {"id": 0, "freeze_planned": False, "freeze_ms": 0},
            {"id": 1, "freeze_planned": True, "freeze_ms": 609},
        ],
    }
    result = dubbing.analyze_sync(block, _bars())
    assert result["decision"] == "PASS"
    cats = [f["category"] for f in result["findings"]]
    assert "audio-freeze-planned" in cats
    assert all(f["severity"] != "major" for f in result["findings"])
    assert result["metrics"]["cues_frozen"] == 1
    assert result["metrics"]["total_freeze_ms"] == 609


def test_analyze_sync_excessive_freeze_is_major():
    block = {
        "language": "en", "max_abs_drift_ms": 0, "cumulative_offset_ms": 0,
        "cues_over_tolerance": [], "cues_over_stretch_cap": [],
        "freeze_frame_enabled": True, "total_freeze_ms": 9000,
        "cues": [{"id": 0, "freeze_planned": True, "freeze_ms": 9000}],
    }
    result = dubbing.analyze_sync(block, _bars())
    assert result["decision"] == "CONDITIONAL_PASS"
    majors = [f for f in result["findings"] if f["severity"] == "major"]
    assert len(majors) == 1
    assert majors[0]["category"] == "audio-freeze-excessive"


# --- 4. _freeze_windows retiming math ----------------------------------------

def test_freeze_windows_offsets_downstream():
    plan = {"freezes": [
        {"cue_id": 1, "at_ms": 4000, "freeze_ms": 600, "natural_ms": 3000,
         "slot_ms": 2000, "stretch_factor": 1.5},
        {"cue_id": 3, "at_ms": 8000, "freeze_ms": 300, "natural_ms": 2000,
         "slot_ms": 1500, "stretch_factor": 1.33},
    ]}
    windows = packaging._freeze_windows(plan, source_duration_ms=10000)
    # offset = window_start + cumulative_freeze (slice_caption_doc re-bases to window-local, so
    # this preserves absolute timing shifted by the accumulated hold):
    #   [0,4000)   +0    -> p maps to p
    #   [4000,8000) +600 (4000+600) -> p maps to p+600
    #   [8000,10000) +900 (8000+900) -> p maps to p+900
    assert windows == [(0, 4000, 0), (4000, 8000, 4600), (8000, 10000, 8900)]


def test_freeze_windows_empty_plan_is_single_window():
    windows = packaging._freeze_windows({"freezes": []}, source_duration_ms=6000)
    assert windows == [(0, 6000, 0)]


def test_freeze_windows_folds_trims():
    # A hold shifts downstream later; a trim collapses [at, at+trim) of source and shifts the
    # remainder earlier. Net offset at a boundary = cumulative_freeze - cumulative_trim.
    plan = {
        "freezes": [
            {"cue_id": 1, "at_ms": 2000, "freeze_ms": 600, "natural_ms": 1800,
             "slot_ms": 2000, "stretch_factor": 0.9},
        ],
        "trims": [
            {"cue_id": 2, "at_ms": 8000, "trim_ms": 600, "natural_ms": 2000,
             "slot_ms": 2000, "stretch_factor": 1.0},
        ],
    }
    windows = packaging._freeze_windows(plan, source_duration_ms=10000)
    # [0,2000)     net 0            -> p -> p
    # [2000,8000)  net +600         -> p -> p+600
    # trim drops [8000,8600) of source; cursor jumps to 8600, net back to 0
    # [8600,10000) net 0            -> p -> p
    assert windows == [(0, 2000, 0), (2000, 8000, 2600), (8600, 10000, 8600)]


# --- shared driver -----------------------------------------------------------

def _drive_to_audio_qa_gate(root: Path, *, freeze: bool, targets=("en",), audio=("en",)) -> str:
    """Init + drive to VIDEO_MUX-ready (through the dub-import no-engine path). `freeze` sets the
    fixture-local company.local.json bar so mux/package see freeze policy on or off."""
    _write_local_config(root, freeze=freeze)
    project.init_project(root, VID, url="https://youtube.com/watch?v=vid00000006",
                         target_languages=list(targets), audio_languages=list(audio))
    paths = root / "projects" / VID
    _fake_source_video(paths / "source" / "video.mp4", duration_ms=6000)
    (paths / "source" / "metadata.json").write_text(json.dumps({
        "title": "Test speech", "channel": "Test channel",
        "url": "https://youtube.com/watch?v=vid00000006", "duration_seconds": 6.0,
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
    manifest = json.loads((paths / "artifacts" / "manifest.json").read_text())
    src_hash = next(a["sha256"] for a in manifest["artifacts"] if a["type"] == "transcript")
    approvals.grant_approval(root, VID, "transcript_qa", "human", [src_hash],
                             scope="transcript", language=None)
    state.transition(root, VID, "SEGMENT_RESOLUTION", "human")
    from video_translation_house import segments as segments_mod
    segments_mod.run_resolve(root, VID, advance=True)

    for lang in targets:
        translate.export_worksheet(root, VID, lang)
        ws_path = paths / "captions" / f"{lang}.worksheet.json"
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

    src = _silence_wav(paths / "tmp" / "dub_src.wav", duration_ms=6000)
    dub_langs = [lang for lang in targets if lang in audio]
    for i, lang in enumerate(dub_langs):
        dubbing.import_dub(root, VID, lang, from_path=src, advance=(i == len(dub_langs) - 1))
    dubbing.run_audio_qa(root, VID)
    state.transition(root, VID, "AUDIO_SYNC_ADJUST", "agent")
    state.transition(root, VID, "AUDIO_QA_GATE", "agent")
    manifest = json.loads((paths / "artifacts" / "manifest.json").read_text())
    for lang in dub_langs:
        dub_hash = next(a["sha256"] for a in manifest["artifacts"]
                        if a["type"] == "dub-wav" and a.get("language") == lang)
        approvals.grant_approval(root, VID, "audio_qa", "human", [dub_hash],
                                 scope="audio", language=lang)
    state.transition(root, VID, "VIDEO_MUX", "human")
    return VID


def _register_freeze_plan(
    root: Path, language: str, freezes: list[dict], trims: list[dict] | None = None,
    *, trim_mode: bool = False,
) -> None:
    """Hand-register a freeze-plan artifact via the CLI-owned writer (import_dub makes none)."""
    manifest = json.loads((root / "projects" / VID / "artifacts" / "manifest.json").read_text())
    dub = next(a for a in manifest["artifacts"]
               if a["type"] == "dub-wav" and a.get("language") == language)
    dubbing._write_freeze_plan(
        root, VID, language, {"freezes": freezes, "trims": trims or []},
        dubbing._audio_quality_bars(root), trim_mode=trim_mode,
        dub_sha256=dub["sha256"], actor="agent", source_artifact_id=dub["artifact_id"],
    )


# --- 5. freeze_segment produces a v+a clip of the requested length -----------

@ffmpeg_required
def test_freeze_segment_duration_and_streams(tmp_path: Path):
    from video_translation_house import media
    src = _fake_source_video(tmp_path / "src.mp4", duration_ms=4000)
    out = media.freeze_segment(src, tmp_path / "freeze.mp4", at_seconds=1.0, duration_ms=1500,
                               audio_sample_rate=44100, audio_channels=2)
    assert out.is_file()
    dur_ms = media.audio_duration_ms(out)
    assert abs(dur_ms - 1500) < 300  # within a couple frames of the requested hold
    summary = media.probe_summary(out)
    assert summary["video"].get("codec")
    assert summary["audio"].get("codec")


# --- 6. run_mux applies the freeze plan --------------------------------------

@ffmpeg_required
def test_mux_applies_freeze_plan_and_traces_it(repo: Path):
    _drive_to_audio_qa_gate(repo, freeze=True)
    # Freeze at t=2500ms (the end of cue 0 — a real cue boundary; freezes never split a cue).
    _register_freeze_plan(repo, "en", [
        {"cue_id": 0, "at_ms": 2500, "freeze_ms": 800, "natural_ms": 3300,
         "slot_ms": 2500, "stretch_factor": 1.32},
    ])
    out = packaging.run_mux(repo, VID, "en", advance=True)
    assert out["freeze_applied"] is True
    assert out["total_freeze_ms"] == 800

    from video_translation_house import media
    dubbed = repo / "projects" / VID / "video" / "en" / "dubbed.mp4"
    assert dubbed.is_file()
    # Retimed picture is ~6800ms (6000 source + 800 freeze); the 6000ms dub is the shorter
    # stream so -shortest lands the container ~6000ms (captions run to 6800, past the audio).
    assert media.audio_duration_ms(dubbed) >= 5900

    # dubbed-video artifact traces to BOTH the dub and the freeze-plan artifact.
    art = out["artifact"]
    manifest = json.loads((repo / "projects" / VID / "artifacts" / "manifest.json").read_text())
    plan_art = next(a for a in manifest["artifacts"]
                    if a["type"] == "freeze-plan" and a.get("language") == "en")
    assert plan_art["artifact_id"] in art["source_artifact_ids"]
    assert art["provenance"].get("freeze_plan_artifact_id") == plan_art["artifact_id"]

    # Scratch dir is cleaned up.
    assert not (dubbed.parent / "_freeze_work").exists()


# --- 6b. trim mode: retimed video is source + freeze - trim ------------------

@ffmpeg_required
def test_build_retimed_video_trim_shortens_picture(tmp_path: Path):
    from video_translation_house import media
    src = _fake_source_video(tmp_path / "src.mp4", duration_ms=6000)
    # Hold 800ms at 2000; trim 500ms of source at 4000. Net = 6000 + 800 - 500 = 6300ms.
    plan = {
        "freezes": [{"cue_id": 1, "at_ms": 2000, "freeze_ms": 800, "natural_ms": 1800,
                     "slot_ms": 2000, "stretch_factor": 0.9}],
        "trims": [{"cue_id": 2, "at_ms": 4000, "trim_ms": 500, "natural_ms": 2000,
                   "slot_ms": 2000, "stretch_factor": 1.0}],
    }
    out = packaging._build_retimed_video(src, plan, tmp_path / "work")
    assert out.is_file()
    dur_ms = media.audio_duration_ms(out)
    assert abs(dur_ms - 6300) < 400  # within a couple frames of source + freeze - trim


# --- 7. retimed standalone subs at package -----------------------------------

@ffmpeg_required
def test_package_retimes_standalone_subs(repo: Path):
    _drive_to_audio_qa_gate(repo, freeze=True)
    # Freeze at 2500 (end of cue 0): cue 1 (2500..6000) slides to 3300..6800 on the deliverable.
    _register_freeze_plan(repo, "en", [
        {"cue_id": 0, "at_ms": 2500, "freeze_ms": 800, "natural_ms": 3300,
         "slot_ms": 2500, "stretch_factor": 1.32},
    ])
    packaging.run_mux(repo, VID, "en", advance=True)
    state.transition(repo, VID, "FINAL_QA_GATE", "agent")
    packaging.run_final_qa(repo, VID)
    manifest = json.loads((repo / "projects" / VID / "artifacts" / "manifest.json").read_text())
    vid_hash = next(a["sha256"] for a in manifest["artifacts"]
                    if a["type"] == "dubbed-video" and a.get("language") == "en")
    approvals.grant_approval(repo, VID, "final_qa", "human", [vid_hash],
                             scope="video", language=None)
    state.transition(repo, VID, "PACKAGE", "human")
    packaging.run_package(repo, VID)

    # The standalone VTT in the package is on the post-freeze timeline (canonical doc untouched):
    # cue 0 unchanged (before the freeze), cue 1 shifted +800ms (at/after the freeze).
    pkg_vtt = (repo / "projects" / VID / "packages" / "en" / "captions.en.vtt").read_text()
    canonical_vtt = (repo / "projects" / VID / "captions" / "captions.en.vtt").read_text()
    assert pkg_vtt != canonical_vtt
    assert "00:00:00.000 --> 00:00:02.500" in pkg_vtt  # cue 0 unchanged
    assert "00:00:03.300 --> 00:00:06.800" in pkg_vtt  # cue 1 shifted by the 800ms hold
    # The canonical caption doc itself was never rewritten.
    assert "00:00:02.500 --> 00:00:06.000" in canonical_vtt


# --- 8. final QA passes the freeze-planned track -----------------------------

@ffmpeg_required
def test_final_qa_passes_freeze_planned_track(repo: Path):
    _drive_to_audio_qa_gate(repo, freeze=True)
    _register_freeze_plan(repo, "en", [
        {"cue_id": 0, "at_ms": 2500, "freeze_ms": 800, "natural_ms": 3300,
         "slot_ms": 2500, "stretch_factor": 1.32},
    ])
    packaging.run_mux(repo, VID, "en", advance=True)
    state.transition(repo, VID, "FINAL_QA_GATE", "agent")
    result = packaging.run_final_qa(repo, VID)
    assert result["decision"] == "PASS"
    # Retiming the picture to the audio keeps |container - dub| small.
    assert abs(result["languages"]["en"]["av_drift_ms"]) <= packaging._AV_ALIGN_TOLERANCE_MS


# --- 9. regression: default-off keeps the old clamp behavior -----------------

@ffmpeg_required
def test_freeze_off_uses_plain_copy_path(repo: Path):
    _drive_to_audio_qa_gate(repo, freeze=False)
    out = packaging.run_mux(repo, VID, "en", advance=True)
    assert out["freeze_applied"] is False
    assert out["total_freeze_ms"] == 0
    art = out["artifact"]
    assert art["provenance"] == {} or "freeze_plan_artifact_id" not in art["provenance"]


# --- 10. import_dub never produces a freeze plan -----------------------------

@ffmpeg_required
def test_import_dub_skips_freeze_plan_even_when_enabled(repo: Path):
    # Drive to CAPTION_VALIDATION, then import a dub with the freeze bar ON.
    _write_local_config(repo, freeze=True)
    project.init_project(repo, VID, url="https://youtube.com/watch?v=vid00000006",
                         target_languages=["en"], audio_languages=["en"])
    paths = repo / "projects" / VID
    _fake_source_video(paths / "source" / "video.mp4", duration_ms=6000)
    (paths / "source" / "metadata.json").write_text(json.dumps({
        "title": "Test speech", "channel": "Test channel",
        "url": "https://youtube.com/watch?v=vid00000006", "duration_seconds": 6.0,
    }))
    state.transition(repo, VID, "LANGUAGE_ID", "agent")
    langid.set_language(repo, VID, "fa", source="manual", confidence=0.9)
    state.transition(repo, VID, "TRANSCRIPTION", "agent")
    result = asr.ASRResult(
        provider="manual", model=None, language="fa",
        cues=[asr.TranscriptCue(0, 0, 6000, "جمله.", confidence=-0.2)],
        has_word_timing=False, duration_seconds=6.0,
    )
    doc = transcript.build_transcript_doc(VID, "fa", result, actor="agent")
    transcript.write_transcript(repo, VID, doc, actor="agent")
    state.transition(repo, VID, "TRANSCRIPT_QA_GATE", "agent")
    from video_translation_house import transcript_qa
    transcript_qa.run_transcript_qa(repo, VID)
    manifest = json.loads((paths / "artifacts" / "manifest.json").read_text())
    src_hash = next(a["sha256"] for a in manifest["artifacts"] if a["type"] == "transcript")
    approvals.grant_approval(repo, VID, "transcript_qa", "human", [src_hash],
                             scope="transcript", language=None)
    state.transition(repo, VID, "SEGMENT_RESOLUTION", "human")
    from video_translation_house import segments as segments_mod
    segments_mod.run_resolve(repo, VID, advance=True)
    translate.export_worksheet(repo, VID, "en")
    ws_path = paths / "captions" / "en.worksheet.json"
    ws = json.loads(ws_path.read_text())
    for cue in ws["cues"]:
        cue["target_text"] = f"line {cue['id']}."
    ws_path.write_text(json.dumps(ws, ensure_ascii=False))
    out = translate.import_worksheet(repo, VID, "en", advance=True)
    approvals.grant_approval(repo, VID, "translation_qa", "human",
                             [out["artifact"]["sha256"]], scope="captions", language="en")
    translate.run_translation_qa(repo, VID)
    state.transition(repo, VID, "CAPTION_TIMING", "human")
    translate.build_captions(repo, VID, "en")
    translate.run_caption_validation(repo, VID)
    state.transition(repo, VID, "CAPTION_VALIDATION", "agent")

    src = _silence_wav(paths / "tmp" / "dub_src.wav", duration_ms=6000)
    dub_result = dubbing.import_dub(repo, VID, "en", from_path=src, advance=True)
    assert dub_result["freeze_plan"] is None
    assert "freeze_plan_skipped_reason" in dub_result
    # No freeze-plan artifact was registered.
    manifest = json.loads((paths / "artifacts" / "manifest.json").read_text())
    assert not any(a["type"] == "freeze-plan" for a in manifest["artifacts"])
