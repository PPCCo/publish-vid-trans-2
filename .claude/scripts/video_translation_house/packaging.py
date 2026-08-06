"""Per-language video mux + deliverable packaging (AUDIO_QA_GATE -> READY_FOR_REVIEW).

Terminal phase of the pipeline. Mirrors the dubbing.py / translate.py contract:

  * The CLI never imports an ML library and never calls an LLM. Mux is pure ffmpeg
    subprocess via media.mux_video (picture copied bit-for-bit, dub encoded to AAC).
  * Per dub-enabled language, mux the source video + audio/<lang>/dub.wav (+ optional soft
    subs) into video/<lang>/dubbed.mp4. Caption-only tracks (dub_enabled=false) produce no
    video; their package carries captions only.
  * The `final` gate report is AGGREGATE (one file per project, PASS iff every dub track's
    dubbed video is stream-complete and A/V-aligned). The human `final_qa` approval is a
    single PROJECT-LEVEL approval (final_qa is NOT in state.per_language_gates) bound to the
    dubbed-video / package hashes.
  * PACKAGE assembles packages/<lang>/ (dubbed video / captions / README with a rights banner
    + checksums) and packages/package-manifest.json. NOTHING is published: the framework stops
    at READY_FOR_REVIEW with upload-ready packages. PACKAGE->READY_FOR_REVIEW stays blocked by
    the rights report until a human sets a distributable rights_status (enforced in state.py).

Gate report filename is keyed by REPORT TYPE ("final"), matching workflow_states.gate_reports.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from . import artifacts as artifacts_mod
from . import media as media_mod
from . import segments as segments_mod
from .captions import render as render_captions
from .captions import script_class, slice_caption_doc
from .errors import ConfigurationError, MuxError, PackageError
from .events import append_event
from .paths import ProjectPaths
from .rights import check_rights
from .translate import (
    SCHEMA_VERSION,
    _active_track_langs,
    _aggregate_decision,
    _maybe_advance_top,
    _rel,
    _set_track,
    _write_gate_report,
    load_captions,
)
from .util import (
    atomic_write_json,
    atomic_write_text,
    load_json,
    load_tools_config,
    project_lock,
    sha256_path,
    utc_now,
)
from .validation import require_valid

# script_class() -> tools.default.json "fonts" key, so burned-in captions render with a
# family that has full glyph coverage for that script instead of missing-glyph boxes.
_FONT_KEY_BY_SCRIPT = {"cjk": "cjk", "rtl": "arabic", "cyrillic": "cyrillic"}


def _font_for_language(root: Path, language: str) -> str | None:
    cls = script_class(language)
    key = _FONT_KEY_BY_SCRIPT.get(cls)
    if not key:
        return None
    return load_tools_config(root).get("fonts", {}).get(key)

# Track stages during Phase 5.
TRACK_STAGE_MUXED = "FINAL_QA_GATE"      # dubbed.mp4 rendered; A/V measured
TRACK_STAGE_PACKAGED = "PACKAGE"          # deliverables assembled

_STATES_ALLOWING_MUX = {"AUDIO_QA_GATE", "VIDEO_MUX", "FINAL_QA_GATE"}
_STATES_ALLOWING_PACKAGE = {"FINAL_QA_GATE", "PACKAGE"}

# A dubbed video's picture and dub audio should end within this of each other. `-shortest`
# already trims the tail, so a breach means the dub is materially longer/shorter than the
# picture (a real defect), not rounding. Surfaced in the final report; not a company bar.
_AV_ALIGN_TOLERANCE_MS = 1000


# --- helpers -----------------------------------------------------------------

def dubbed_video_relpath(language: str) -> str:
    """Project-relative path for a language's muxed video: video/<lang>/dubbed.mp4."""
    return f"video/{language}/dubbed.mp4"


def package_dir_relpath(language: str) -> str:
    return f"packages/{language}"


def package_manifest_relpath() -> str:
    return "packages/package-manifest.json"


def _track(state: dict[str, Any], language: str) -> dict[str, Any]:
    return state.get("language_tracks", {}).get(language, {})


def _dub_enabled(state: dict[str, Any], language: str) -> bool:
    return bool(_track(state, language).get("dub_enabled", False))


def _active_artifact(root: Path, project_id: str, artifact_type: str,
                     language: str | None) -> dict[str, Any] | None:
    """Return the currently-active (non-superseded) artifact of a type[@lang], or None.

    The active hash is state.active_artifacts[type@lang]; we resolve it to the manifest
    entry so callers can assert freshness and read its path."""
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    key = f"{artifact_type}@{language}" if language else artifact_type
    digest = state.get("active_artifacts", {}).get(key)
    if not digest:
        return None
    return artifacts_mod.get_artifact_by_hash(root, project_id, digest)


def _source_video(paths: ProjectPaths) -> Path | None:
    for child in sorted(paths.source_dir.glob("*")):
        if child.suffix.lower() in {".mp4", ".mkv", ".webm"}:
            return child
    return None


def _source_provenance(paths: ProjectPaths) -> dict[str, Any]:
    meta_path = paths.source_dir / "metadata.json"
    if not meta_path.is_file():
        return {}
    meta = load_json(meta_path)
    return {k: meta.get(k) for k in ("title", "channel", "url", "duration_seconds") if k in meta}


# --- 1. mux ------------------------------------------------------------------

_MUX_MODES = {"soft-subs", "burned-in", "no-subs"}


def run_mux(
    root: Path,
    project_id: str,
    language: str,
    *,
    mode: str = "soft-subs",
    actor: str = "agent",
    advance: bool = False,
) -> dict[str, Any]:
    """Mux the source video + a language's dub track into video/<lang>/dubbed.mp4.

    Guarded to state at/through AUDIO_QA_GATE + dub_enabled track. Uses the ACTIVE dub-wav
    artifact (so a superseded dub can't be muxed). `mode` selects how the language's VTT
    captions are attached: "soft-subs" (default, a toggleable mov_text track, picture copied
    bit-for-bit), "burned-in" (rendered into the picture via ffmpeg's subtitles filter, video
    re-encoded — for platforms that don't reliably render soft subs), or "no-subs" (dub audio
    only). Registers dubbed-video@<lang>; advances the track to FINAL_QA_GATE.
    """
    if mode not in _MUX_MODES:
        raise MuxError(f"mode must be one of {sorted(_MUX_MODES)}, got {mode!r}")
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    if state["current_state"] not in _STATES_ALLOWING_MUX:
        raise ConfigurationError(
            f"mux expects the project at/through AUDIO_QA_GATE, is at {state['current_state']}"
        )
    if not _dub_enabled(state, language):
        raise MuxError(f"track {language!r} is not dub_enabled (caption-only); nothing to mux")

    src_video = _source_video(paths)
    if src_video is None:
        # The source media may have been deleted after translation/dub (it's expensive to keep
        # and only mux needs the picture). Try the sanctioned re-fetch — VIDTRANS_FETCH_ENABLED
        # gated — before giving up, so a deleted source is recovered transparently at mux time.
        from .ingest import ensure_source_present

        ensure_source_present(root, project_id, actor=actor)
        src_video = _source_video(paths)
    if src_video is None:
        raise MuxError("no source video in source/ to mux against")

    dub_artifact = _active_artifact(root, project_id, "dub-wav", language)
    if not dub_artifact:
        raise MuxError(
            f"no active dub-wav for {language!r}; run `dub run`/`dub import` (and pass audio_qa) first"
        )
    dub_path = paths.directory / dub_artifact["path"]
    if not dub_path.is_file():
        raise MuxError(f"registered dub is missing on disk: {dub_artifact['path']}")

    vtt = paths.captions_dir / f"captions.{language}.vtt"
    subs_path = vtt if (mode != "no-subs" and vtt.is_file()) else None
    if mode == "burned-in" and subs_path is None:
        raise MuxError(f"mode=burned-in requires captions.{language}.vtt; run `captions build` first")

    dst = paths.directory / dubbed_video_relpath(language)
    with project_lock(paths.lock):
        dst.parent.mkdir(parents=True, exist_ok=True)
        if mode == "burned-in":
            font = _font_for_language(root, language)
            media_mod.mux_video_burned_in(src_video, dub_path, subs_path, dst, font_name=font)
        else:
            media_mod.mux_video(src_video, dub_path, dst, subs=subs_path)

    artifact = artifacts_mod.register_artifact(
        root, project_id, dst, "dubbed-video", "VIDEO_MUX", actor, language=language,
        source_artifact_ids=[dub_artifact["artifact_id"]],
    )
    _set_track(root, project_id, language, stage=TRACK_STAGE_MUXED,
               status="in_progress", actor=actor, notes="dubbed video muxed")
    append_event(paths.events, project_id, "VIDEO_MUXED", actor, {
        "language": language, "dubbed_sha256": artifact["sha256"], "mode": mode,
    })
    dub_quorum = [lang for lang in _active_track_langs(state) if _dub_enabled(state, lang)]
    advanced = _maybe_advance_top(root, project_id, actor, advance, "AUDIO_QA_GATE",
                                  "VIDEO_MUX", TRACK_STAGE_MUXED, dub_quorum)
    return {"dubbed_video": _rel(dst, paths), "artifact": artifact, "language": language,
            "mode": mode, "advanced_to": advanced}


# --- 2. final QA (aggregate gate report) -------------------------------------

def _finding(severity: str, category: str, summary: str, *,
             language: str | None = None) -> dict[str, Any]:
    finding: dict[str, Any] = {"severity": severity, "category": category, "summary": summary}
    if language is not None:
        finding["language"] = language
    return finding


def analyze_mux(block: dict[str, Any], tol_ms: int = _AV_ALIGN_TOLERANCE_MS) -> dict[str, Any]:
    """Deterministic per-language decision from a probed dubbed-video block.

    FAIL : no video stream, no audio stream, or |video_dur - audio_dur| beyond tolerance.
    COND : (reserved) minor issues — currently none escalate here.
    PASS : streams present and A/V durations aligned within tolerance.
    """
    lang = block["language"]
    findings: list[dict[str, Any]] = []
    if not block.get("has_video"):
        findings.append(_finding("blocker", "mux-no-video",
                                  f"[{lang}] dubbed video has no video stream.", language=lang))
    if not block.get("has_audio"):
        findings.append(_finding("blocker", "mux-no-audio",
                                  f"[{lang}] dubbed video has no audio stream.", language=lang))
    drift = block.get("av_drift_ms")
    if drift is not None and abs(drift) > tol_ms:
        findings.append(_finding(
            "blocker", "mux-av-misaligned",
            f"[{lang}] audio ends {drift}ms from the picture (tolerance {tol_ms}ms) — "
            f"the dub is materially longer/shorter than the video.",
            language=lang,
        ))
    decision = "FAIL" if any(f["severity"] == "blocker" for f in findings) else "PASS"
    return {"decision": decision, "findings": findings}


def _probe_dubbed_block(paths: ProjectPaths, language: str) -> dict[str, Any]:
    """Probe a language's dubbed.mp4 into the block analyze_mux consumes."""
    path = paths.directory / dubbed_video_relpath(language)
    if not path.is_file():
        return {"language": language, "present": False, "has_video": False,
                "has_audio": False, "av_drift_ms": None}
    summary = media_mod.probe_summary(path)
    video = summary.get("video") or {}
    audio = summary.get("audio") or {}
    dur_ms = media_mod.audio_duration_ms(path)  # container duration
    dub_ms = None
    dub = paths.audio_dir / language / "dub.wav"
    if dub.is_file():
        dub_ms = media_mod.audio_duration_ms(dub)
    drift = (dur_ms - dub_ms) if (dub_ms is not None) else None
    return {
        "language": language,
        "present": True,
        "has_video": bool(video.get("codec")),
        "has_audio": bool(audio.get("codec")),
        "container_ms": dur_ms,
        "dub_ms": dub_ms,
        "av_drift_ms": drift,
    }


def run_final_qa(root: Path, project_id: str, *, actor: str = "agent") -> dict[str, Any]:
    """Aggregate `final` gate report across every active dub-enabled track.

    Single file keyed by report type; PASS iff every dubbed video is stream-complete and
    A/V-aligned. The human `final_qa` approval remains a single project-level approval.
    """
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)

    dub_langs = [lang for lang in _active_track_langs(state) if _dub_enabled(state, lang)]
    findings: list[dict[str, Any]] = []
    per_lang: dict[str, Any] = {}
    decisions: list[str] = []
    for lang in dub_langs:
        block = _probe_dubbed_block(paths, lang)
        if not block["present"]:
            decisions.append("FAIL")
            findings.append(_finding("blocker", "mux-missing",
                                      f"[{lang}] dub-enabled track has no dubbed video — run `package mux`.",
                                      language=lang))
            per_lang[lang] = {"present": False}
            continue
        analysis = analyze_mux(block)
        decisions.append(analysis["decision"])
        findings += analysis["findings"]
        per_lang[lang] = {k: block[k] for k in ("has_video", "has_audio", "container_ms",
                                                "dub_ms", "av_drift_ms")}

    decision = _aggregate_decision(decisions)
    gate_path = _write_gate_report(
        root, project_id, "final", decision, findings,
        {"languages": per_lang, "av_align_tolerance_ms": _AV_ALIGN_TOLERANCE_MS}, actor,
    )
    append_event(paths.events, project_id, "FINAL_QA_REPORTED", actor, {
        "decision": decision, "languages": dub_langs,
    })
    return {"decision": decision, "report": _rel(gate_path, paths),
            "languages": per_lang, "findings": findings}


# --- 3. package --------------------------------------------------------------

def _readme(project_id: str, language: str, dub_enabled: bool, prov: dict[str, Any],
            distributable: bool, rights_status: str, deliverables: list[dict[str, Any]]) -> str:
    banner = (
        "✅ Rights cleared for distribution.\n"
        if distributable else
        "⛔ NOT CLEARED FOR DISTRIBUTION.\nA human must set a distributable rights_status "
        "(rights set) before any use. These files are for review only.\n"
    )
    lines = [
        f"# {project_id} — {language} package",
        "",
        banner,
        f"- rights_status: **{rights_status}**",
        f"- language: {language}  (audio dub: {'yes' if dub_enabled else 'captions only'})",
    ]
    if prov.get("title"):
        lines.append(f"- source title: {prov['title']}")
    if prov.get("channel"):
        lines.append(f"- source channel: {prov['channel']}")
    if prov.get("url"):
        lines.append(f"- source url: {prov['url']}")
    lines += ["", "## Deliverables (sha256)", ""]
    for d in deliverables:
        lines.append(f"- `{Path(d['path']).name}` — {d['type']} — {d['sha256']}")
    lines += ["", "_Produced by publish-vid-trans; the framework does not publish. "
              "Distribution is a separate, human-authorized step._", ""]
    return "\n".join(lines)


def _copy_registered(root: Path, project_id: str, src: Path, dst: Path,
                     artifact_type: str, actor: str, language: str) -> dict[str, Any]:
    """Copy a produced file into the package dir and record it as a deliverable entry."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    digest, size = sha256_path(dst)
    paths = ProjectPaths(root, project_id).require()
    return {"type": artifact_type, "path": _rel(dst, paths), "sha256": digest, "size_bytes": size}


# --- selection assembly (cut/join at package time) ---------------------------
# Assemble-time strategy: translate/captions/dub run on the WHOLE source timeline and produce
# the full-length dubbed.mp4 (dub-enabled) or leave the source video (caption-only) plus the
# canonical captions.<lang>.json. Here we cut those finished artifacts at the resolved window
# bounds and, when join_clips, concat the pieces. Whole-video selections skip all of this and
# take the unchanged fast path. Captions are sliced from the canonical doc and re-offset onto
# the (clip-local or joined) timeline, then re-rendered — never re-translated.


def _write_captions_for_timeline(
    caption_doc: dict[str, Any], windows: list[tuple[int, int, int]], dest_dir: Path,
    language: str,
) -> list[tuple[str, Path]]:
    """Slice+re-offset the canonical caption doc across ``windows`` (each (start,end,offset))
    into one continuous doc, render SRT+VTT into ``dest_dir``. Returns [(fmt, path), ...].

    A single window with offset 0 yields clip-local captions; multiple windows with cumulative
    offsets yield captions continuous across a joined video's seams."""
    merged_cues: list[dict[str, Any]] = []
    for start_ms, end_ms, offset_ms in windows:
        piece = slice_caption_doc(caption_doc, start_ms=start_ms, end_ms=end_ms, offset_ms=offset_ms)
        for cue in piece["cues"]:
            cue = dict(cue)
            cue["id"] = len(merged_cues)
            merged_cues.append(cue)
    doc = dict(caption_doc)
    doc["cues"] = merged_cues
    out: list[tuple[str, Path]] = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    for fmt in ("srt", "vtt"):
        text = render_captions(doc, fmt)
        path = dest_dir / f"captions.{language}.{fmt}"
        atomic_write_text(path, text if text.endswith("\n") else text + "\n")
        out.append((fmt, path))
    return out


def _clip_source_for_lang(paths: ProjectPaths, state: dict[str, Any], language: str) -> Path:
    """The finished full-timeline video to cut for a language: the dubbed.mp4 for dub-enabled
    tracks (dub already muxed over the full picture), else the source video (caption-only keeps
    the original audio)."""
    if _dub_enabled(state, language):
        vid = paths.directory / dubbed_video_relpath(language)
        if not vid.is_file():
            raise PackageError(f"[{language}] missing dubbed video; run `package mux` first")
        return vid
    src = _source_video(paths)
    if src is None:
        raise PackageError(f"[{language}] caption-only selection needs a source video to cut")
    return src


def _assemble_selection_package(
    root: Path, project_id: str, paths: ProjectPaths, state: dict[str, Any],
    language: str, seg_doc: dict[str, Any], pkg_dir: Path, actor: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build a selection (multi-window) package for one language via cut/join at PACKAGE.

    Returns (deliverables, selection_meta). Cuts each resolved window out of the finished
    full-timeline video (frame-accurate re-encode), then either concatenates them into one
    joined.mp4 (join_clips) or emits each as a per-clip deliverable. Captions are sliced and
    re-offset to match the assembled timeline. tmp clip files live under packages/<lang>/_work.
    """
    caption_doc = load_captions(root, project_id, language)
    source_video = _clip_source_for_lang(paths, state, language)
    join_clips = bool(seg_doc["join_clips"])
    seg_list = seg_doc["segments"]
    work = pkg_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)

    # Extract one frame-accurate clip per window from the finished video.
    clip_videos: list[Path] = []
    for seg in seg_list:
        start_ms, end_ms = int(seg["start_ms"]), int(seg["end_ms"])
        clip = work / f"clip-{seg['index']}.mp4"
        media_mod.slice_video(
            source_video, clip,
            start_seconds=start_ms / 1000, duration_seconds=(end_ms - start_ms) / 1000,
            reencode=True,
        )
        clip_videos.append(clip)

    deliverables: list[dict[str, Any]] = []
    selection_meta = {
        "join_clips": join_clips,
        "selection_hash": seg_doc.get("selection_hash"),
        "clips": [
            {"index": s["index"], "id": s["id"], "label": s.get("label"),
             "source_start_ms": int(s["start_ms"]), "source_end_ms": int(s["end_ms"])}
            for s in seg_list
        ],
    }

    if join_clips:
        joined = pkg_dir / "joined.mp4"
        media_mod.concat_videos(clip_videos, joined)
        deliverables.append(_deliverable(paths, joined, "joined-video"))
        # Captions continuous across the joined timeline: cumulative offsets = running duration.
        windows: list[tuple[int, int, int]] = []
        offset = 0
        for seg in seg_list:
            start_ms, end_ms = int(seg["start_ms"]), int(seg["end_ms"])
            windows.append((start_ms, end_ms, offset))
            offset += end_ms - start_ms
        for fmt, cap_path in _write_captions_for_timeline(caption_doc, windows, pkg_dir, language):
            deliverables.append(_deliverable(paths, cap_path, f"captions-{fmt}"))
    else:
        for seg, clip in zip(seg_list, clip_videos):
            idx = seg["index"]
            clip_dir = pkg_dir / f"clip-{idx}"
            clip_dir.mkdir(parents=True, exist_ok=True)
            dst = clip_dir / "clip.mp4"
            dst.write_bytes(clip.read_bytes())
            deliverables.append(_deliverable(paths, dst, "clip-video", clip_index=idx))
            start_ms, end_ms = int(seg["start_ms"]), int(seg["end_ms"])
            for fmt, cap_path in _write_captions_for_timeline(
                caption_doc, [(start_ms, end_ms, 0)], clip_dir, language
            ):
                deliverables.append(_deliverable(paths, cap_path, f"captions-{fmt}", clip_index=idx))

    # Drop the scratch clips; deliverables have been copied/concatenated out of _work.
    for clip in clip_videos:
        clip.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        work.rmdir()
    return deliverables, selection_meta


def _deliverable(paths: ProjectPaths, path: Path, kind: str, *, clip_index: int | None = None) -> dict[str, Any]:
    digest, size = sha256_path(path)
    entry: dict[str, Any] = {"type": kind, "path": _rel(path, paths),
                             "sha256": digest, "size_bytes": size}
    if clip_index is not None:
        entry["clip_index"] = clip_index
    return entry


def run_package(
    root: Path,
    project_id: str,
    *,
    actor: str = "agent",
    advance: bool = False,
) -> dict[str, Any]:
    """Assemble packages/<lang>/ for every active track and write packages/package-manifest.json.

    Dub-enabled tracks get the dubbed video + captions; caption-only tracks get captions only.
    Each package carries a README with a rights banner and checksums. Registers package@<lang>
    and package-manifest. Advances each track to PACKAGE; when `advance` and all active tracks
    are packaged, advances the top state FINAL_QA_GATE -> PACKAGE (subject to the final_qa gate).

    Nothing is published: writes go under packages/, never outputs/.
    """
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    if state["current_state"] not in _STATES_ALLOWING_PACKAGE:
        raise ConfigurationError(
            f"package expects the project at/through FINAL_QA_GATE, is at {state['current_state']}"
        )

    rights = check_rights(root, project_id)
    distributable = bool(rights.get("distributable"))
    rights_status = str(rights.get("rights_status", "unreviewed"))
    prov = _source_provenance(paths)

    langs = _active_track_langs(state)
    if not langs:
        raise PackageError("no active language tracks to package")

    # Selection (multi-window) resolution, if any. whole_video / unresolved => the whole-video
    # deliverable layout (dubbed.mp4 or captions-only). A non-whole-video selection triggers the
    # assemble-time cut/join path per language.
    seg_doc: dict[str, Any] | None = None
    if paths.segments_manifest.is_file():
        loaded = load_json(paths.segments_manifest)
        if not loaded.get("whole_video", True):
            seg_doc = loaded
    selection_hash = seg_doc.get("selection_hash") if seg_doc else None

    packages: list[dict[str, Any]] = []
    for lang in langs:
        dub_enabled = _dub_enabled(state, lang)
        pkg_dir = paths.directory / package_dir_relpath(lang)
        deliverables: list[dict[str, Any]] = []
        selection_meta: dict[str, Any] | None = None

        if seg_doc is not None:
            # Assemble-time cut/join: build clip/joined deliverables + re-offset captions.
            deliverables, selection_meta = _assemble_selection_package(
                root, project_id, paths, state, lang, seg_doc, pkg_dir, actor)
        else:
            # Whole-video layout (unchanged): full captions + (dub-enabled) full dubbed video.
            for fmt in ("srt", "vtt"):
                src = paths.captions_dir / f"captions.{lang}.{fmt}"
                if not src.is_file():
                    raise PackageError(
                        f"[{lang}] missing captions.{lang}.{fmt}; run `captions build` first"
                    )
                deliverables.append(_copy_registered(
                    root, project_id, src, pkg_dir / src.name, f"captions-{fmt}", actor, lang))
            if dub_enabled:
                vid = paths.directory / dubbed_video_relpath(lang)
                if not vid.is_file():
                    raise PackageError(f"[{lang}] missing dubbed video; run `package mux` first")
                deliverables.append(_copy_registered(
                    root, project_id, vid, pkg_dir / "dubbed.mp4", "dubbed-video", actor, lang))

        readme_text = _readme(project_id, lang, dub_enabled, prov, distributable,
                              rights_status, deliverables)
        readme_path = pkg_dir / "README.md"
        with project_lock(paths.lock):
            readme_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(readme_path, readme_text + "\n")
        r_digest, r_size = sha256_path(readme_path)
        deliverables.append({"type": "readme", "path": _rel(readme_path, paths),
                             "sha256": r_digest, "size_bytes": r_size})

        artifacts_mod.register_artifact(
            root, project_id, pkg_dir, "package", "PACKAGE", actor, language=lang,
            provenance={"selection_hash": selection_hash} if selection_hash else None,
        )
        packages.append({
            "language": lang,
            "dub_enabled": dub_enabled,
            "directory": package_dir_relpath(lang),
            "selection": selection_meta,
            "deliverables": deliverables,
        })
        _set_track(root, project_id, lang, stage=TRACK_STAGE_PACKAGED,
                   status="in_progress", actor=actor, notes="package assembled")

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "created_at": utc_now(),
        "created_by": actor,
        "source": prov,
        "rights_status": rights_status,
        "distributable": distributable,
        "packages": packages,
    }
    require_valid(root, manifest, "package-manifest.schema.json")
    manifest_path = paths.directory / package_manifest_relpath()
    with project_lock(paths.lock):
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(manifest_path, manifest)
    manifest_artifact = artifacts_mod.register_artifact(
        root, project_id, manifest_path, "package-manifest", "PACKAGE", actor,
    )
    append_event(paths.events, project_id, "PACKAGED", actor, {
        "languages": langs, "distributable": distributable, "rights_status": rights_status,
        "manifest_sha256": manifest_artifact["sha256"],
    })
    advanced = _maybe_advance_top(root, project_id, actor, advance, "FINAL_QA_GATE",
                                  "PACKAGE", TRACK_STAGE_PACKAGED)
    return {"manifest": _rel(manifest_path, paths), "artifact": manifest_artifact,
            "packages": packages, "distributable": distributable,
            "rights_status": rights_status, "advanced_to": advanced}


def load_package_manifest(root: Path, project_id: str) -> dict[str, Any]:
    paths = ProjectPaths(root, project_id).require()
    path = paths.directory / package_manifest_relpath()
    if not path.is_file():
        raise ConfigurationError(
            f"no package manifest at {package_manifest_relpath()}; run `package build` first"
        )
    return load_json(path)
