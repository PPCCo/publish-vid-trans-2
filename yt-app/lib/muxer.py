"""Mux a dub track onto its (re-timed) picture — a port of ``packaging.run_mux``.

Thin reimplementation over plain paths + a freeze plan dict, with no state machine, artifact
registration, or event log. Every framework LEAF it calls (``media.*``,
``captions.slice_caption_doc`` / ``captions.render``) is imported lazily.

Three pictures, mirroring the framework (CLAUDE.md rules 14/15):

  * **still-image** language — loop one fixed frame for the dub's whole length, lay the dub over
    it. No freeze plan (a static frame can't desync).
  * **freeze-plan** language — rebuild the picture on the post-adjust timeline via
    ``build_retimed_video`` (source slices interleaved with held frames; trimmed regions dropped),
    and re-time the standalone captions onto that same timeline (the canonical caption doc is
    never edited — rules 6/12).
  * **plain** language — an empty/absent plan copies the source picture bit-for-bit (``-c:v copy``
    inside ``media.mux_video``).

Then the subs are attached per ``mode`` (soft-subs / burned-in / no-subs) and, LAST, a
deliberate uniform ``playback_speed`` (rule 15) re-times the finished file (audio+video by the
same factor) if != 1.0.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_MUX_MODES = {"soft-subs", "burned-in", "no-subs"}


class MuxError(RuntimeError):
    """A mux could not be produced (missing picture source, bad mode, missing subs for burn-in)."""


def freeze_windows(
    freeze_plan: dict[str, Any], source_duration_ms: int
) -> list[tuple[int, int, int]]:
    """Port of ``packaging._freeze_windows`` — plan → caption-retiming windows.

    Each window ``(orig_start, orig_end, offset)`` is a stretch of ORIGINAL timeline placed at
    ``orig_start + (cum_freeze - cum_trim)`` on the post-adjust timeline. Trimmed regions collapse
    (no window covers them). ``slice_caption_doc`` re-bases in-window cues to window-local time
    then adds ``offset``.
    """
    events = sorted(
        [("freeze", int(f["at_ms"]), int(f["freeze_ms"])) for f in freeze_plan.get("freezes", [])]
        + [("trim", int(t["at_ms"]), int(t["trim_ms"])) for t in freeze_plan.get("trims", [])],
        key=lambda e: (e[1], 0 if e[0] == "trim" else 1),
    )
    windows: list[tuple[int, int, int]] = []
    cursor = 0
    cum_freeze = 0
    cum_trim = 0
    for kind, at, amount in events:
        if at > cursor:
            windows.append((cursor, at, cursor + cum_freeze - cum_trim))
            cursor = at
        if kind == "freeze":
            cum_freeze += amount
        else:
            drop_end = min(at + amount, source_duration_ms)
            cum_trim += drop_end - at
            cursor = drop_end
    if source_duration_ms > cursor:
        windows.append((cursor, source_duration_ms, cursor + cum_freeze - cum_trim))
    elif not windows:
        windows.append((0, max(source_duration_ms, 1), cum_freeze - cum_trim))
    return windows


def build_retimed_video(
    src_video: Path | str, freeze_plan: dict[str, Any], work_dir: Path | str
) -> Path:
    """Port of ``packaging._build_retimed_video`` — rebuild the picture on the post-adjust timeline.

    Source slices between events, a held frame at each freeze point, source dropped at each trim
    point; concatenated into one re-encoded MP4 (self-contained GOPs for concat). The muxed dub
    replaces the audio at mux time, so the frozen inserts' audio only needs to satisfy
    ``concat_videos``' per-part v+a contract.
    """
    from video_translation_house import media as media_mod  # lazy

    src_video = Path(src_video).expanduser().resolve()
    work_dir = Path(work_dir).expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    src_summary = media_mod.probe_summary(src_video)
    src_dur_ms = media_mod.audio_duration_ms(src_video)
    a = src_summary.get("audio") or {}
    v = src_summary.get("video") or {}
    sr = int(a.get("sample_rate") or 44100)
    ch = int(a.get("channels") or 2)
    pix_fmt = v.get("pix_fmt") or "yuv420p"

    events = sorted(
        [("freeze", int(f["at_ms"]), int(f["freeze_ms"])) for f in freeze_plan.get("freezes", [])]
        + [("trim", int(t["at_ms"]), int(t["trim_ms"])) for t in freeze_plan.get("trims", [])],
        key=lambda e: (e[1], 0 if e[0] == "trim" else 1),
    )
    parts: list[Path] = []
    cursor_ms = 0
    idx = 0
    for kind, at, amount in events:
        at_ms = min(at, src_dur_ms)
        if at_ms > cursor_ms:
            slice_path = work_dir / f"seg-{idx}.mp4"
            media_mod.slice_video(
                src_video, slice_path,
                start_seconds=cursor_ms / 1000, duration_seconds=(at_ms - cursor_ms) / 1000,
                reencode=True,
            )
            parts.append(slice_path)
            cursor_ms = at_ms
            idx += 1
        if kind == "freeze":
            frozen = work_dir / f"freeze-{idx}.mp4"
            media_mod.freeze_segment(
                src_video, frozen,
                at_seconds=max(0.0, (at_ms - 1) / 1000), duration_ms=int(amount),
                audio_sample_rate=sr, audio_channels=ch, pix_fmt=pix_fmt,
            )
            parts.append(frozen)
            idx += 1
        else:
            cursor_ms = min(cursor_ms + int(amount), src_dur_ms)
    if src_dur_ms > cursor_ms:
        tail = work_dir / f"seg-{idx}.mp4"
        media_mod.slice_video(
            src_video, tail,
            start_seconds=cursor_ms / 1000, duration_seconds=(src_dur_ms - cursor_ms) / 1000,
            reencode=True,
        )
        parts.append(tail)
    retimed = work_dir / "retimed.mp4"
    media_mod.concat_videos(parts, retimed)
    return retimed


def _write_captions_for_timeline(
    caption_doc: dict[str, Any],
    windows: list[tuple[int, int, int]],
    dest_dir: Path,
    language: str,
) -> list[tuple[str, Path]]:
    """Port of ``packaging._write_captions_for_timeline`` — slice+re-offset the canonical doc
    across ``windows`` into one continuous doc; render SRT+VTT into ``dest_dir``."""
    from video_translation_house.captions import render as render_captions  # lazy
    from video_translation_house.captions import slice_caption_doc  # lazy

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
        path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        out.append((fmt, path))
    return out


def _has_freezes_or_trims(freeze_plan: dict[str, Any] | None) -> bool:
    return bool(freeze_plan and (freeze_plan.get("freezes") or freeze_plan.get("trims")))


def run_mux(
    root: Path,
    *,
    language: str,
    dub_wav: Path | str,
    dest_mp4: Path | str,
    source_video: Path | str | None = None,
    still_image: str | None = None,
    freeze_plan: dict[str, Any] | None = None,
    caption_doc: dict[str, Any] | None = None,
    subs_vtt: Path | str | None = None,
    mode: str = "soft-subs",
    playback_speed: float = 1.0,
    work_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Mux ``dub_wav`` onto its picture into ``dest_mp4``.

    Picture source precedence: ``still_image`` (looped for the dub's length) → non-empty
    ``freeze_plan`` over ``source_video`` (retimed) → plain ``source_video`` copy. ``mode``
    selects sub attachment (``soft-subs`` needs ``subs_vtt`` or ``caption_doc``; ``burned-in``
    requires subs; ``no-subs`` ignores them). A ``playback_speed`` != 1.0 uniformly re-times the
    finished file LAST. Returns ``{"dubbed_video", "mode", "playback_speed", "retimed": bool,
    "still_image": bool}``.
    """
    from video_translation_house import media as media_mod  # lazy

    language = str(language).strip().lower()
    if mode not in _MUX_MODES:
        raise MuxError(f"mode must be one of {sorted(_MUX_MODES)}, got {mode!r}")
    dub_path = Path(dub_wav).expanduser().resolve()
    if not dub_path.is_file():
        raise MuxError(f"dub wav missing: {dub_path}")
    dst = Path(dest_mp4).expanduser().resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(work_dir).expanduser().resolve() if work_dir else (dst.parent / "_freeze_work")
    scratch.mkdir(parents=True, exist_ok=True)

    is_still = still_image is not None
    src_video = None
    if not is_still:
        if source_video is None:
            raise MuxError(f"no source video to mux against for {language!r}")
        src_video = Path(source_video).expanduser().resolve()
        if not src_video.is_file():
            raise MuxError(f"source video missing: {src_video}")

    # Determine the subtitle source path. Prefer an explicit VTT; else render one from the doc.
    subs_path: Path | None = None
    if mode != "no-subs":
        if subs_vtt is not None and Path(subs_vtt).is_file():
            subs_path = Path(subs_vtt).expanduser().resolve()
        elif caption_doc is not None:
            from video_translation_house.captions import render as render_captions  # lazy

            subs_path = scratch / f"captions.{language}.vtt"
            text = render_captions(caption_doc, "vtt")
            subs_path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    if mode == "burned-in" and subs_path is None:
        raise MuxError(f"mode=burned-in requires captions for {language!r} (pass subs_vtt or caption_doc)")

    active_plan = freeze_plan if (_has_freezes_or_trims(freeze_plan) and not is_still) else None

    # Build the picture source.
    if is_still:
        dub_dur_ms = media_mod.audio_duration_ms(dub_path)
        still_mp4 = scratch / f"still.{language}.mp4"
        video_source: Path = media_mod.still_image_video(still_image, still_mp4, duration_ms=dub_dur_ms)
    elif active_plan is not None:
        video_source = build_retimed_video(src_video, active_plan, scratch)
        if mode != "no-subs" and caption_doc is not None:
            src_dur_ms = media_mod.audio_duration_ms(src_video)
            windows = freeze_windows(active_plan, src_dur_ms)
            rendered = _write_captions_for_timeline(caption_doc, windows, scratch, language)
            subs_path = next((p for fmt, p in rendered if fmt == "vtt"), subs_path)
    else:
        video_source = src_video

    if mode == "burned-in":
        media_mod.mux_video_burned_in(video_source, dub_path, subs_path, dst)
    else:
        media_mod.mux_video(video_source, dub_path, dst, subs=subs_path)

    # Deliberate uniform playback speed applied LAST (rule 15).
    if abs(float(playback_speed) - 1.0) >= 1e-6:
        sped = scratch / f"sped.{language}.mp4"
        media_mod.respeed_video(dst, sped, factor=float(playback_speed))
        dst.unlink(missing_ok=True)
        sped.replace(dst)

    return {
        "dubbed_video": str(dst),
        "mode": mode,
        "playback_speed": float(playback_speed),
        "retimed": active_plan is not None,
        "still_image": is_still,
    }
