"""Selection resolution: turn a project's `selection` into canonical, hashed segments.

This is the one new pipeline step (state SEGMENT_RESOLUTION, between the full-source
TRANSCRIPT_QA_GATE and TRANSLATION). It exists so the rest of the pipeline can stay
whole-video-shaped: each resolved segment is a clip-local mini-timeline that translate /
captions / dub already know how to process, and only the media *edges* (cutting at
resolve-time, joining at package-time) differ from the whole-video path.

Design (see plan "cut at the edges, clip-local timeline in the middle"):

  * `selection: null`/absent  => a single implicit segment [0, source_duration]. `whole_video`
    is set true so packaging keeps the byte-for-byte `-c:v copy` fast path (no cut, no join).
  * `selection.windows: [...]` => one resolved segment per window, IN LIST ORDER (out-of-source
    order is allowed and is how re-sequencing works). Each edge is optionally snapped to the
    nearest cue boundary of the QA'd full-source transcript (never a second ASR pass).
  * The resolved list is hashed (`selection_hash`) and that hash is threaded into every
    downstream artifact's `provenance`, so changing the selection re-registers artifacts with
    new hashes and auto-invalidates any bound approval (company rule 6).

Timecodes accept HH:MM:SS(.mmm), MM:SS(.mmm), or plain (fractional) seconds. Everything is
resolved to integer milliseconds on the absolute source timeline.

Nothing here touches state.json/manifest.json directly except through the sanctioned
state/artifact helpers — the CLI owns all state (company rule 1). Snapping and cutting are
pure/offline; cutting shells out to ffmpeg via media.py.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import artifacts as artifacts_mod
from . import media as media_mod
from .errors import ConfigurationError
from .events import append_event
from .paths import ProjectPaths
from .state import transition
from .transcript import load_transcript
from .util import atomic_write_json, hash_json, load_json, load_yaml, project_lock, utc_now
from .validation import require_valid

SCHEMA_VERSION = "1.0"

# Default snap search radius (ms) when the project does not set selection.snap_search_window_ms.
DEFAULT_SNAP_SEARCH_MS = 2000

_STATE = "SEGMENT_RESOLUTION"
# `segments resolve` runs at SEGMENT_RESOLUTION; the full transcript already exists (produced
# at TRANSCRIPTION, approved at the transcript_qa gate whose forward edge lands us here).
_STATES_ALLOWING_RESOLVE = {"SEGMENT_RESOLUTION"}
# `segments cut` may run once resolved, at SEGMENT_RESOLUTION or later (idempotent media op).
_STATES_ALLOWING_CUT = {
    "SEGMENT_RESOLUTION", "TRANSLATION", "TRANSLATION_QA_GATE", "CAPTION_TIMING",
    "CAPTION_VALIDATION", "DUBBING", "AUDIO_SYNC_ADJUST", "AUDIO_QA_GATE",
    "VIDEO_MUX", "FINAL_QA_GATE", "PACKAGE",
}


# --- timecode parsing --------------------------------------------------------

_HMS_RE = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?\s*$")
_SECONDS_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")


def parse_timecode(value: Any) -> int:
    """Parse HH:MM:SS(.mmm) / MM:SS(.mmm) / plain (fractional) seconds -> integer ms.

    Raises ConfigurationError on anything unparseable, empty, or negative. Numbers are
    accepted directly (already in seconds) so JSON like ``{"start": 12.5}`` also works.
    """
    if isinstance(value, bool):  # bool is an int subclass; reject to avoid True==1s surprises
        raise ConfigurationError(f"invalid timecode: {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds < 0:
            raise ConfigurationError(f"negative timecode: {value!r}")
        return round(seconds * 1000)
    if not isinstance(value, str):
        raise ConfigurationError(f"invalid timecode type: {value!r}")
    text = value.strip()
    if not text:
        raise ConfigurationError("empty timecode")
    m = _HMS_RE.match(text)
    if m:
        hours = int(m.group(1)) if m.group(1) else 0
        minutes = int(m.group(2))
        secs = int(m.group(3))
        if minutes >= 60 or secs >= 60:
            raise ConfigurationError(f"minutes/seconds must be < 60 in {value!r}")
        frac = m.group(4) or ""
        millis = int((frac + "000")[:3]) if frac else 0
        return ((hours * 3600 + minutes * 60 + secs) * 1000) + millis
    m = _SECONDS_RE.match(text)
    if m:
        return round(float(m.group(1)) * 1000)
    raise ConfigurationError(
        f"unparseable timecode {value!r}; use HH:MM:SS(.mmm), MM:SS, or seconds"
    )


def _coerce_bool(value: Any, default: bool) -> bool:
    """Defensively coerce 0/1/"0"/"1"/true/false-ish to bool (schema type is boolean, but
    humans hand-edit project.yaml and the use cases passed strings like "0"/"1")."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "no", "n", "off", ""}:
            return False
    raise ConfigurationError(f"cannot interpret {value!r} as a boolean")


# --- snapping ----------------------------------------------------------------

def _boundaries(transcript: dict[str, Any] | None) -> list[int]:
    """Sorted, de-duplicated candidate snap points: every cue start and end (ms)."""
    if not transcript:
        return []
    points: set[int] = set()
    for cue in transcript.get("cues", []) or []:
        try:
            points.add(int(cue["start_ms"]))
            points.add(int(cue["end_ms"]))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(points)


def _nearest_boundary(target: int, boundaries: list[int], radius: int) -> int | None:
    """Nearest boundary to ``target`` within ``radius`` ms, or None if none qualifies.

    Ties (equal distance on both sides) resolve to the earlier boundary for determinism.
    """
    if not boundaries or radius < 0:
        return None
    best: int | None = None
    best_dist = radius + 1
    for b in boundaries:
        dist = abs(b - target)
        if dist < best_dist or (dist == best_dist and best is not None and b < best):
            best, best_dist = b, dist
    return best if best is not None and best_dist <= radius else None


# --- resolution --------------------------------------------------------------

def _source_duration_ms(paths: ProjectPaths) -> int:
    meta_path = paths.source_dir / "metadata.json"
    if not meta_path.is_file():
        raise ConfigurationError(
            "source/metadata.json missing; run ingest before resolving segments"
        )
    seconds = load_json(meta_path).get("duration_seconds")
    if seconds is None:
        raise ConfigurationError("source metadata has no duration_seconds; cannot bound segments")
    return max(0, round(float(seconds) * 1000))


def _effective_snap(window: dict[str, Any], project_snap_edges: bool) -> bool:
    """Whether THIS window should snap. Per-window `exact:true` forces exact (no snap) and
    always wins; otherwise the project `snap_edges` default applies. Returns True to snap."""
    if _coerce_bool(window.get("exact"), False):
        return False
    return project_snap_edges


def resolve_segments(
    *,
    project_id: str,
    selection: dict[str, Any] | None,
    join_clips: bool,
    source_duration_ms: int,
    transcript: dict[str, Any] | None,
    actor: str,
) -> dict[str, Any]:
    """Pure resolution: `selection` -> the segments.json document (no I/O, no state).

    Splitting the pure math out from `run_resolve` keeps it trivially unit-testable (timecode
    parsing, bounds, ordering, snap vs exact, hash stability) with no filesystem or ffmpeg.
    """
    snap_edges = _coerce_bool((selection or {}).get("snap_edges"), True)
    raw_radius = (selection or {}).get("snap_search_window_ms")
    snap_radius = int(raw_radius) if raw_radius is not None else DEFAULT_SNAP_SEARCH_MS
    if snap_radius < 0:
        raise ConfigurationError("snap_search_window_ms must be >= 0")

    notes: list[str] = []
    boundaries = _boundaries(transcript)

    # --- whole-video fast path (null/absent selection) ---
    if selection is None:
        segment = _segment_dict(
            index=0, id_="whole", label=None,
            requested_start=0, requested_end=source_duration_ms,
            start=0, end=source_duration_ms, exact=True, adjustments=[],
            has_speech=bool(boundaries) or None,
        )
        return _document(project_id, True, join_clips, snap_edges, snap_radius,
                         source_duration_ms, [segment], notes, actor)

    windows = selection.get("windows")
    if windows is None or not isinstance(windows, list):
        raise ConfigurationError("selection.windows must be a list (use selection:null for whole video)")
    if len(windows) == 0:
        raise ConfigurationError(
            "selection.windows is empty; use selection:null (or omit selection) for the whole video"
        )

    segments: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    prev_source_start: int | None = None
    out_of_order = False
    for idx, window in enumerate(windows):
        if not isinstance(window, dict):
            raise ConfigurationError(f"selection.windows[{idx}] must be an object")
        req_start = parse_timecode(window.get("start"))
        req_end = parse_timecode(window.get("end"))
        if req_end <= req_start:
            raise ConfigurationError(
                f"selection.windows[{idx}]: end ({req_end}ms) must be greater than start ({req_start}ms)"
            )
        if req_start < 0 or req_end > source_duration_ms:
            raise ConfigurationError(
                f"selection.windows[{idx}]: [{req_start},{req_end}]ms is outside the source "
                f"[0,{source_duration_ms}]ms"
            )

        wid = window.get("id") or f"seg{idx}"
        if wid in seen_ids:
            raise ConfigurationError(f"duplicate window id {wid!r} in selection.windows")
        seen_ids.add(wid)

        do_snap = _effective_snap(window, snap_edges)
        start, end, adjustments = _resolve_edges(
            req_start, req_end, do_snap=do_snap, boundaries=boundaries, radius=snap_radius,
        )
        snapped = start != req_start or end != req_end
        has_speech = _window_has_speech(transcript, start, end) if transcript else None

        segments.append(_segment_dict(
            index=idx, id_=str(wid), label=window.get("label"),
            requested_start=req_start, requested_end=req_end,
            start=start, end=end, exact=not do_snap, adjustments=adjustments,
            has_speech=has_speech,
        ))
        if has_speech is False:
            notes.append(f"segment {idx} ({wid}) contains no transcript speech; dub will be silent")
        if prev_source_start is not None and req_start < prev_source_start:
            out_of_order = True
        prev_source_start = req_start

    if out_of_order:
        notes.append("windows are not in source-time order; output follows list order (re-sequencing)")
    _note_overlaps(segments, notes)

    return _document(project_id, False, join_clips, snap_edges, snap_radius,
                     source_duration_ms, segments, notes, actor)


def _resolve_edges(
    req_start: int, req_end: int, *, do_snap: bool, boundaries: list[int], radius: int,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Resolve one window's edges, returning (start, end, adjustments)."""
    adjustments: list[dict[str, Any]] = []
    if not do_snap:
        adjustments.append(_adj("start", "exact", req_start, req_start))
        adjustments.append(_adj("end", "exact", req_end, req_end))
        return req_start, req_end, adjustments

    start = req_start
    snap_start = _nearest_boundary(req_start, boundaries, radius)
    if snap_start is None:
        adjustments.append(_adj("start", "unsnapped-no-candidate", req_start, req_start,
                                detail=f"no cue boundary within {radius}ms"))
    else:
        start = snap_start
        adjustments.append(_adj("start", "snapped", req_start, snap_start))

    end = req_end
    snap_end = _nearest_boundary(req_end, boundaries, radius)
    if snap_end is None:
        adjustments.append(_adj("end", "unsnapped-no-candidate", req_end, req_end,
                                detail=f"no cue boundary within {radius}ms"))
    elif snap_end <= start:
        # Snapping would invert or zero the window — refuse this edge, keep the request.
        adjustments.append(_adj("end", "snap-refused", req_end, req_end,
                                detail=f"snap candidate {snap_end}ms would not follow start {start}ms"))
    else:
        end = snap_end
        adjustments.append(_adj("end", "snapped", req_end, snap_end))

    # If the start snap inverted against a non-snapped end, undo the start snap too.
    if end <= start:
        start = req_start
        for a in adjustments:
            if a["edge"] == "start" and a["kind"] == "snapped":
                a["kind"] = "snap-refused"
                a["resolved_ms"] = req_start
                a["detail"] = "reverted: snapped start did not precede end"
    return start, end, adjustments


def _adj(edge: str, kind: str, requested: int, resolved: int, *, detail: str | None = None) -> dict[str, Any]:
    return {"edge": edge, "kind": kind, "requested_ms": requested, "resolved_ms": resolved,
            "detail": detail}


def _window_has_speech(transcript: dict[str, Any] | None, start_ms: int, end_ms: int) -> bool:
    if not transcript:
        return False
    for cue in transcript.get("cues", []) or []:
        try:
            cs, ce = int(cue["start_ms"]), int(cue["end_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        if cs < end_ms and ce > start_ms:  # any overlap
            return True
    return False


def _note_overlaps(segments: list[dict[str, Any]], notes: list[str]) -> None:
    """Emit an informational note when resolved windows overlap or duplicate (allowed)."""
    ordered = sorted(segments, key=lambda s: (s["start_ms"], s["end_ms"]))
    for a, b in zip(ordered, ordered[1:]):
        if b["start_ms"] < a["end_ms"]:
            notes.append(
                f"segments {a['index']} and {b['index']} overlap in source time "
                f"([{a['start_ms']},{a['end_ms']}] vs [{b['start_ms']},{b['end_ms']}])"
            )


def _segment_dict(
    *, index: int, id_: str, label: str | None,
    requested_start: int, requested_end: int, start: int, end: int,
    exact: bool, adjustments: list[dict[str, Any]], has_speech: bool | None,
) -> dict[str, Any]:
    return {
        "index": index,
        "id": id_,
        "label": label,
        "requested_start_ms": requested_start,
        "requested_end_ms": requested_end,
        "start_ms": start,
        "end_ms": end,
        "snapped": start != requested_start or end != requested_end,
        "exact": exact,
        "adjustments": adjustments,
        "has_speech": has_speech,
        "clip_wav": None,
        "clip_video": None,
    }


def _canonical_for_hash(segments: list[dict[str, Any]], join_clips: bool) -> Any:
    """The stable projection hashed into selection_hash. Only fields that change the MEANING
    of the selection (resolved bounds, order, join intent, exactness) — never volatile fields
    like timestamps, adjustments prose, or clip paths, so re-resolving identical input is
    hash-stable."""
    return {
        "join_clips": bool(join_clips),
        "segments": [
            {"index": s["index"], "start_ms": s["start_ms"], "end_ms": s["end_ms"],
             "exact": s["exact"]}
            for s in segments
        ],
    }


def selection_hash(segments: list[dict[str, Any]], join_clips: bool) -> str:
    return hash_json(_canonical_for_hash(segments, join_clips))


def _document(
    project_id: str, whole_video: bool, join_clips: bool, snap_edges: bool, snap_radius: int,
    source_duration_ms: int, segments: list[dict[str, Any]], notes: list[str], actor: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "whole_video": whole_video,
        "join_clips": bool(join_clips),
        "snap_edges": bool(snap_edges),
        "snap_search_window_ms": int(snap_radius),
        "source_duration_ms": int(source_duration_ms),
        "segments": segments,
        "selection_hash": selection_hash(segments, join_clips),
        "notes": notes,
        "resolved_at": utc_now(),
        "resolved_by": actor,
    }


# --- clip-local transcript derivation ----------------------------------------

def rebase_transcript_for_segment(
    full: dict[str, Any], segment: dict[str, Any], *, project_id: str,
) -> dict[str, Any]:
    """Derive a clip-local transcript for one segment from the full-source transcript.

    Cues overlapping [start_ms, end_ms) are kept, clipped to the window, and shifted so the
    window start becomes clip-local zero. Word timings (optional, per B3) are shifted and
    trimmed the same way. Cue ids are renumbered from 0. The result is a normal transcript
    doc translate/dub can consume exactly as they consume the whole-video one.
    """
    start, end = int(segment["start_ms"]), int(segment["end_ms"])
    offset = start
    out_cues: list[dict[str, Any]] = []
    for cue in full.get("cues", []) or []:
        cs, ce = int(cue["start_ms"]), int(cue["end_ms"])
        if ce <= start or cs >= end:
            continue  # no overlap
        new_start = max(cs, start) - offset
        new_end = min(ce, end) - offset
        if new_end <= new_start:
            continue
        new_cue = dict(cue)
        new_cue["id"] = len(out_cues)
        new_cue["start_ms"] = new_start
        new_cue["end_ms"] = new_end
        if cue.get("words"):
            new_words = []
            for w in cue["words"]:
                try:
                    ws, we = int(w["start_ms"]), int(w["end_ms"])
                except (KeyError, TypeError, ValueError):
                    continue
                if we <= start or ws >= end:
                    continue
                nw = dict(w)
                nw["start_ms"] = max(ws, start) - offset
                nw["end_ms"] = min(we, end) - offset
                new_words.append(nw)
            new_cue["words"] = new_words
        out_cues.append(new_cue)

    doc = dict(full)
    doc["cues"] = out_cues
    doc["duration_seconds"] = round((end - start) / 1000, 3)
    doc["created_at"] = utc_now()
    # Mark provenance so a reader can tell this is a derived clip transcript, not source ASR.
    doc["segment"] = {"index": segment["index"], "id": segment["id"],
                      "source_start_ms": start, "source_end_ms": end}
    return doc


# --- orchestration (I/O + state) ---------------------------------------------

def _load_selection_config(paths: ProjectPaths) -> tuple[dict[str, Any] | None, bool]:
    """Read (selection, join_clips) from project.yaml with defensive defaults."""
    cfg = load_yaml(paths.config)
    selection = cfg.get("selection")
    if selection is not None and not isinstance(selection, dict):
        raise ConfigurationError("project selection must be an object or null")
    join_clips = _coerce_bool(cfg.get("join_clips"), True)
    return selection, join_clips


def run_resolve(
    root: Path,
    project_id: str,
    *,
    actor: str = "agent",
    advance: bool = False,
) -> dict[str, Any]:
    """Resolve the project's selection into segments/segments.json (SEGMENT_RESOLUTION).

    Reads selection/join_clips from project.yaml and the QA'd full-source transcript (for
    snap boundaries), writes the validated segments manifest, registers it as a
    content-addressed artifact carrying the selection_hash in provenance, and emits
    SEGMENTS_RESOLVED. Optionally advances SEGMENT_RESOLUTION -> TRANSLATION.
    """
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    if state["current_state"] not in _STATES_ALLOWING_RESOLVE:
        raise ConfigurationError(
            f"segments resolve expects state SEGMENT_RESOLUTION, project is at {state['current_state']}"
        )
    selection, join_clips = _load_selection_config(paths)
    source_duration_ms = _source_duration_ms(paths)

    transcript = None
    try:
        transcript = load_transcript(root, project_id)
    except ConfigurationError:
        # No full transcript yet: only acceptable for exact/whole-video (snapping needs cues).
        needs_boundaries = selection is not None and _coerce_bool(
            selection.get("snap_edges"), True
        ) and any(not _coerce_bool(w.get("exact"), False) for w in selection.get("windows", []) or [])
        if needs_boundaries:
            raise ConfigurationError(
                "snapping requires the full-source transcript; run `transcript run` first "
                "or set exact cuts (snap_edges:false / per-window exact:true)"
            ) from None

    doc = resolve_segments(
        project_id=project_id, selection=selection, join_clips=join_clips,
        source_duration_ms=source_duration_ms, transcript=transcript, actor=actor,
    )
    require_valid(root, doc, "segments.schema.json")

    with project_lock(paths.lock):
        paths.segments_manifest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(paths.segments_manifest, doc)
    artifact = artifacts_mod.register_artifact(
        root, project_id, paths.segments_manifest, "segments", _STATE, actor,
        provenance={"selection_hash": doc["selection_hash"]},
    )
    append_event(paths.events, project_id, "SEGMENTS_RESOLVED", actor, {
        "whole_video": doc["whole_video"], "join_clips": doc["join_clips"],
        "segments": len(doc["segments"]), "selection_hash": doc["selection_hash"],
        "notes": doc["notes"],
    })

    advanced_to = _STATE
    if advance:
        transition(root, project_id, "TRANSLATION", actor, reason="segments resolved")
        advanced_to = "TRANSLATION"
    return {"project_id": project_id, "segments": _rel(paths.segments_manifest, paths),
            "artifact": artifact, "whole_video": doc["whole_video"],
            "count": len(doc["segments"]), "selection_hash": doc["selection_hash"],
            "notes": doc["notes"], "advanced_to": advanced_to}


def load_segments(root: Path, project_id: str) -> dict[str, Any]:
    paths = ProjectPaths(root, project_id).require()
    if not paths.segments_manifest.is_file():
        raise ConfigurationError(
            "no segments.json; run `segments resolve` (SEGMENT_RESOLUTION) first"
        )
    return load_json(paths.segments_manifest)


def selection_hash_for(root: Path, project_id: str) -> str | None:
    """The active selection_hash if segments are resolved, else None. Downstream stages thread
    this into artifact provenance so a changed selection invalidates their approvals."""
    paths = ProjectPaths(root, project_id).require()
    if not paths.segments_manifest.is_file():
        return None
    try:
        return load_json(paths.segments_manifest).get("selection_hash")
    except (ValueError, OSError):
        return None


def run_cut(
    root: Path,
    project_id: str,
    *,
    actor: str = "agent",
    reencode: bool = True,
) -> dict[str, Any]:
    """Extract each resolved segment's clip WAV + clip video and derive its clip-local
    transcript. Skipped (no-op media) on the whole-video fast path — that path keeps the full
    source and its whole-video transcript untouched, so packaging can `-c:v copy`.

    Writes segments/clips/<idx>/{audio.wav,video.mp4} and, when a full transcript exists,
    transcript/segments/<idx>/source.<lang>.json. Updates segments.json with the clip paths.
    Idempotent: re-running overwrites the clips.
    """
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    if state["current_state"] not in _STATES_ALLOWING_CUT:
        raise ConfigurationError(
            f"segments cut expects state at/through SEGMENT_RESOLUTION, is at {state['current_state']}"
        )
    doc = load_segments(root, project_id)

    if doc["whole_video"]:
        return {"project_id": project_id, "whole_video": True, "cut": [],
                "note": "whole-video selection: no cutting (full source is the single segment)"}

    src_video = _source_video(paths)
    transcript = None
    try:
        transcript = load_transcript(root, project_id)
    except ConfigurationError:
        transcript = None
    language = transcript.get("language") if transcript else None

    cut: list[dict[str, Any]] = []
    for segment in doc["segments"]:
        idx = segment["index"]
        start_ms, end_ms = int(segment["start_ms"]), int(segment["end_ms"])
        clip_dir = paths.segment_clip_dir(idx)
        clip_dir.mkdir(parents=True, exist_ok=True)
        start_s = start_ms / 1000
        dur_s = (end_ms - start_ms) / 1000

        # Clip WAV (dub-rate) — reuse slice_wav; dub cadence keys off this clip-local audio.
        wav_dest = clip_dir / "audio.wav"
        media_mod.slice_wav(
            paths.source_dir / "audio.wav", wav_dest,
            start_seconds=start_s, duration_seconds=dur_s,
            sample_rate=media_mod.DUB_SAMPLE_RATE, channels=media_mod.DUB_CHANNELS,
        )
        segment["clip_wav"] = _rel(wav_dest, paths)

        # Clip video (frame-accurate, re-encoded) when a source video exists.
        if src_video is not None:
            vid_dest = clip_dir / "video.mp4"
            media_mod.slice_video(
                src_video, vid_dest, start_seconds=start_s, duration_seconds=dur_s,
                reencode=reencode,
            )
            segment["clip_video"] = _rel(vid_dest, paths)

        # Clip-local transcript for translate/dub.
        clip_transcript_rel = None
        if transcript is not None and language:
            clip_doc = rebase_transcript_for_segment(transcript, segment, project_id=project_id)
            require_valid(root, clip_doc, "transcript.schema.json")
            t_dir = paths.transcript_dir / "segments" / str(idx)
            t_dir.mkdir(parents=True, exist_ok=True)
            t_dest = t_dir / f"source.{language}.json"
            atomic_write_json(t_dest, clip_doc)
            clip_transcript_rel = _rel(t_dest, paths)

        cut.append({"index": idx, "clip_wav": segment["clip_wav"],
                    "clip_video": segment.get("clip_video"),
                    "clip_transcript": clip_transcript_rel})

    # Persist the clip paths back into segments.json (media pointers only; selection_hash and
    # bounds are unchanged, so this does NOT re-key provenance).
    with project_lock(paths.lock):
        atomic_write_json(paths.segments_manifest, doc)
    append_event(paths.events, project_id, "SEGMENTS_CUT", actor, {
        "segments": len(cut), "has_video": src_video is not None,
        "has_transcript": transcript is not None,
    })
    return {"project_id": project_id, "whole_video": False, "cut": cut}


def _source_video(paths: ProjectPaths) -> Path | None:
    for child in sorted(paths.source_dir.glob("*")):
        if child.suffix.lower() in {".mp4", ".mkv", ".webm"}:
            return child
    return None


def _rel(path: Path, paths: ProjectPaths) -> str:
    return path.resolve().relative_to(paths.directory.resolve()).as_posix()
