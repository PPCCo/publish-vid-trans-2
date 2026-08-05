"""Tests for source-video selection: resolution, snapping, clip-local timelines, the
selection_hash provenance seam, and the assemble-time cut/join package layout.

Unit tests exercise the pure math (timecode parsing, bounds/overlap/order, snap vs exact,
clip-local transcript derivation, caption slice offsets, hash stability) with no binaries.
Integration tests (ffmpeg-guarded, driven through the no-engine import paths) cover the three
target use cases: whole-video fast path unchanged, join_clips=false -> per-clip deliverables,
join_clips=true -> one joined.mp4 with continuous captions, and caption-only+join.
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
    segments,
    state,
    transcript,
    translate,
)
from video_translation_house.captions import slice_caption_doc  # noqa: E402
from video_translation_house.engines import asr  # noqa: E402
from video_translation_house.errors import ConfigurationError  # noqa: E402
from video_translation_house.util import executable  # noqa: E402

VID = "yt-vid00000007"

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


# A transcript whose cue boundaries are the snap candidates: {0, 3000, 9000, 12000, 20000}.
def _transcript_doc() -> dict:
    return {
        "schema_version": "1.0",
        "project_id": VID,
        "language": "fa",
        "engine": {"provider": "manual", "model": None},
        "cues": [
            {"id": 0, "start_ms": 0, "end_ms": 3000, "text": "one"},
            {"id": 1, "start_ms": 3000, "end_ms": 9000, "text": "two"},
            {"id": 2, "start_ms": 12000, "end_ms": 20000, "text": "three"},
        ],
        "created_at": "2026-01-01T00:00:00Z",
    }


def _sel(*windows, **extra) -> dict:
    out = {"windows": list(windows)}
    out.update(extra)
    return out


def _w(start_ms: int, end_ms: int, **extra) -> dict:
    """Build a window from millisecond bounds. Timecodes are emitted as ``MM:SS.mmm`` strings
    (the schema's string form) rather than bare numbers, since bare numbers parse as SECONDS."""
    def tc(ms: int) -> str:
        m, rem = divmod(int(ms), 60000)
        s, mm = divmod(rem, 1000)
        return f"{m:02d}:{s:02d}.{mm:03d}"
    w = {"start": tc(start_ms), "end": tc(end_ms)}
    w.update(extra)
    return w


# --- timecode parsing --------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("00:00:03.000", 3000),
    ("01:02:03", 3723000),
    ("02:30", 150000),
    ("12.5", 12500),
    (12.5, 12500),
    (0, 0),
    ("0", 0),
    ("00:00:00.250", 250),
])
def test_parse_timecode_ok(value, expected):
    assert segments.parse_timecode(value) == expected


@pytest.mark.parametrize("value", [
    "", "  ", "abc", "-5", -5, True, False, None, "00:60:00", "00:00:60", [1, 2],
])
def test_parse_timecode_rejects(value):
    with pytest.raises(ConfigurationError):
        segments.parse_timecode(value)


# --- whole-video fast path ---------------------------------------------------

def test_numeric_seconds_windows_validate_against_schema(repo: Path):
    """The schema and parse_timecode agree that a window edge may be a number of seconds
    (not only a timecode string). Guards the two staying in sync."""
    from video_translation_house.validation import validate_data
    cfg = {
        "schema_version": "1.0", "project_id": VID,
        "source": {"url": "https://x/y"}, "target_languages": ["en"],
        "join_clips": True,
        "selection": {"windows": [{"start": 3, "end": 9.5, "exact": True}]},
    }
    assert validate_data(repo, cfg, "project.schema.json") == []
    doc = segments.resolve_segments(
        project_id=VID, selection=cfg["selection"], join_clips=True,
        source_duration_ms=20000, transcript=None, actor="agent",
    )
    seg = doc["segments"][0]
    assert (seg["start_ms"], seg["end_ms"]) == (3000, 9500)


def test_null_selection_is_whole_video():
    doc = segments.resolve_segments(
        project_id=VID, selection=None, join_clips=True,
        source_duration_ms=20000, transcript=_transcript_doc(), actor="agent",
    )
    assert doc["whole_video"] is True
    assert len(doc["segments"]) == 1
    seg = doc["segments"][0]
    assert (seg["start_ms"], seg["end_ms"]) == (0, 20000)
    assert seg["id"] == "whole"
    assert seg["exact"] is True


# --- resolution bounds / order / overlap / empty -----------------------------

def test_empty_windows_rejected():
    with pytest.raises(ConfigurationError):
        segments.resolve_segments(
            project_id=VID, selection=_sel(), join_clips=True,
            source_duration_ms=20000, transcript=None, actor="agent",
        )


def test_end_not_after_start_rejected():
    with pytest.raises(ConfigurationError):
        segments.resolve_segments(
            project_id=VID, selection=_sel(_w(5000, 5000), snap_edges=False),
            join_clips=True, source_duration_ms=20000, transcript=None, actor="agent",
        )


def test_window_out_of_bounds_rejected():
    with pytest.raises(ConfigurationError):
        segments.resolve_segments(
            project_id=VID, selection=_sel(_w(1000, 99000), snap_edges=False),
            join_clips=True, source_duration_ms=20000, transcript=None, actor="agent",
        )


def test_duplicate_ids_rejected():
    with pytest.raises(ConfigurationError):
        segments.resolve_segments(
            project_id=VID,
            selection=_sel(_w(0, 1000, id="a"), _w(2000, 3000, id="a"), snap_edges=False),
            join_clips=True, source_duration_ms=20000, transcript=None, actor="agent",
        )


def test_out_of_order_allowed_preserves_list_order_and_notes():
    doc = segments.resolve_segments(
        project_id=VID,
        selection=_sel(_w(10000, 12000), _w(1000, 2000), snap_edges=False),
        join_clips=True, source_duration_ms=20000, transcript=None, actor="agent",
    )
    # list order preserved (re-sequencing is the point)
    assert [s["start_ms"] for s in doc["segments"]] == [10000, 1000]
    assert any("not in source-time order" in n for n in doc["notes"])


def test_overlap_allowed_with_note():
    doc = segments.resolve_segments(
        project_id=VID,
        selection=_sel(_w(1000, 5000), _w(4000, 8000), snap_edges=False),
        join_clips=True, source_duration_ms=20000, transcript=None, actor="agent",
    )
    assert any("overlap" in n for n in doc["notes"])


# --- snapping vs exact -------------------------------------------------------

def test_snap_moves_edges_to_nearest_cue_boundary():
    # requested 3100 -> nearest boundary 3000; 8900 -> 9000. radius default 2000.
    doc = segments.resolve_segments(
        project_id=VID, selection=_sel({"start": "00:00:03.100", "end": "00:00:08.900"}),
        join_clips=True, source_duration_ms=20000, transcript=_transcript_doc(), actor="agent",
    )
    seg = doc["segments"][0]
    assert (seg["start_ms"], seg["end_ms"]) == (3000, 9000)
    assert seg["snapped"] is True
    assert seg["exact"] is False


def test_per_window_exact_overrides_project_snap():
    # snap_edges default True, but exact:true forces the requested timecodes.
    doc = segments.resolve_segments(
        project_id=VID,
        selection=_sel(_w(3100, 8900, exact=True)),
        join_clips=True, source_duration_ms=20000, transcript=_transcript_doc(), actor="agent",
    )
    seg = doc["segments"][0]
    assert (seg["start_ms"], seg["end_ms"]) == (3100, 8900)
    assert seg["exact"] is True
    assert seg["snapped"] is False


def test_no_snap_candidate_keeps_requested():
    # boundary set has nothing within 2000ms of 49000/55000 (source is longer here).
    doc = segments.resolve_segments(
        project_id=VID, selection=_sel(_w(49000, 55000)),
        join_clips=True, source_duration_ms=60000, transcript=_transcript_doc(), actor="agent",
    )
    seg = doc["segments"][0]
    assert (seg["start_ms"], seg["end_ms"]) == (49000, 55000)


# --- selection_hash ----------------------------------------------------------

def test_selection_hash_stable_across_reresolution():
    sel = _sel(_w(3000, 9000, exact=True))
    kw = dict(project_id=VID, join_clips=True, source_duration_ms=20000,
              transcript=_transcript_doc(), actor="agent")
    a = segments.resolve_segments(selection=dict(sel), **kw)["selection_hash"]
    b = segments.resolve_segments(selection=dict(sel), **kw)["selection_hash"]
    assert a == b
    assert a.startswith("sha256:")


def test_selection_hash_changes_when_bounds_change():
    kw = dict(project_id=VID, join_clips=True, source_duration_ms=20000,
              transcript=_transcript_doc(), actor="agent")
    a = segments.resolve_segments(
        selection=_sel(_w(3000, 9000, exact=True)), **kw)["selection_hash"]
    b = segments.resolve_segments(
        selection=_sel(_w(3000, 12000, exact=True)), **kw)["selection_hash"]
    assert a != b


def test_selection_hash_changes_when_join_flips():
    sel = _sel(_w(3000, 9000, exact=True))
    kw = dict(project_id=VID, source_duration_ms=20000, transcript=_transcript_doc(),
              actor="agent")
    a = segments.resolve_segments(selection=dict(sel), join_clips=True, **kw)["selection_hash"]
    b = segments.resolve_segments(selection=dict(sel), join_clips=False, **kw)["selection_hash"]
    assert a != b


# --- clip-local transcript derivation ----------------------------------------

def test_rebase_transcript_subtracts_offset_and_trims_edges():
    full = _transcript_doc()
    # window [3000, 12000): cue1 [3000,9000] -> [0,6000]; cue2 [12000,20000] excluded (starts at end)
    seg = {"index": 0, "id": "seg0", "start_ms": 3000, "end_ms": 12000}
    clip = segments.rebase_transcript_for_segment(full, seg, project_id=VID)
    assert [(c["start_ms"], c["end_ms"]) for c in clip["cues"]] == [(0, 6000)]
    assert clip["cues"][0]["id"] == 0
    assert clip["segment"] == {"index": 0, "id": "seg0",
                               "source_start_ms": 3000, "source_end_ms": 12000}
    assert clip["duration_seconds"] == 9.0


def test_rebase_transcript_clips_straddling_cue():
    full = _transcript_doc()
    # window [5000, 7000) straddles cue1 [3000,9000] -> clipped to [0,2000] clip-local.
    seg = {"index": 1, "id": "seg1", "start_ms": 5000, "end_ms": 7000}
    clip = segments.rebase_transcript_for_segment(full, seg, project_id=VID)
    assert [(c["start_ms"], c["end_ms"]) for c in clip["cues"]] == [(0, 2000)]


# --- caption slice offset math -----------------------------------------------

def _caption_doc() -> dict:
    return {
        "schema_version": "1.0", "project_id": VID, "language": "en",
        "engine": {"provider": "manual", "model": None},
        "cues": [
            {"id": 0, "start_ms": 1000, "end_ms": 4000, "target_text": "a"},
            {"id": 1, "start_ms": 5000, "end_ms": 8000, "target_text": "b"},
            {"id": 2, "start_ms": 13000, "end_ms": 16000, "target_text": "c"},
        ],
        "created_at": "2026-01-01T00:00:00Z",
    }


def test_slice_caption_doc_clip_local_zero():
    out = slice_caption_doc(_caption_doc(), start_ms=3000, end_ms=9000, offset_ms=0)
    # cue0 [1000,4000] -> [0,1000]; cue1 [5000,8000] -> [2000,5000]; cue2 excluded
    assert [(c["start_ms"], c["end_ms"]) for c in out["cues"]] == [(0, 1000), (2000, 5000)]
    assert [c["id"] for c in out["cues"]] == [0, 1]


def test_slice_caption_doc_with_offset_for_joined_seam():
    # Same window but placed after 10000ms of already-joined material.
    out = slice_caption_doc(_caption_doc(), start_ms=3000, end_ms=9000, offset_ms=10000)
    assert [(c["start_ms"], c["end_ms"]) for c in out["cues"]] == [(10000, 11000), (12000, 15000)]


# ============================================================================
# Integration: drive the real pipeline through the no-engine paths.
# ============================================================================

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


def _silence_wav(dest: Path, *, duration_ms: int) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
         "-t", f"{duration_ms / 1000:.3f}", "-ac", "1", "-ar", "24000",
         "-c:a", "pcm_s16le", str(dest)],
        capture_output=True, check=True,
    )
    return dest


def _drive_to_package(
    root: Path, *, selection=None, join_clips=True, targets=("en",), audio=("en",),
    duration_ms=20000,
) -> str:
    """Init with a selection, drive through SEGMENT_RESOLUTION -> ... -> PACKAGE (state), so a
    caller can then run_package. Uses the import (no-engine) paths so no ML is needed."""
    project.init_project(root, VID, url="https://youtube.com/watch?v=vid00000007",
                         target_languages=list(targets), audio_languages=list(audio),
                         selection=selection, join_clips=join_clips)
    paths = root / "projects" / VID
    _fake_source_video(paths / "source" / "video.mp4", duration_ms=duration_ms)
    (paths / "source" / "audio.wav").parent.mkdir(parents=True, exist_ok=True)
    _silence_wav(paths / "source" / "audio.wav", duration_ms=duration_ms)
    (paths / "source" / "metadata.json").write_text(json.dumps({
        "title": "Test speech", "channel": "Test channel",
        "url": "https://youtube.com/watch?v=vid00000007", "duration_seconds": duration_ms / 1000,
    }))

    state.transition(root, VID, "LANGUAGE_ID", "agent")
    langid.set_language(root, VID, "fa", source="manual", confidence=0.9)
    state.transition(root, VID, "TRANSCRIPTION", "agent")
    result = asr.ASRResult(
        provider="manual", model=None, language="fa",
        cues=[
            asr.TranscriptCue(0, 0, 3000, "جمله اول.", confidence=-0.2),
            asr.TranscriptCue(1, 3000, 9000, "جمله دوم.", confidence=-0.3),
            asr.TranscriptCue(2, 12000, 20000, "جمله سوم.", confidence=-0.3),
        ],
        has_word_timing=False, duration_seconds=duration_ms / 1000,
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
    segments.run_resolve(root, VID, advance=True)

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

    dub_langs = [lang for lang in targets if lang in audio]
    if not dub_langs:
        # Fully caption-only project: the sanctioned edge skips dubbing/mux/final entirely.
        state.transition(root, VID, "PACKAGE", "agent")
        return VID

    src = _silence_wav(paths / "tmp" / "dub_src.wav", duration_ms=duration_ms)
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

    # Mux each dub-enabled track, then final QA + gate -> PACKAGE state.
    for lang in dub_langs:
        packaging.run_mux(root, VID, lang, advance=(lang == dub_langs[-1]))
    state.transition(root, VID, "FINAL_QA_GATE", "agent")
    packaging.run_final_qa(root, VID)
    manifest = json.loads((paths / "artifacts" / "manifest.json").read_text())
    vid_hash = next(a["sha256"] for a in manifest["artifacts"]
                    if a["type"] == "dubbed-video" and a.get("language") == dub_langs[0])
    approvals.grant_approval(root, VID, "final_qa", "human", [vid_hash],
                             scope="video", language=None)
    state.transition(root, VID, "PACKAGE", "human")
    return VID


@ffmpeg_required
def test_whole_video_package_layout_unchanged(repo: Path):
    _drive_to_package(repo, selection=None)
    out = packaging.run_package(repo, VID)
    pkg = out["packages"][0]
    assert pkg["selection"] is None  # whole-video: no assemble-time cut/join
    types = {d["type"] for d in pkg["deliverables"]}
    assert {"dubbed-video", "captions-srt", "captions-vtt", "readme"} <= types
    assert "joined-video" not in types and "clip-video" not in types


@ffmpeg_required
def test_three_windows_no_join_produces_per_clip_deliverables(repo: Path):
    selection = _sel(
        _w(0, 3000, exact=True),
        _w(3000, 9000, exact=True),
        _w(12000, 20000, exact=True),
    )
    _drive_to_package(repo, selection=selection, join_clips=False)
    out = packaging.run_package(repo, VID)
    pkg = out["packages"][0]
    assert pkg["selection"]["join_clips"] is False
    assert len(pkg["selection"]["clips"]) == 3

    clip_videos = [d for d in pkg["deliverables"] if d["type"] == "clip-video"]
    assert len(clip_videos) == 3
    assert sorted(d["clip_index"] for d in clip_videos) == [0, 1, 2]
    # each clip carries its own captions
    caps = [d for d in pkg["deliverables"] if d["type"] in ("captions-srt", "captions-vtt")]
    assert {d["clip_index"] for d in caps} == {0, 1, 2}
    assert not any(d["type"] == "joined-video" for d in pkg["deliverables"])

    from video_translation_house.media import probe_summary
    first = repo / "projects" / VID / clip_videos[0]["path"]
    assert probe_summary(first)["video"].get("codec")


@ffmpeg_required
def test_two_windows_join_produces_single_joined_video(repo: Path):
    selection = _sel(
        _w(0, 3000, exact=True),
        _w(12000, 20000, exact=True),
    )
    _drive_to_package(repo, selection=selection, join_clips=True)
    out = packaging.run_package(repo, VID)
    pkg = out["packages"][0]
    assert pkg["selection"]["join_clips"] is True

    joined = [d for d in pkg["deliverables"] if d["type"] == "joined-video"]
    assert len(joined) == 1
    assert not any(d["type"] == "clip-video" for d in pkg["deliverables"])

    from video_translation_house.media import audio_duration_ms, probe_summary
    jpath = repo / "projects" / VID / joined[0]["path"]
    summary = probe_summary(jpath)
    assert summary["video"].get("codec") and summary["audio"].get("codec")
    # duration ~ sum of the two windows (3000 + 8000 = 11000ms), within concat tolerance.
    dur = audio_duration_ms(jpath)
    assert 9500 <= dur <= 12500

    # Joined captions are continuous (a single caption set, no clip_index).
    caps = [d for d in pkg["deliverables"] if d["type"] in ("captions-srt", "captions-vtt")]
    assert caps and all(d.get("clip_index") is None for d in caps)


@ffmpeg_required
def test_caption_only_join_keeps_original_audio_no_dub(repo: Path):
    # target 'ar' is NOT dub-enabled -> caption-only; selection + join -> cut source video.
    selection = _sel(
        _w(0, 3000, exact=True),
        _w(12000, 20000, exact=True),
    )
    _drive_to_package(repo, selection=selection, join_clips=True,
                      targets=("ar",), audio=())
    out = packaging.run_package(repo, VID)
    pkg = next(p for p in out["packages"] if p["language"] == "ar")
    assert pkg["dub_enabled"] is False
    assert pkg["selection"]["join_clips"] is True
    joined = [d for d in pkg["deliverables"] if d["type"] == "joined-video"]
    assert len(joined) == 1

    from video_translation_house.media import probe_summary
    jpath = repo / "projects" / VID / joined[0]["path"]
    summary = probe_summary(jpath)
    assert summary["video"].get("codec") and summary["audio"].get("codec")


# --- provenance: changing the selection invalidates a bound approval ---------

def test_reresolve_changes_selection_hash_provenance(repo: Path):
    """A changed selection re-registers segments with a new selection_hash (the seam that
    auto-invalidates downstream approvals via content-addressing, company rule 6)."""
    project.init_project(repo, VID, url="https://youtube.com/watch?v=vid00000007",
                         target_languages=["en"], audio_languages=["en"],
                         selection=_sel(_w(0, 3000, exact=True)))
    paths = repo / "projects" / VID
    (paths / "source" / "metadata.json").write_text(json.dumps({"duration_seconds": 20.0}))
    state.transition(repo, VID, "LANGUAGE_ID", "agent")
    langid.set_language(repo, VID, "fa", source="manual", confidence=0.9)
    state.transition(repo, VID, "TRANSCRIPTION", "agent")
    result = asr.ASRResult(
        provider="manual", model=None, language="fa",
        # Enough cues for the 20s duration to clear the QA granularity check (~1 cue/10s) so
        # this test exercises segment re-resolution, not transcript QA.
        cues=[asr.TranscriptCue(i, i * 3000, i * 3000 + 2000, f"x{i}.", confidence=-0.2)
              for i in range(3)],
        has_word_timing=False, duration_seconds=20.0,
    )
    transcript.write_transcript(repo, VID, transcript.build_transcript_doc(VID, "fa", result, actor="agent"),
                                actor="agent")
    state.transition(repo, VID, "TRANSCRIPT_QA_GATE", "agent")
    from video_translation_house import transcript_qa
    transcript_qa.run_transcript_qa(repo, VID)
    manifest = json.loads((paths / "artifacts" / "manifest.json").read_text())
    src_hash = next(a["sha256"] for a in manifest["artifacts"] if a["type"] == "transcript")
    approvals.grant_approval(repo, VID, "transcript_qa", "human", [src_hash],
                             scope="transcript", language=None)
    state.transition(repo, VID, "SEGMENT_RESOLUTION", "human")

    first = segments.run_resolve(repo, VID)
    h1 = first["selection_hash"]

    # Human edits project.yaml selection; re-resolve -> different hash + re-registered artifact.
    from video_translation_house.util import atomic_write_yaml, load_yaml
    cfg = load_yaml(paths / "project.yaml")
    cfg["selection"]["windows"][0]["end"] = "00:05.000"
    atomic_write_yaml(paths / "project.yaml", cfg)
    second = segments.run_resolve(repo, VID)
    assert second["selection_hash"] != h1

    seg_artifacts = [a for a in json.loads((paths / "artifacts" / "manifest.json").read_text())["artifacts"]
                     if a["type"] == "segments"]
    hashes = {a.get("provenance", {}).get("selection_hash") for a in seg_artifacts}
    assert h1 in hashes and second["selection_hash"] in hashes
