"""Render a dub track for one language — a faithful port of ``dubbing.run_dub``'s inner loop.

This is a THIN REIMPLEMENTATION over plain dicts (a caption doc + an effective config), not a
call into the state-coupled framework: no ``ProjectPaths``, no ``state.json``, no artifact
registration, no event log, and — deliberately, per the plan — **no ``voice_clone_consent``
gate** (yt-app is a local operator tool with no rights record; cloning defaults on for
clone-languages, ``--no-clone`` opts out). Every framework LEAF it uses (``media.*``,
``engines.tts.synthesize_cue``, and the pure helpers ``clone_languages`` /
``company_default_voice_gender`` / ``resolve_dub_voice`` from ``dubbing``) is imported lazily.

The algorithm is copied verbatim from the framework so the output matches byte-for-byte-ish:

  * **Constant audio speed** (rule 14): the TTS/keep-source audio for a cue plays at its natural
    length — NEVER time-stretched. Only ``media.normalize_wav`` (format-only) runs. The picture
    absorbs 100% of the mismatch later, via the freeze plan the muxer consumes.
  * **Running-gap freeze plan** (``_plan_freezes``): ``gap_i = rendered_start - (caption_start +
    cum_freeze - cum_trim)``. ``gap>0`` → hold the frame; ``gap<0`` → trim (Model A, langs in
    ``freeze_trim_languages``) or clamp with an honest residual (Model B). A tail entry reconciles
    picture-end to audio-end.
  * **keep-source-audio** cues (rule 14): splice ``source_audio[start:end]`` instead of TTS.
  * **Citation strip** (rule 16): the TTS copy has ``(Quran s:a)`` removed; the caption keeps it.
  * **Clone-ref head-slice** (~25s) for XTTS clone languages.

Returns a plain dict describing the produced dub wav + the freeze plan + a sync report block —
the caller (``ytpipe``) persists them as sidecar files.
"""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from typing import Any

# Longest run of pure silence tolerated in a dub before it is a hard problem (rule 14 blocker
# in the framework; here surfaced in the returned report for the caller to act on). Floor below
# which inter-cue gaps are routine, not holes.
SILENT_SPAN_FLOOR_MS = 250


class DubError(RuntimeError):
    """A dub could not be rendered (missing voice, missing source for a keep-source cue, …)."""


# --------------------------------------------------------------------------- pure helpers (port)

def _strip_citations(text: str) -> str:
    """Port of ``dubbing._strip_citations`` — reuse the framework's exact regex + collapse."""
    from video_translation_house.dubbing import _strip_citations as fw  # lazy, pure

    return fw(text)


def _natural_pause_before(cues: list[dict[str, Any]], idx: int, gap_ms: int = 700) -> bool:
    if idx == 0:
        return True
    prev = cues[idx - 1]
    return (cues[idx]["start_ms"] - prev["end_ms"]) >= gap_ms


def _lead_silence_ms(
    cues: list[dict[str, Any]], idx: int, timeline_ms: int, *, is_still_image: bool
) -> int:
    """Port of ``dubbing._lead_silence_ms`` (see that docstring for the still-image rationale)."""
    if not is_still_image:
        return max(0, cues[idx]["start_ms"] - timeline_ms)
    if idx == 0:
        return max(0, cues[idx]["start_ms"] - timeline_ms)
    prev = cues[idx - 1]
    pause = cues[idx]["start_ms"] - prev["end_ms"]
    if pause >= 700:
        return pause
    return 0


def plan_freezes(
    cue_measures: list[dict[str, Any]],
    *,
    trim: bool = False,
    source_duration_ms: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Port of ``dubbing._plan_freezes`` — running-gap picture re-timing + tail reconciliation."""
    freezes: list[dict[str, Any]] = []
    trims: list[dict[str, Any]] = []
    cum_freeze = 0
    cum_trim = 0
    last_measure: dict[str, Any] | None = None
    for m in cue_measures:
        cap_start = int(m["caption_start_ms"])
        slot_ms = max(1, int(m["caption_end_ms"]) - cap_start)
        natural_ms = int(m["natural_ms"])
        sf = round(natural_ms / slot_ms, 4) if slot_ms else 1.0
        gap = int(m["rendered_start_ms"]) - (cap_start + cum_freeze - cum_trim)
        if gap > 0:
            freezes.append({
                "cue_id": int(m["id"]), "at_ms": cap_start, "freeze_ms": int(gap),
                "natural_ms": natural_ms, "slot_ms": int(slot_ms), "stretch_factor": sf,
            })
            cum_freeze += gap
        elif gap < 0 and trim:
            trims.append({
                "cue_id": int(m["id"]), "at_ms": cap_start, "trim_ms": int(-gap),
                "natural_ms": natural_ms, "slot_ms": int(slot_ms), "stretch_factor": sf,
            })
            cum_trim += -gap
        last_measure = m

    if source_duration_ms is not None and last_measure is not None:
        src_dur = int(source_duration_ms)
        final_audio_end = int(last_measure["rendered_end_ms"])
        picture_end = src_dur + cum_freeze - cum_trim
        tail_gap = final_audio_end - picture_end
        tail_cue = int(last_measure["id"])
        if tail_gap > 0:
            freezes.append({
                "cue_id": tail_cue, "at_ms": src_dur, "freeze_ms": int(tail_gap),
                "natural_ms": 0, "slot_ms": 1, "stretch_factor": 1.0, "tail": True,
            })
            cum_freeze += tail_gap
        elif tail_gap < 0 and trim:
            trim_ms = -tail_gap
            trims.append({
                "cue_id": tail_cue, "at_ms": max(0, src_dur - trim_ms), "trim_ms": int(trim_ms),
                "natural_ms": 0, "slot_ms": 1, "stretch_factor": 1.0, "tail": True,
            })
            cum_trim += trim_ms
    return {"freezes": freezes, "trims": trims}


def _audio_bars(root: Path) -> dict[str, Any]:
    """Company ``quality_bars.audio`` (read live; never hardcode)."""
    from video_translation_house.util import load_company_config  # lazy

    bars = (load_company_config(root).get("quality_bars", {}) or {}).get("audio", {}) or {}
    return {
        "target_lufs": float(bars.get("target_lufs", -16.0)),
        "per_cue_drift_tolerance_ms": int(bars.get("per_cue_drift_tolerance_ms", 150)),
        "max_freeze_ms_per_cue": int(bars.get("max_freeze_ms_per_cue", 4000)),
        "freeze_trim_languages": [str(x) for x in bars.get("freeze_trim_languages", [])],
        "silent_span_max_ms": int(bars.get("silent_span_max_ms", 7000)),
    }


# --------------------------------------------------------------------------- voice resolution

def _clone_ref_trim(root: Path, src_wav: Path, *, dest_dir: Path, seconds: int = 25) -> Path:
    """Cached head-slice of ``src_wav`` for XTTS clone reference (port of the framework helper)."""
    from video_translation_house import media as media_mod  # lazy

    if seconds <= 0 or not src_wav.is_file():
        return src_wav
    try:
        total_ms = media_mod.audio_duration_ms(src_wav)
        if total_ms <= seconds * 1000:
            return src_wav
        mtime = int(src_wav.stat().st_mtime)
        cache_dir = dest_dir / ".clone-ref-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        dest = cache_dir / f"head-{seconds}s-{mtime}.wav"
        if dest.is_file() and media_mod.audio_duration_ms(dest) > 0:
            return dest
        media_mod.slice_wav(
            src_wav, dest, start_seconds=0.0, duration_seconds=float(seconds),
            sample_rate=media_mod.DUB_SAMPLE_RATE, channels=media_mod.DUB_CHANNELS,
        )
        return dest if dest.is_file() and media_mod.audio_duration_ms(dest) > 0 else src_wav
    except Exception:  # noqa: BLE001 - trimming is best-effort; full file is always valid
        return src_wav


def _clone_ref_trim_seconds(cfg: dict[str, Any]) -> int:
    """Head-slice length from the effective config's ``xtts_worker.clone_ref_trim_seconds``."""
    try:
        return int((cfg.get("xtts_worker") or {}).get("clone_ref_trim_seconds", 25))
    except (TypeError, ValueError):
        return 25


# --------------------------------------------------------------------------- render

def run_dub(
    root: Path,
    *,
    language: str,
    caption_doc: dict[str, Any],
    dub_wav: Path | str,
    is_clone: bool,
    source_audio: Path | str | None = None,
    source_video: Path | str | None = None,
    still_image: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    voice: str | None = None,
    gender: str | None = None,
    clone_ref_trim_seconds: int = 25,
) -> dict[str, Any]:
    """Render ``caption_doc`` into ``dub_wav`` for ``language``.

    ``is_clone`` — this language dubs via XTTS voice-clone off ``source_audio`` (resolved by
    ``langspec`` from company ``clone_languages``); gender/registry lookup is skipped.
    ``still_image`` — a per-language fixed frame (rule 15): no freeze plan is produced (a static
    frame can't desync). ``source_video`` supplies the tail-reconciliation duration for the
    freeze plan (skipped when None or still-image).

    Voice resolution (non-clone, no explicit ``model``): ``gender`` → company default (male);
    the staged ``(language, gender)`` voice is looked up in the company registry and this raises
    ``DubError`` if none is staged (e.g. ``fa`` has no piper voice) — never a silent wrong voice.

    Returns ``{"dub": <abs path>, "freeze_plan": {...}|None, "sync": {...},
    "freeze_plan_skipped_reason": <str>|None}``.
    """
    from video_translation_house import media as media_mod  # lazy
    from video_translation_house.dubbing import (  # lazy — pure config readers
        company_default_voice_gender,
        resolve_dub_voice,
    )
    from video_translation_house.engines import tts as tts_mod  # lazy

    language = str(language).strip().lower()
    cues = caption_doc.get("cues") or []
    if not cues:
        raise DubError(f"captions for {language!r} have no cues to dub")

    bars = _audio_bars(root)
    sr, ch = media_mod.DUB_SAMPLE_RATE, media_mod.DUB_CHANNELS
    dub_wav = Path(dub_wav).expanduser().resolve()
    dub_wav.parent.mkdir(parents=True, exist_ok=True)

    # --- keep-source-audio precondition: need the source WAV present for any flagged cue.
    keep_source_any = any("keep-source-audio" in (c.get("flags") or []) for c in cues)
    src_audio = Path(source_audio).expanduser().resolve() if source_audio else None
    if keep_source_any and (src_audio is None or not src_audio.is_file()):
        raise DubError(
            "a cue is flagged keep-source-audio but the source audio is missing; the original "
            "speaker audio is required to splice the marked window."
        )

    # --- clone reference (XTTS): a short cached head-slice of the source speaker's audio.
    clone_ref: Path | None = None
    if is_clone:
        if src_audio is None or not src_audio.is_file():
            raise DubError(
                f"language {language!r} dubs by voice-clone but no source audio was provided "
                "as the clone reference."
            )
        provider = "xtts"
        clone_ref = _clone_ref_trim(
            root, src_audio, dest_dir=dub_wav.parent, seconds=clone_ref_trim_seconds,
        )

    # --- gender-aware voice selection (non-clone, no explicit model): fail loud if unstaged.
    resolved_gender: str | None = None
    if model is None and not is_clone:
        resolved_gender = gender or company_default_voice_gender(root)
        if resolved_gender not in ("male", "female"):
            resolved_gender = company_default_voice_gender(root)
        picked = resolve_dub_voice(root, language, resolved_gender)
        if picked is None:
            raise DubError(
                f"no {resolved_gender!r} dub voice is staged for language {language!r}. "
                f"Stage a {resolved_gender} piper .onnx under company.local.json → "
                f"dubbing.voices.{language}.{resolved_gender}, or pass an explicit model. "
                f"(Note: 'fa' has no staged piper voice.)"
            )
        provider = provider or picked["provider"]
        model = picked["model"]

    is_still_image = still_image is not None

    # --- per-cue synthesis loop (constant audio speed; picture retimed later).
    parts: list[Path] = []
    cue_measures: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix=f"ytapp-dub-{language}-") as tmp:
        tmpdir = Path(tmp)
        timeline_ms = 0
        for i, cue in enumerate(cues):
            slot_ms = max(1, int(cue["end_ms"]) - int(cue["start_ms"]))
            lead = _lead_silence_ms(cues, i, timeline_ms, is_still_image=is_still_image)
            if lead > 0:
                gap = tmpdir / f"gap-{i}.wav"
                media_mod.silent_wav(gap, duration_ms=lead, sample_rate=sr, channels=ch)
                parts.append(gap)
                timeline_ms += lead

            raw = tmpdir / f"cue-{i}.raw.wav"
            keep_source = "keep-source-audio" in (cue.get("flags") or [])
            if keep_source:
                media_mod.slice_wav(
                    src_audio, raw,
                    start_seconds=int(cue["start_ms"]) / 1000.0,
                    duration_seconds=max(1, int(cue["end_ms"]) - int(cue["start_ms"])) / 1000.0,
                    sample_rate=sr, channels=ch,
                )
            elif (cue.get("target_text") or "").strip():
                spoken = _strip_citations(cue["target_text"])
                tts_mod.synthesize_cue(
                    spoken, raw, provider=provider, language=language,
                    model=model, voice=voice, clone_ref=clone_ref, root=root,
                )
            else:
                media_mod.silent_wav(raw, duration_ms=1, sample_rate=sr, channels=ch)

            natural_ms = media_mod.audio_duration_ms(raw)
            stretch = (natural_ms / slot_ms) if slot_ms else 1.0
            fit = tmpdir / f"cue-{i}.fit.wav"
            media_mod.normalize_wav(raw, fit, sample_rate=sr, channels=ch)  # format only
            parts.append(fit)
            rendered_ms = media_mod.audio_duration_ms(fit)
            rendered_start = timeline_ms
            timeline_ms += rendered_ms
            cue_measures.append({
                "id": int(cue["id"]),
                "caption_start_ms": int(cue["start_ms"]),
                "caption_end_ms": int(cue["end_ms"]),
                "rendered_start_ms": rendered_start,
                "rendered_end_ms": timeline_ms,
                "natural_ms": natural_ms,
                "stretch_factor": round(stretch, 4),
                "keep_source": keep_source,
            })

        concat = tmpdir / "concat.wav"
        media_mod.concat_wavs(parts, concat, sample_rate=sr, channels=ch)
        media_mod.loudnorm(concat, dub_wav, target_lufs=bars["target_lufs"],
                           sample_rate=sr, channels=ch)

    # --- freeze plan (skipped for still-image langs — a static frame can't desync).
    freeze_plan: dict[str, Any] | None
    skipped_reason: str | None = None
    if is_still_image:
        freeze_plan = None
        skipped_reason = "still-image language: fixed frame cannot desync"
    else:
        source_duration_ms = None
        if source_video:
            sv = Path(source_video).expanduser().resolve()
            if sv.is_file():
                source_duration_ms = int(media_mod.audio_duration_ms(sv))
        trim_mode = language in bars["freeze_trim_languages"]
        freeze_plan = plan_freezes(
            cue_measures, trim=trim_mode, source_duration_ms=source_duration_ms,
        )

    sync = build_sync_report(
        language, cue_measures, bars,
        freeze_plan=freeze_plan, still_image=is_still_image,
        provider=provider, model=model, voice_gender=resolved_gender,
    )
    return {
        "dub": str(dub_wav),
        "freeze_plan": freeze_plan,
        "sync": sync,
        "freeze_plan_skipped_reason": skipped_reason,
    }


# --------------------------------------------------------------------------- sync report (port)

def build_sync_report(
    language: str,
    cue_measures: list[dict[str, Any]],
    bars: dict[str, Any],
    *,
    freeze_plan: dict[str, list[dict[str, Any]]] | None = None,
    still_image: bool = False,
    provider: str | None = None,
    model: str | None = None,
    voice_gender: str | None = None,
) -> dict[str, Any]:
    """Port of ``dubbing._build_sync_report`` — post-adjust drift + silent-span analysis.

    Returns a single language block (no file I/O, no schema validation, no artifact
    registration — the caller persists it). ``over_tolerance`` / ``silent_span_over_bar`` flags
    let ``ytpipe`` surface a problem the way the framework's ``analyze_sync`` would.
    """
    tol = bars["per_cue_drift_tolerance_ms"]
    freeze_mode = freeze_plan is not None
    frozen_ms_by_cue = {int(f["cue_id"]): int(f["freeze_ms"])
                        for f in (freeze_plan or {}).get("freezes", []) if not f.get("tail")}
    trim_ms_by_cue = {int(t["cue_id"]): int(t["trim_ms"])
                      for t in (freeze_plan or {}).get("trims", []) if not t.get("tail")}

    schedule = [{"start_ms": m["caption_start_ms"], "end_ms": m["caption_end_ms"]}
                for m in cue_measures]
    cues_out: list[dict[str, Any]] = []
    cumulative = 0
    max_abs = 0
    over_tol: list[int] = []
    cumulative_freeze = 0
    cumulative_trim = 0
    for i, m in enumerate(cue_measures):
        freeze_planned = m["id"] in frozen_ms_by_cue
        trim_planned = m["id"] in trim_ms_by_cue
        if still_image:
            drift = 0
            cumulative = 0
            reset = False
        elif freeze_mode:
            cumulative_freeze += frozen_ms_by_cue.get(m["id"], 0)
            cumulative_trim += trim_ms_by_cue.get(m["id"], 0)
            net = cumulative_freeze - cumulative_trim
            drift = int(m["rendered_start_ms"]) - (int(m["caption_start_ms"]) + net)
            cumulative = drift
            reset = False
        else:
            drift = int(m["rendered_end_ms"] - m["caption_end_ms"])
            reset = _natural_pause_before(schedule, i)
            if reset:
                cumulative = 0
            cumulative += drift
        max_abs = max(max_abs, abs(cumulative))
        is_over_tol = abs(cumulative) > tol
        if is_over_tol:
            over_tol.append(m["id"])
        cues_out.append({
            "id": m["id"],
            "caption_start_ms": m["caption_start_ms"],
            "caption_end_ms": m["caption_end_ms"],
            "rendered_start_ms": m["rendered_start_ms"],
            "rendered_end_ms": m["rendered_end_ms"],
            "drift_ms": drift,
            "stretch_factor": m["stretch_factor"],
            "over_tolerance": is_over_tol,
            "reset_here": reset,
            "freeze_planned": freeze_planned,
            "freeze_ms": frozen_ms_by_cue.get(m["id"], 0),
            "trim_planned": trim_planned,
            "trim_ms": trim_ms_by_cue.get(m["id"], 0),
        })

    # Silent-span QA (rule 14): pure-silence spans on the rendered timeline; keep-source exempt.
    audible = [m for m in cue_measures
               if m.get("keep_source") or int(m.get("natural_ms", 0)) > 1]
    keep_bounds = [(int(m["rendered_start_ms"]), int(m["rendered_end_ms"]))
                   for m in cue_measures if m.get("keep_source")]

    def _touches_keep(start: int, end: int) -> bool:
        return any(ks < end and start < ke for ks, ke in keep_bounds) or any(
            ke == start or ks == end for ks, ke in keep_bounds
        )

    silent_spans: list[dict[str, int]] = []
    prev_end = 0
    for m in audible:
        gap_start = prev_end
        gap_end = int(m["rendered_start_ms"])
        if gap_end - gap_start >= SILENT_SPAN_FLOOR_MS and not _touches_keep(gap_start, gap_end):
            silent_spans.append({
                "start_ms": gap_start, "end_ms": gap_end, "duration_ms": gap_end - gap_start,
            })
        prev_end = max(prev_end, int(m["rendered_end_ms"]))
    max_silent_span = max((s["duration_ms"] for s in silent_spans), default=0)

    return {
        "language": language,
        "provider": provider,
        "model": model,
        "voice_gender": voice_gender,
        "target_lufs": bars["target_lufs"],
        "cumulative_offset_ms": int(cumulative),
        "max_abs_drift_ms": int(max_abs),
        "cues_over_tolerance": over_tol,
        "silent_spans_ms": silent_spans,
        "max_silent_span_ms": int(max_silent_span),
        "silent_span_over_bar": bool(max_silent_span > bars["silent_span_max_ms"]),
        "still_image": bool(still_image),
        "freeze_frame_enabled": bool(freeze_plan is not None),
        "total_freeze_ms": int(sum(int(f["freeze_ms"]) for f in (freeze_plan or {}).get("freezes", []))),
        "total_trim_ms": int(sum(int(t["trim_ms"]) for t in (freeze_plan or {}).get("trims", []))),
        "cues": cues_out,
    }
