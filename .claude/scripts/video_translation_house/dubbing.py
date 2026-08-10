"""Per-language dubbing + sync (CAPTION_VALIDATION -> AUDIO_QA_GATE).

Mirrors the translate.py contract and the Phase-2/3 export/import posture:

  * The CLI never imports an ML library. TTS runs as a subprocess via engines/tts.py; if no
    engine is installed, callers use `dub import` on a pre-rendered WAV — the whole pipeline
    stays exercisable offline (exactly as `transcript import` does for ASR).
  * Only tracks with `dub_enabled: true` are dubbed; caption-only tracks skip to PACKAGE.
  * Default voice is NEUTRAL. Cloning the source speaker requires `voice_clone_consent: true`
    in the rights record — run_dub refuses a clone request otherwise (company rule 5).
  * The dub track is fit to the caption timing that is the downstream contract: each cue is
    synthesized, then tempo-fit into its slot up to the configured stretch cap. Beyond the
    cap we DO NOT force an unnatural stretch — we clamp, flag the cue, and let drift accrue
    (surfaced in the sync report). Cumulative drift is reset at natural pauses so one long
    cue cannot poison the whole track.
  * Reports are AGGREGATE (one `audio-sync` file per project, PASS iff every active dub track
    passes); approvals are PER-LANGUAGE (`audio_qa` in state.py per_language_gates). This
    reuses the Phase-3 split — no state.py / paths.py changes.

Gate report filename is keyed by REPORT TYPE ("audio-sync"), matching
workflow_states.json.gate_reports (the Phase-2 rule transition_blockers relies on).
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from . import artifacts as artifacts_mod
from . import media as media_mod
from . import obs
from .engines import tts as tts_mod
from .errors import ConfigurationError, DubbingError, EngineUnavailableError
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
    load_company_config,
    load_json,
    load_yaml,
    project_lock,
    utc_now,
)
from .validation import require_valid

# Language tracks pass through these stages during Phase 4.
TRACK_STAGE_DUBBED = "AUDIO_SYNC_ADJUST"   # dub rendered; sync measured
TRACK_STAGE_SYNCED = "AUDIO_QA_GATE"       # ready for the human audio_qa gate

_STATES_ALLOWING_DUB = {"CAPTION_VALIDATION", "DUBBING", "AUDIO_SYNC_ADJUST", "AUDIO_QA_GATE"}


# --- helpers -----------------------------------------------------------------

def dub_relpath(language: str) -> str:
    """Project-relative path for a language's dub track: audio/<lang>/dub.wav."""
    return f"audio/{language}/dub.wav"


# Inline Quran citations like "(Quran 100:6)" / "(Quran 41:20-21)" belong in the CAPTION text
# (rule 16) but must NOT be spoken by the dub — piper would voice "Quran one hundred six" mid-verse.
# So the dub synthesizes from a citation-stripped copy of target_text; the caption doc/render path
# keep target_text verbatim (this never mutates the caption artifact). Applies in EVERY language
# incl. `ar` (the Arabic caption shows the citation, but the Arabic dub shouldn't say "(Quran …)").
# The `[^)]*` class matches ranges and multi-part refs; the leading `\s*` prevents a double space.
_QURAN_CITATION_RE = re.compile(r"\s*\(Quran[^)]*\)")


def _strip_citations(text: str) -> str:
    """Remove inline ``(Quran s:a)`` citations from a caption string before TTS synthesis.

    Caption text carries the citation for the reader; the spoken dub does not. Pure/deterministic:
    strips every citation token, then collapses the whitespace the removal frees up so no double
    spaces remain. No-op on citation-free text."""
    stripped = _QURAN_CITATION_RE.sub("", text)
    # Collapse any run of spaces/tabs the removal left mid-line; keep newlines intact.
    stripped = re.sub(r"[ \t]{2,}", " ", stripped)
    return stripped.strip()


# Male is the HARD default dub voice for every language (company rule / TASK 2). A voice is
# chosen from the company `dubbing.voices` registry by (language, gender); an explicit
# `dub run --model` still overrides. Voice-clone CONSENT is a separate rights-gate fact (rule 5)
# and is NOT what selects gender.
DEFAULT_VOICE_GENDER = "male"


def company_default_voice_gender(root: Path) -> str:
    """Company-wide default dub voice gender (male unless overridden in company config)."""
    company = load_company_config(root)
    g = (company.get("dubbing", {}) or {}).get("default_voice_gender", DEFAULT_VOICE_GENDER)
    return g if g in ("male", "female") else DEFAULT_VOICE_GENDER


def clone_languages(root: Path) -> list[str]:
    """Languages that are ALWAYS dubbed via XTTS voice-clone (company policy), bypassing the
    piper gender registry — the male/female axis does not apply to them. See
    `company.default.json` → `dubbing.clone_languages` (default ["en","zh"]). Consent-gated
    (rule 5); `dub run` fails loud if consent/engine/clone-ref is missing."""
    return list((load_company_config(root).get("dubbing", {}) or {}).get("clone_languages", []) or [])


def resolve_dub_voice(root: Path, language: str, gender: str) -> dict[str, str] | None:
    """Look up the staged voice for (language, gender) in the company `dubbing.voices` registry.

    Returns ``{"provider", "model", "gender", "voice_source"}`` when a voice with a real
    ``model`` path is registered, else ``None`` (caller decides how to surface the gap).
    """
    company = load_company_config(root)
    voices = (company.get("dubbing", {}) or {}).get("voices", {}) or {}
    entry = (voices.get(language, {}) or {}).get(gender)
    if not entry or not entry.get("model"):
        return None
    return {
        "provider": entry.get("provider") or "piper",
        "model": entry["model"],
        "gender": gender,
        "voice_source": "company.dubbing.voices",
    }


def sync_report_relpath() -> str:
    return "audio/sync-report.json"


def freeze_plan_relpath(language: str) -> str:
    """Project-relative path for a language's freeze plan: audio/freeze-plan.<lang>.json."""
    return f"audio/freeze-plan.{language}.json"


def _audio_quality_bars(root: Path) -> dict[str, Any]:
    """Read quality_bars.audio from company.default.json (never hardcode the bars)."""
    from .util import load_company_config

    company = load_company_config(root)
    bars = company.get("quality_bars", {}).get("audio", {})
    return {
        "target_lufs": float(bars.get("target_lufs", -16.0)),
        "max_time_stretch": float(bars.get("max_time_stretch", 1.3)),
        "per_cue_drift_tolerance_ms": int(bars.get("per_cue_drift_tolerance_ms", 150)),
        "cumulative_drift_ceiling_ms": int(bars.get("cumulative_drift_ceiling_ms", 500)),
        "freeze_frame_enabled": bool(bars.get("freeze_frame_enabled", False)),
        "freeze_stretch_cap": float(bars.get("freeze_stretch_cap", 1.15)),
        "max_freeze_ms_per_cue": int(bars.get("max_freeze_ms_per_cue", 4000)),
        # Languages that use freeze-frame Model A (freeze + TRIM → residual 0); every other
        # freeze-mode language uses Model B (freeze/hold only, honest residual). Default: none.
        "freeze_trim_languages": [str(x) for x in bars.get("freeze_trim_languages", [])],
        # Longest allowed run of pure silence in a dub before it FAILs (blocker). Guards against
        # untranscribed/untranslated speech leaving the dub dead-air; keep-source-audio windows are
        # exempt (the reciter's own voice fills them). See CLAUDE.md rule 14.
        "silent_span_max_ms": int(bars.get("silent_span_max_ms", 7000)),
    }


def _track(state: dict[str, Any], language: str) -> dict[str, Any]:
    return state.get("language_tracks", {}).get(language, {})


def _dub_enabled(state: dict[str, Any], language: str) -> bool:
    return bool(_track(state, language).get("dub_enabled", False))


def _dub_langs(state: dict[str, Any]) -> list[str]:
    """Active tracks that participate in dubbing/mux — the quorum for those top-state edges.

    Caption-only tracks (dub_enabled=false) legitimately skip DUBBING..VIDEO_MUX, so they are
    excluded here; otherwise they would stall `_maybe_advance_top` on a mixed project."""
    return [lang for lang in _active_track_langs(state) if _dub_enabled(state, lang)]


def _natural_pause_before(cues: list[dict[str, Any]], idx: int, gap_ms: int = 700) -> bool:
    """A cue starts a fresh 'phrase' (safe cumulative-offset reset point) when the silence
    before it is long enough to absorb prior drift."""
    if idx == 0:
        return True
    prev = cues[idx - 1]
    return (cues[idx]["start_ms"] - prev["end_ms"]) >= gap_ms


def _lead_silence_ms(cues: list[dict[str, Any]], idx: int, timeline_ms: int, *,
                      is_still_image: bool) -> int:
    """How much silence (if any) to insert before cue ``idx`` on the running dub timeline.

    Non-still-image tracks always wait for the cue's nominal caption start (the freeze plan
    absorbs any resulting slot mismatch on the picture side later): ``lead = start_ms -
    timeline_ms``, i.e. re-anchored to the ABSOLUTE caption schedule.

    Still-image tracks (rule 15) get no freeze plan — a static frame can't desync — so
    re-anchoring to the absolute caption schedule is wrong: once earlier cues have overrun their
    slots (very common — translated speech rarely matches the source's per-cue pacing), the
    caption schedule and the audio timeline have already diverged, and "waiting" for the next
    absolute timestamp reinserts that ENTIRE accumulated divergence as one silence block (seen
    live: an 860ms real source pause reinserting ~165s of dead air because prior cues had drifted
    that far behind). So for still-image tracks the timeline never re-anchors to `start_ms` at
    all — only the RELATIVE gap to the immediately preceding cue matters: play back-to-back
    (``_natural_pause_before`` false) or insert exactly that cue-to-cue pause's own duration
    (true), never the absolute lead.
    """
    if not is_still_image:
        return max(0, cues[idx]["start_ms"] - timeline_ms)
    if idx == 0:
        return max(0, cues[idx]["start_ms"] - timeline_ms)
    prev = cues[idx - 1]
    pause = cues[idx]["start_ms"] - prev["end_ms"]
    if pause >= 700:
        return pause
    return 0


def _plan_freezes(
    cue_measures: list[dict[str, Any]],
    bars: dict[str, Any],
    *,
    trim: bool = False,
    source_duration_ms: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Pure: from per-cue measures (already gathered by run_dub), decide how to re-time the
    picture at each cue boundary so it tracks the back-to-back dubbed audio.

    Under freeze-frame policy the per-cue loop fits audio only to ``freeze_stretch_cap`` (gentle),
    so a cue's rendered audio can run longer OR shorter than its slot. That mismatch does NOT stay
    local: run_dub lays cue audio back-to-back (lead silence only when the audio is EARLY), so a
    long cue pushes every following cue's audio *later* until a natural gap lets it catch up. We
    therefore plan on a **running gap** between the audio playhead and the (already-retimed)
    picture playhead. ``rendered_start_ms``/``caption_start_ms`` come straight from the audio
    timeline run_dub built, so the gap is real, not modelled::

        net   = cumulative_freeze - cumulative_trim          # picture shift so far
        gap_i = rendered_start_ms[i] - (caption_start_ms[i] + net)

    ``gap_i > 0`` — audio is later than picture: **hold** the frame for ``gap_i`` at this cue's
    start (``at_ms == caption_start_ms``, always a cue boundary). Always emitted.

    ``gap_i < 0`` — audio is earlier than picture: only in **trim mode** (Model A) do we **trim**
    the picture by ``-gap_i`` there (skip that much source) so the picture catches up → residual 0
    by construction. In hold-only mode (Model B) we clamp at 0 and never drop source frames, so a
    one-sided residual (picture lagging the audio) remains and is surfaced honestly as drift in the
    sync report. See CLAUDE.md rule 14.

    **Tail reconciliation.** Cue-boundary gaps only align cue *starts*; the last cue's own
    slot→render mismatch and any source that runs *past the last cue* (the video tail) are still
    unaccounted, so the total picture length (``source_duration_ms + cum_freeze - cum_trim``) can
    overshoot or undershoot the audio's total length (``rendered_end_ms`` of the last cue). When
    ``source_duration_ms`` is given we emit one final tail entry at ``at_ms == source_duration_ms``
    to reconcile the *ends*: ``tail_gap = final_audio_end - picture_end``. ``tail_gap > 0`` (audio
    longer than picture) → **freeze** the last frame for ``tail_gap`` (always emitted, both modes,
    so the picture never ends before the audio). ``tail_gap < 0`` (picture longer than audio, e.g.
    an untrimmed video tail after the last cue) → in **trim mode** trim the picture tail by
    ``-tail_gap`` so picture end == audio end; in hold-only mode leave the honest residual.

    Returns ``{"freezes": [...], "trims": [...]}`` in cue order (trims empty unless ``trim``);
    the tail entry, when present, sorts last by ``at_ms``.
    """
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

    # Tail reconciliation: make the picture END where the audio ENDS.
    #   picture_end = source_duration_ms + cum_freeze - cum_trim
    #   tail_gap    = final_audio_end - picture_end
    # A tail FREEZE holds the last frame at at_ms == source_duration_ms (the full source slice is
    # emitted first, then the hold). A tail TRIM drops [at, at+trim_ms); to shorten the *end* of the
    # source it must land at at_ms == source_duration_ms - trim_ms so the dropped span is real source
    # (a trim at source_duration_ms would drop nothing). The tail entry sorts last by at_ms.
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


def _source_video_duration_ms(paths: ProjectPaths) -> int | None:
    """Duration of the source picture in ms, resolved the same way packaging's mux does
    (``audio_duration_ms`` over the source video container), or None if no source video is present.
    Used for tail reconciliation in ``_plan_freezes`` so the retimed picture ends where the dub
    audio ends."""
    for child in sorted(paths.source_dir.glob("*")):
        if child.suffix.lower() in {".mp4", ".mkv", ".webm"}:
            return int(media_mod.audio_duration_ms(child))
    return None


def _still_image_for(cfg: dict[str, Any], language: str) -> str | None:
    """The per-language still-image path from project.yaml, or None (keep source video).

    Duplicated intentionally from packaging (which imports this module — importing it back would
    cycle); it's a trivial config read and both must agree on the same rule-15 semantics: a
    language present in `images` shows a fixed frame, so it gets no freeze plan and no drift."""
    img = (cfg.get("images") or {}).get(language)
    return img or None




# --- 1. render a dub ---------------------------------------------------------

def run_dub(
    root: Path,
    project_id: str,
    language: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    voice: str | None = None,
    gender: str | None = None,
    clone: bool = False,
    actor: str = "agent",
    advance: bool = False,
) -> dict[str, Any]:
    """Synthesize a full dub track for one language from captions.<lang>.json.

    Per cue: synthesize -> measure -> tempo-fit into the cue slot (up to the stretch cap;
    beyond the cap: clamp + flag, never force) -> place at start_ms with silence padding ->
    concat -> EBU R128 loudnorm to target LUFS. Registers the dub artifact, writes the sync
    report, advances the track to AUDIO_SYNC_ADJUST.
    """
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    if state["current_state"] not in _STATES_ALLOWING_DUB:
        raise ConfigurationError(
            f"dubbing expects the project at/through CAPTION_VALIDATION, "
            f"is at {state['current_state']}"
        )
    if not _dub_enabled(state, language):
        raise DubbingError(
            f"track {language!r} is not dub_enabled (caption-only); nothing to synthesize"
        )

    # Clone-language policy (company `dubbing.clone_languages`, default ["en","zh"]): these
    # languages are ALWAYS dubbed via XTTS voice-clone off the source speaker, NOT the piper
    # gender registry — gender does not apply to them. When the operator pinned neither --model
    # nor --clone, auto-promote to a clone dub here (before the consent gate below, so consent
    # is enforced for the auto case too). An explicit --model or --gender override still wins
    # (auto-clone only fires when --model is unset). `provider`/`clone_ref`/gender-skip all
    # follow from clone=True + provider="xtts" using the EXISTING machinery below.
    auto_clone = (not clone) and (model is None) and (language in clone_languages(root))
    if auto_clone:
        clone = True
        provider = "xtts"

    if clone and not check_rights(root, project_id).get("voice_clone_consent", False):
        raise DubbingError(
            f"voice cloning required for {language!r} but rights record has "
            "voice_clone_consent=false. "
            + ("This language is in company dubbing.clone_languages (always XTTS-clone); "
               if auto_clone else "")
            + "A human must record consent (rights set --voice-clone-consent) before cloning."
        )

    doc = load_captions(root, project_id, language)
    cues = doc["cues"]
    if not cues:
        raise DubbingError(f"captions for {language!r} have no cues to dub")

    bars = _audio_quality_bars(root)
    # CONSTANT AUDIO SPEED (rule 5 / TASK 2): dubbed audio is NEVER time-stretched to fit its
    # caption slot — every cue plays at its natural TTS length and the PICTURE is re-timed
    # (freeze/trim) around it at mux. `max_time_stretch`/`freeze_stretch_cap` are retained in
    # the bars snapshot for reporting/back-compat only; they no longer gate any audio speed.
    sr, ch = media_mod.DUB_SAMPLE_RATE, media_mod.DUB_CHANNELS

    clone_ref: Path | None = None
    if clone:
        # Reference speaker is the extracted source audio (consent already verified above).
        # If the source media was deleted after translation, recover it via the sanctioned
        # (fetch-gated) restore so cloning has a reference; a no-op when already present.
        src_wav = paths.source_dir / "audio.wav"
        if not src_wav.is_file():
            from .ingest import ensure_source_present

            ensure_source_present(root, project_id, actor=actor)
        clone_ref = src_wav if src_wav.is_file() else None

    # Keep-source-audio cues (rule 5 / rule 14): a cue flagged `keep-source-audio` plays the
    # ORIGINAL reciter/speaker audio under every language instead of TTS (untranslatable recited
    # passages — e.g. Quranic recitation). That splice needs the source WAV present even for a
    # non-clone dub, so restore it once (fetch-gated) before the loop if any cue is flagged.
    keep_source_any = any(
        "keep-source-audio" in (c.get("flags") or []) for c in cues
    )
    if keep_source_any:
        src_wav = paths.source_dir / "audio.wav"
        if not src_wav.is_file():
            from .ingest import ensure_source_present

            ensure_source_present(root, project_id, actor=actor)
        if not src_wav.is_file():
            raise DubbingError(
                "a cue is flagged keep-source-audio but source/audio.wav is missing and could "
                "not be restored (set VIDTRANS_FETCH_ENABLED=1 to re-fetch). The original "
                "speaker audio is required to splice the marked window."
            )

    # Gender-aware voice selection (TASK 2). Male is the hard default for every language.
    # When the operator did not pin an explicit --model and this is not a clone, resolve the
    # dub voice from the company `dubbing.voices` registry by (language, gender), where gender
    # is: an explicit override > the project's chosen dubbing.voice_gender > company default
    # (male). If no voice is staged for that (language, gender) we FAIL loudly rather than let
    # the engine fall back to whatever built-in (often wrong-gender) voice it ships with.
    resolved_gender: str | None = None
    if model is None and not clone:
        cfg = load_yaml(paths.config) or {}
        project_gender = (cfg.get("dubbing", {}) or {}).get("voice_gender")
        resolved_gender = gender or project_gender or company_default_voice_gender(root)
        if resolved_gender not in ("male", "female"):
            resolved_gender = company_default_voice_gender(root)
        picked = resolve_dub_voice(root, language, resolved_gender)
        if picked is None:
            raise DubbingError(
                f"no {resolved_gender!r} dub voice is staged for language {language!r}. "
                f"Male is the default; stage a {resolved_gender} piper .onnx and register it "
                f"under company.local.json → dubbing.voices.{language}.{resolved_gender} "
                f"(model = absolute .onnx path), or pass an explicit `dub run --model <path>` "
                f"to override. See OPERATING-GUIDE.md (dub voice staging)."
            )
        provider = provider or picked["provider"]
        model = picked["model"]

    # Resolved early (not just at freeze-plan time below) because it changes how lead-silence is
    # placed in the synthesis loop right below.
    still_image_path = _still_image_for(load_yaml(paths.config) or {}, language)
    is_still_image = still_image_path is not None

    parts: list[Path] = []
    cue_measures: list[dict[str, Any]] = []
    total_cues = len(cues)
    obs.phase(f"dubbing {language} (synthesizing cues)")
    with tempfile.TemporaryDirectory(prefix=f"dub-{language}-") as tmp:
        tmpdir = Path(tmp)
        timeline_ms = 0
        for i, cue in enumerate(cues):
            # Per-cue liveness: the heartbeat thread renders "cue N/total, Nm elapsed" every ~30s.
            # A cheap shared-state write (no per-cue print — the timer does the printing).
            obs.progress(i + 1, total_cues, unit="cue")
            slot_ms = max(1, cue["end_ms"] - cue["start_ms"])
            # Lead silence up to this cue's start (keeps cues time-anchored) — see
            # `_lead_silence_ms` for the still-image exception (rule 15).
            lead = _lead_silence_ms(cues, i, timeline_ms, is_still_image=is_still_image)
            if lead > 0:
                gap = tmpdir / f"gap-{i}.wav"
                media_mod.silent_wav(gap, duration_ms=lead, sample_rate=sr, channels=ch)
                parts.append(gap)
                timeline_ms += lead

            raw = tmpdir / f"cue-{i}.raw.wav"
            keep_source = "keep-source-audio" in (cue.get("flags") or [])
            if keep_source:
                # Splice the ORIGINAL source audio for this cue's window instead of calling TTS
                # (rule 5 / rule 14): the source speaker's own voice plays under every language for
                # a recited/untranslatable passage. The slice is real audio at its natural length,
                # so it flows through the same normalize -> concat -> loudnorm path as a TTS cue.
                src_wav = paths.source_dir / "audio.wav"
                media_mod.slice_wav(
                    src_wav, raw,
                    start_seconds=cue["start_ms"] / 1000.0,
                    duration_seconds=max(1, cue["end_ms"] - cue["start_ms"]) / 1000.0,
                    sample_rate=sr, channels=ch,
                )
            elif cue["target_text"].strip():
                # Speak the verse only — strip any inline (Quran s:a) caption citation (rule 16).
                spoken = _strip_citations(cue["target_text"])
                tts_mod.synthesize_cue(
                    spoken, raw, provider=provider, language=language,
                    model=model, voice=voice, clone_ref=clone_ref, root=root,
                    project_id=project_id,
                )
            else:
                # An empty/whitespace-only cue -- no TTS engine call, since feeding piper empty
                # stdin is unverified behavior. Synthesize silence directly instead of a
                # zero-length gap so downstream duration math stays sane.
                media_mod.silent_wav(raw, duration_ms=1, sample_rate=sr, channels=ch)
            natural_ms = media_mod.audio_duration_ms(raw)
            # No tempo change — normalize format only so the back-to-back concat is uniform.
            # stretch_factor is recorded for reporting (how far the natural length is from the
            # slot, i.e. how much picture re-timing the plan will absorb), NOT applied to audio.
            stretch = (natural_ms / slot_ms) if slot_ms else 1.0

            fit = tmpdir / f"cue-{i}.fit.wav"
            media_mod.normalize_wav(raw, fit, sample_rate=sr, channels=ch)
            parts.append(fit)
            rendered_ms = media_mod.audio_duration_ms(fit)
            rendered_start = timeline_ms
            timeline_ms += rendered_ms
            cue_measures.append({
                "id": cue["id"],
                "caption_start_ms": cue["start_ms"],
                "caption_end_ms": cue["end_ms"],
                "rendered_start_ms": rendered_start,
                "rendered_end_ms": timeline_ms,
                "natural_ms": natural_ms,
                "stretch_factor": round(stretch, 4),
                "over_stretch_cap": False,  # audio is never stretched → never over cap
                "keep_source": keep_source,  # spliced source audio → exempt from silent-span QA
            })

        obs.phase(f"dubbing {language} (concat + loudness normalize)")
        concat = tmpdir / "concat.wav"
        media_mod.concat_wavs(parts, concat, sample_rate=sr, channels=ch)
        dub_path = paths.directory / dub_relpath(language)
        dub_path.parent.mkdir(parents=True, exist_ok=True)
        media_mod.loudnorm(concat, dub_path, target_lufs=bars["target_lufs"],
                           sample_rate=sr, channels=ch)

    artifact = artifacts_mod.register_artifact(
        root, project_id, dub_path, "dub-wav", "DUBBING", actor, language=language,
    )
    # Freeze plan: how to re-time the picture to the audio at mux. Because audio is ALWAYS at
    # natural speed now (rule 5 / TASK 2), the picture MUST absorb 100% of every slot mismatch,
    # so the plan is ALWAYS computed (no opt-in flag gate). Languages in freeze_trim_languages
    # use Model A (freeze + trim → residual 0); the rest use Model B (hold only, honest residual).
    #
    # EXCEPTION — a still-image language needs NO freeze plan (rule 15): its picture at mux is a
    # single fixed frame stretched over the WHOLE dub, so audio-vs-picture drift is structurally
    # zero (a static frame can't desync). `package mux` already ignores the plan for these langs
    # (packaging._active_freeze_plan short-circuits on a configured image), so a plan built here
    # is not only unused — its hold-only residual would surface phantom `over_tolerance` drifts in
    # the sync report and FAIL `dub qa` for a scenario that cannot desync. So we skip planning and
    # tell the sync report this track is still-image (drift not measured against a moving picture).
    still_image = still_image_path
    trim_mode = language in bars["freeze_trim_languages"]
    if still_image is not None:
        freeze_plan = None
        freeze_plan_rel: str | None = None
    else:
        # Tail reconciliation needs the source-picture length (the same duration the mux rebuilds
        # against) so the retimed picture ends exactly where the dub audio ends. Resolve the source
        # video the same way packaging does; if it's absent (e.g. source media pruned) fall back to
        # None — the plan then only aligns cue boundaries, no tail entry.
        source_duration_ms = _source_video_duration_ms(paths)
        freeze_plan = _plan_freezes(cue_measures, bars, trim=trim_mode,
                                    source_duration_ms=source_duration_ms)
        freeze_plan_rel = None
        if freeze_plan is not None:
            freeze_plan_rel = _write_freeze_plan(
                root, project_id, language, freeze_plan, bars, trim_mode=trim_mode,
                dub_sha256=artifact["sha256"], actor=actor,
                source_artifact_id=artifact["artifact_id"],
            )
    provider_used = provider or _resolved_provider_label(root, language)
    voice_source = (
        "company.dubbing.voices" if resolved_gender is not None
        else ("explicit-model" if model is not None
              else ("company.dubbing.clone_languages" if auto_clone
                    else ("voice-clone" if clone else None)))
    )
    report = _build_sync_report(
        root, project_id, language, cue_measures, bars,
        provider=provider_used, model=model, dub_sha256=artifact["sha256"], actor=actor,
        freeze_plan=freeze_plan, voice_gender=resolved_gender, voice_source=voice_source,
        still_image=still_image is not None,
    )
    _set_track(root, project_id, language, stage=TRACK_STAGE_DUBBED,
               status="in_progress", actor=actor, notes="dub rendered; sync measured")
    append_event(paths.events, project_id, "DUB_RENDERED", actor, {
        "language": language, "cues": len(cues), "provider": provider_used,
        "voice_gender": resolved_gender, "dub_sha256": artifact["sha256"], "cloned": bool(clone),
        "freezes": len(freeze_plan["freezes"]) if freeze_plan is not None else 0,
        "trims": len(freeze_plan["trims"]) if freeze_plan is not None else 0,
    })
    advanced = _maybe_advance_top(root, project_id, actor, advance, "CAPTION_VALIDATION",
                                  "DUBBING", TRACK_STAGE_DUBBED,
                                  _dub_langs(load_json(paths.state)))
    return {"dub": _rel(dub_path, paths), "artifact": artifact, "language": language,
            "sync": report["languages"][language], "advanced_to": advanced,
            "freeze_plan": freeze_plan_rel}


def _resolved_provider_label(root: Path, language: str) -> str:
    try:
        return tts_mod._resolve_provider(None, language, root=root)
    except EngineUnavailableError:
        return "unknown"


# --- 2. import a pre-rendered dub (no-engine path) ---------------------------

def import_dub(
    root: Path,
    project_id: str,
    language: str,
    *,
    from_path: str | Path,
    actor: str = "agent",
    advance: bool = False,
) -> dict[str, Any]:
    """Ingest a pre-rendered dub WAV (dubbed out-of-band) as audio/<lang>/dub.wav, build the
    sync report from caption timing + the imported track's duration, register, and advance.

    This mirrors `transcript import`: it lets the whole Phase-4 pipeline run with NO TTS
    engine installed. Per-cue drift can only be estimated from the caption schedule here (we
    do not have per-cue boundaries in an opaque imported track), so cue measures record the
    caption schedule with a whole-track offset check against the imported duration.
    """
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    if state["current_state"] not in _STATES_ALLOWING_DUB:
        raise ConfigurationError(
            f"dub import expects the project at/through CAPTION_VALIDATION, "
            f"is at {state['current_state']}"
        )
    if not _dub_enabled(state, language):
        raise DubbingError(f"track {language!r} is not dub_enabled (caption-only)")

    src = Path(from_path)
    if not src.is_absolute():
        src = paths.directory / src
    if not src.is_file():
        raise DubbingError(f"dub WAV not found: {src}")

    doc = load_captions(root, project_id, language)
    cues = doc["cues"]
    if not cues:
        raise DubbingError(f"captions for {language!r} have no cues")

    dub_path = paths.directory / dub_relpath(language)
    bars = _audio_quality_bars(root)
    with project_lock(paths.lock):
        dub_path.parent.mkdir(parents=True, exist_ok=True)
        # Normalize the imported track to the dub format + target loudness (idempotent).
        media_mod.loudnorm(src, dub_path, target_lufs=bars["target_lufs"],
                           sample_rate=media_mod.DUB_SAMPLE_RATE,
                           channels=media_mod.DUB_CHANNELS)

    imported_ms = media_mod.audio_duration_ms(dub_path)
    captions_span = cues[-1]["end_ms"] - cues[0]["start_ms"]
    # Distribute the whole-track offset proportionally across cues for a schedule estimate.
    cue_measures: list[dict[str, Any]] = []
    scale = (imported_ms / captions_span) if captions_span else 1.0
    origin = cues[0]["start_ms"]
    for cue in cues:
        r_start = origin + round((cue["start_ms"] - origin) * scale)
        r_end = origin + round((cue["end_ms"] - origin) * scale)
        cue_measures.append({
            "id": cue["id"],
            "caption_start_ms": cue["start_ms"],
            "caption_end_ms": cue["end_ms"],
            "rendered_start_ms": r_start,
            "rendered_end_ms": r_end,
            "natural_ms": r_end - r_start,
            "stretch_factor": round(scale, 4),
            "over_stretch_cap": scale > bars["max_time_stretch"],
        })

    artifact = artifacts_mod.register_artifact(
        root, project_id, dub_path, "dub-wav", "DUBBING", actor, language=language,
    )
    report = _build_sync_report(
        root, project_id, language, cue_measures, bars,
        provider="imported", model=None, dub_sha256=artifact["sha256"], actor=actor,
    )
    _set_track(root, project_id, language, stage=TRACK_STAGE_DUBBED,
               status="in_progress", actor=actor, notes="dub imported; sync measured")
    append_event(paths.events, project_id, "DUB_IMPORTED", actor, {
        "language": language, "dub_sha256": artifact["sha256"], "source": str(src),
    })
    advanced = _maybe_advance_top(root, project_id, actor, advance, "CAPTION_VALIDATION",
                                  "DUBBING", TRACK_STAGE_DUBBED,
                                  _dub_langs(load_json(paths.state)))
    result = {"dub": _rel(dub_path, paths), "artifact": artifact, "language": language,
              "sync": report["languages"][language], "advanced_to": advanced,
              "freeze_plan": None}
    if bars["freeze_frame_enabled"]:
        # An opaque imported track has no per-cue natural durations, so a meaningful per-cue
        # freeze plan can't be computed here — freeze-frame is real-TTS (`run_dub`) only.
        result["freeze_plan_skipped_reason"] = (
            "imported dub has no per-cue boundaries; freeze-frame planning applies to the "
            "TTS render path (dub run) only"
        )
    return result


# --- 3. sync report ----------------------------------------------------------

def _build_sync_report(
    root: Path,
    project_id: str,
    language: str,
    cue_measures: list[dict[str, Any]],
    bars: dict[str, Any],
    *,
    provider: str | None,
    model: str | None,
    dub_sha256: str | None,
    actor: str,
    freeze_plan: dict[str, list[dict[str, Any]]] | None = None,
    voice_gender: str | None = None,
    voice_source: str | None = None,
    still_image: bool = False,
) -> dict[str, Any]:
    """Merge one language's cue measures into audio/sync-report.json and register it.

    Drift is rendered_end - caption_end. Cumulative offset is reset to 0 at natural pauses so
    a single long cue cannot poison the whole track. Preserves other languages' blocks.

    Under freeze-frame policy (``freeze_plan`` given, a ``{"freezes", "trims"}`` dict) the picture
    is retimed to the audio at mux by holding the frame for each freeze and skipping source for each
    trim. Drift is then measured on the **post-adjust timeline**: a cue's residual is how far its
    rendered-audio start still sits from its caption start after the accumulated adjustment has
    shifted the picture, ``rendered_start - (caption_start + cum_freeze - cum_trim)``. In trim mode
    (Model A) every cue reads ~0 residual by construction; in hold-only mode (Model B) a residual
    remains where the audio *underruns* its slot (we never trim source frames there).
    ``over_stretch_cap`` is always False here because the gentle cap is the only fit applied.
    analyze_sync's blocker/major checks are unchanged code reading these truthful numbers.
    """
    paths = ProjectPaths(root, project_id).require()
    tol = bars["per_cue_drift_tolerance_ms"]
    cap = bars["max_time_stretch"]
    freeze_mode = freeze_plan is not None
    # Exclude tail entries: they reconcile the picture's *total length* to the audio (keyed by
    # at_ms for the mux/windows), not a cue-START boundary, and they reuse the last cue's id — so
    # counting them here would double-adjust that cue's residual. The mux (packaging) keys events by
    # at_ms and DOES apply them.
    frozen_ms_by_cue = {int(f["cue_id"]): int(f["freeze_ms"])
                        for f in (freeze_plan or {}).get("freezes", []) if not f.get("tail")}
    trim_ms_by_cue = {int(t["cue_id"]): int(t["trim_ms"])
                      for t in (freeze_plan or {}).get("trims", []) if not t.get("tail")}

    cues_out: list[dict[str, Any]] = []
    cumulative = 0
    max_abs = 0
    over_tol: list[int] = []
    over_cap: list[int] = []
    cumulative_freeze = 0
    cumulative_trim = 0
    # Reconstruct the cue list for pause detection from the caption schedule.
    schedule = [{"start_ms": m["caption_start_ms"], "end_ms": m["caption_end_ms"]}
                for m in cue_measures]
    for i, m in enumerate(cue_measures):
        freeze_planned = m["id"] in frozen_ms_by_cue
        trim_planned = m["id"] in trim_ms_by_cue
        if still_image:
            # Still-image track (rule 15): the picture is one fixed frame stretched over the whole
            # dub, so there is no moving picture for the audio to drift against — residual is 0 by
            # construction. Report it honestly rather than measuring a phantom audio-vs-picture gap.
            drift = 0
            cumulative = 0
            reset = False
            is_over_cap = False
        elif freeze_mode:
            # A freeze/trim on this cue is applied BEFORE it, shifting the picture; measure the
            # residual audio-vs-picture gap on the post-adjust timeline. No cumulative-reset game —
            # freezes/trims, not natural pauses, are what realign the picture here.
            cumulative_freeze += frozen_ms_by_cue.get(m["id"], 0)
            cumulative_trim += trim_ms_by_cue.get(m["id"], 0)
            net = cumulative_freeze - cumulative_trim
            drift = int(m["rendered_start_ms"]) - (int(m["caption_start_ms"]) + net)
            cumulative = drift
            reset = False
            is_over_cap = False
        else:
            # Legacy clamp-and-drift model: drift accumulates against the caption slot, reset at
            # natural pauses so one long cue can't poison the whole track.
            drift = int(m["rendered_end_ms"] - m["caption_end_ms"])
            reset = _natural_pause_before(schedule, i)
            if reset:
                cumulative = 0
            cumulative += drift
            is_over_cap = bool(m.get("over_stretch_cap"))
        max_abs = max(max_abs, abs(cumulative))
        is_over_tol = abs(cumulative) > tol
        if is_over_tol:
            over_tol.append(m["id"])
        if is_over_cap:
            over_cap.append(m["id"])
        cues_out.append({
            "id": m["id"],
            "caption_start_ms": m["caption_start_ms"],
            "caption_end_ms": m["caption_end_ms"],
            "rendered_start_ms": m["rendered_start_ms"],
            "rendered_end_ms": m["rendered_end_ms"],
            "drift_ms": drift,
            "stretch_factor": m["stretch_factor"],
            "over_tolerance": is_over_tol,
            "over_stretch_cap": is_over_cap,
            "reset_here": reset,
            "freeze_planned": freeze_planned,
            "freeze_ms": frozen_ms_by_cue.get(m["id"], 0),
            "trim_planned": trim_planned,
            "trim_ms": trim_ms_by_cue.get(m["id"], 0),
        })

    # Silent-span QA (rule 14): walk the rendered dub timeline and record pure-silence spans — the
    # leading gap before the first audible cue and every gap between consecutive audible renders. An
    # "audible" cue carries real audio: it has non-empty target_text OR is a keep-source splice.
    # Empty/whitespace-only cues render 1ms of silence, so they contribute to a gap, not audio. A
    # span bounded by a keep-source cue is EXEMPT — the reciter's own audio fills that window, so a
    # residual beside it is not a hole. Spans below a small floor are ignored (routine inter-cue
    # pauses). analyze_sync escalates a non-exempt span over `silent_span_max_ms` to a blocker.
    SILENT_SPAN_FLOOR_MS = 250
    audible = [
        m for m in cue_measures
        if m.get("keep_source") or int(m.get("natural_ms", 0)) > 1
    ]
    keep_bounds: list[tuple[int, int]] = [
        (int(m["rendered_start_ms"]), int(m["rendered_end_ms"]))
        for m in cue_measures if m.get("keep_source")
    ]

    def _touches_keep(start: int, end: int) -> bool:
        # Exempt a span that abuts (or overlaps) any keep-source cue's rendered window.
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
                "start_ms": gap_start, "end_ms": gap_end,
                "duration_ms": gap_end - gap_start,
            })
        prev_end = max(prev_end, int(m["rendered_end_ms"]))
    max_silent_span = max((s["duration_ms"] for s in silent_spans), default=0)

    lang_block = {
        "language": language,
        "provider": provider,
        "model": model,
        "voice_gender": voice_gender,
        "voice_source": voice_source,
        "dub_path": dub_relpath(language),
        "dub_sha256": dub_sha256,
        "target_lufs": bars["target_lufs"],
        "measured_lufs": None,
        "cumulative_offset_ms": int(cumulative),
        "max_abs_drift_ms": int(max_abs),
        "cues_over_tolerance": over_tol,
        "cues_over_stretch_cap": over_cap,
        "silent_spans_ms": silent_spans,
        "max_silent_span_ms": int(max_silent_span),
        "still_image": bool(still_image),
        "freeze_frame_enabled": bool(freeze_plan is not None),
        # Totals reflect the FULL plan including the tail entry (the picture-length reconciliation
        # the mux applies), even though the tail is excluded from the per-cue residual accounting
        # above — so the disclosed total matches the picture the mux actually builds.
        "total_freeze_ms": int(sum(int(f["freeze_ms"]) for f in (freeze_plan or {}).get("freezes", []))),
        "total_trim_ms": int(sum(int(t["trim_ms"]) for t in (freeze_plan or {}).get("trims", []))),
        "cues": cues_out,
    }

    report_path = paths.directory / sync_report_relpath()
    with project_lock(paths.lock):
        if report_path.is_file():
            report = load_json(report_path)
        else:
            report = {
                "schema_version": SCHEMA_VERSION,
                "project_id": project_id,
                "created_at": utc_now(),
                "created_by": actor,
                "quality_bars": bars,
                "languages": {},
            }
        report["languages"][language] = lang_block
        report["created_at"] = utc_now()
        report["created_by"] = actor
        report["quality_bars"] = bars
        require_valid(root, report, "audio-sync.schema.json")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(report_path, report)
    artifacts_mod.register_artifact(
        root, project_id, report_path, "sync-report", "AUDIO_SYNC_ADJUST", actor,
    )
    _ = cap  # cap already applied upstream; kept for clarity of the bars snapshot
    return report


def load_sync_report(root: Path, project_id: str) -> dict[str, Any]:
    paths = ProjectPaths(root, project_id).require()
    path = paths.directory / sync_report_relpath()
    if not path.is_file():
        raise ConfigurationError(
            f"no sync report at {sync_report_relpath()}; run `dub run`/`dub import` first"
        )
    return load_json(path)


def _write_freeze_plan(
    root: Path,
    project_id: str,
    language: str,
    freeze_plan: dict[str, list[dict[str, Any]]],
    bars: dict[str, Any],
    *,
    trim_mode: bool,
    dub_sha256: str | None,
    actor: str,
    source_artifact_id: str,
) -> str:
    """Persist a language's freeze plan to audio/freeze-plan.<lang>.json and register it.

    Written whenever freeze-frame policy is on (even with an empty plan — a valid, informative
    artifact). ``freeze_plan`` is the ``{"freezes", "trims"}`` dict from ``_plan_freezes``. In
    hold-only mode (``trim_mode`` False, Model B) ``trims`` is empty; in trim mode (Model A) it
    records where the picture is shortened so residual reaches 0. Registered as a `freeze-plan`
    artifact tracing to the dub-wav (rule 6), so mux/package resolve the ACTIVE plan the same way
    they resolve the active dub."""
    paths = ProjectPaths(root, project_id).require()
    freezes = freeze_plan["freezes"]
    trims = freeze_plan["trims"]
    doc = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "language": language,
        "dub_sha256": dub_sha256,
        "created_at": utc_now(),
        "created_by": actor,
        "mode": "trim" if trim_mode else "hold",
        "quality_bars": {
            "freeze_frame_enabled": bars["freeze_frame_enabled"],
            "freeze_stretch_cap": bars["freeze_stretch_cap"],
            "max_freeze_ms_per_cue": bars["max_freeze_ms_per_cue"],
            "freeze_trim_languages": bars["freeze_trim_languages"],
        },
        "total_freeze_ms": int(sum(f["freeze_ms"] for f in freezes)),
        "total_trim_ms": int(sum(t["trim_ms"] for t in trims)),
        "freezes": freezes,
        "trims": trims,
    }
    plan_path = paths.directory / freeze_plan_relpath(language)
    with project_lock(paths.lock):
        require_valid(root, doc, "freeze-plan.schema.json")
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(plan_path, doc)
    artifacts_mod.register_artifact(
        root, project_id, plan_path, "freeze-plan", TRACK_STAGE_DUBBED, actor,
        language=language, source_artifact_ids=[source_artifact_id],
    )
    return _rel(plan_path, paths)


def load_freeze_plan(root: Path, project_id: str, language: str) -> dict[str, Any] | None:
    paths = ProjectPaths(root, project_id).require()
    path = paths.directory / freeze_plan_relpath(language)
    return load_json(path) if path.is_file() else None


# --- 4. audio-sync QA (aggregate gate report) --------------------------------

def analyze_sync(lang_block: dict[str, Any], bars: dict[str, Any]) -> dict[str, Any]:
    """Deterministic per-language decision from a sync-report language block.

    FAIL  : cumulative offset breaches the ceiling (unrecoverable drift).
    COND. : some cues exceed per-cue tolerance, or a stretch was clamped over the cap.
    PASS  : everything within budget.
    """
    ceiling = bars["cumulative_drift_ceiling_ms"]
    findings: list[dict[str, Any]] = []
    lang = lang_block["language"]
    if abs(lang_block.get("max_abs_drift_ms", 0)) > ceiling:
        findings.append(_finding(
            "blocker", "audio-cumulative-drift",
            f"[{lang}] cumulative offset {lang_block['max_abs_drift_ms']}ms exceeds "
            f"ceiling {ceiling}ms — re-segment or re-time before the audio_qa gate.",
            language=lang,
        ))
    for cid in lang_block.get("cues_over_stretch_cap", []):
        findings.append(_finding(
            "major", "audio-stretch-over-cap",
            f"[{lang}] cue {cid} needed a tempo stretch beyond the cap "
            f"({bars['max_time_stretch']}x); clamped, so it will lag — tighten the "
            f"translation or split the source segment.",
            language=lang, cue_id=cid,
        ))
    for cid in lang_block.get("cues_over_tolerance", []):
        findings.append(_finding(
            "minor", "audio-drift-over-tolerance",
            f"[{lang}] cue {cid} drifts past per-cue tolerance "
            f"({bars['per_cue_drift_tolerance_ms']}ms).",
            language=lang, cue_id=cid,
        ))
    # Silent-span check (rule 14): a long stretch of pure silence in the dub means the source had
    # untranscribed/untranslated speech (or a hole in the timeline) — the dub sits dead there. Any
    # span over the bar is a BLOCKER → FAIL. Keep-source-audio windows are already excluded upstream
    # in _build_sync_report (the reciter's own voice fills them), so this never trips on them.
    silent_cap = bars["silent_span_max_ms"]
    for span in lang_block.get("silent_spans_ms", []):
        if span["duration_ms"] > silent_cap:
            findings.append(_finding(
                "blocker", "audio-silent-span-excessive",
                f"[{lang}] {span['duration_ms']}ms of silence "
                f"({span['start_ms']}–{span['end_ms']}ms) exceeds the {silent_cap}ms cap — the "
                f"dub is dead air here. Recover the missing speech into the transcript (or mark "
                f"the window keep-source-audio if it is untranslatable source audio).",
                language=lang,
            ))
    # Freeze-frame policy: over-slot cues are resolved by holding the picture at mux, not by
    # audio drift. Each is an informational `note` (PASS-preserving — _aggregate_decision only
    # escalates on blocker/major); an over-long freeze escalates to a `major` so a person sees it.
    if lang_block.get("freeze_frame_enabled"):
        cap_ms = bars["max_freeze_ms_per_cue"]
        for cue in lang_block.get("cues", []):
            if not cue.get("freeze_planned"):
                continue
            freeze_ms = int(cue.get("freeze_ms", 0))
            if freeze_ms > cap_ms:
                findings.append(_finding(
                    "major", "audio-freeze-excessive",
                    f"[{lang}] cue {cue['id']} needs a {freeze_ms}ms picture freeze, beyond the "
                    f"{cap_ms}ms per-cue cap — the frozen frame will linger; tighten the "
                    f"translation or split the source segment.",
                    language=lang, cue_id=cue["id"],
                ))
            else:
                findings.append(_finding(
                    "note", "audio-freeze-planned",
                    f"[{lang}] cue {cue['id']} audio runs long; picture will be frozen "
                    f"{freeze_ms}ms at mux to keep the dub near natural speed.",
                    language=lang, cue_id=cue["id"],
                ))
    if any(f["severity"] == "blocker" for f in findings):
        decision = "FAIL"
    elif any(f["severity"] == "major" for f in findings):
        decision = "CONDITIONAL_PASS"
    else:
        decision = "PASS"
    metrics = {
        "cumulative_offset_ms": lang_block.get("cumulative_offset_ms"),
        "max_abs_drift_ms": lang_block.get("max_abs_drift_ms"),
        "cues": len(lang_block.get("cues", [])),
        "cues_over_tolerance": len(lang_block.get("cues_over_tolerance", [])),
        "cues_over_stretch_cap": len(lang_block.get("cues_over_stretch_cap", [])),
        "freeze_frame_enabled": bool(lang_block.get("freeze_frame_enabled")),
        "total_freeze_ms": int(lang_block.get("total_freeze_ms", 0)),
        "cues_frozen": sum(1 for c in lang_block.get("cues", []) if c.get("freeze_planned")),
        "max_silent_span_ms": int(lang_block.get("max_silent_span_ms", 0)),
        "silent_spans": len(lang_block.get("silent_spans_ms", [])),
    }
    return {"decision": decision, "findings": findings, "metrics": metrics}


def _finding(severity: str, category: str, summary: str, *,
             language: str | None = None, cue_id: int | None = None) -> dict[str, Any]:
    finding: dict[str, Any] = {"severity": severity, "category": category, "summary": summary}
    if language is not None:
        finding["language"] = language
    if cue_id is not None:
        finding["cue_id"] = cue_id
    return finding


def run_audio_qa(root: Path, project_id: str, *, actor: str = "agent") -> dict[str, Any]:
    """Aggregate `audio-sync` gate report across every active dub-enabled track.

    Single file keyed by report type; PASS iff every dub track passes. The human `audio_qa`
    approval remains per-language. Advances each measured track to AUDIO_QA_GATE.
    """
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    bars = _audio_quality_bars(root)

    dub_langs = [lang for lang in _active_track_langs(state) if _dub_enabled(state, lang)]
    has_report = (paths.directory / sync_report_relpath()).is_file()
    report = load_sync_report(root, project_id) if has_report else {"languages": {}}

    findings: list[dict[str, Any]] = []
    per_lang: dict[str, Any] = {}
    decisions: list[str] = []
    for lang in dub_langs:
        block = report.get("languages", {}).get(lang)
        if not block:
            decisions.append("FAIL")
            findings.append(_finding(
                "blocker", "audio-missing-dub",
                f"[{lang}] dub_enabled track has no dub/sync yet — run `dub run`/`dub import`.",
                language=lang,
            ))
            continue
        analysis = analyze_sync(block, bars)
        decisions.append(analysis["decision"])
        findings += analysis["findings"]
        per_lang[lang] = analysis["metrics"]

    decision = _aggregate_decision(decisions)
    gate_path = _write_gate_report(
        root, project_id, "audio-sync", decision, findings,
        {"languages": per_lang, "quality_bars": bars}, actor,
    )
    append_event(paths.events, project_id, "AUDIO_SYNC_REPORTED", actor, {
        "decision": decision, "languages": dub_langs,
    })
    # Advance each measured track toward the gate (top-level unchanged; slowest-track rule
    # governs the AUDIO_SYNC_ADJUST -> AUDIO_QA_GATE top transition, done by the human/CLI).
    for lang in dub_langs:
        if report.get("languages", {}).get(lang):
            _set_track(root, project_id, lang, stage=TRACK_STAGE_SYNCED,
                       status="in_progress", actor=actor, notes="audio-sync reported")
    return {"decision": decision, "report": _rel(gate_path, paths),
            "languages": per_lang, "findings": findings}
