"""Deterministic pipeline driver for the scripted / manual route (TASK 1).

``project autopilot <id>`` runs the pure-script steps of the transcribe->translate->dub
pipeline back-to-back WITHOUT Claude, stopping the moment a human gate (or a genuine decision
point / blocker) is reached. It is the engine behind ``run_pipeline.sh`` and the
``/process-manual`` skill: the whole point is to keep tokens out of Claude for the mechanical
stages, and only spend them on QA + gate review afterward.

Design guarantees (mirror the standing rules):
  * It advances ONLY on non-gate, forward edges — exactly what ``state.plan()`` marks PROCEED.
    It NEVER grants an approval, NEVER crosses a human gate, NEVER sets rights (rules 2/13).
  * Every mutation still flows through the existing CLI-owned module functions (rule 1) — this
    module is an orchestrator, not a new state writer.
  * Translation worksheet-fill (the one genuinely non-deterministic step) is done by the MT
    engine adapter when ``mt=True``; without it, autopilot stops at TRANSLATION and hands off
    (fill via the Claude route, or install an MT engine and re-run with --mt).
  * ``--dry-run`` plans the step sequence without executing or mutating anything.

The loop: read ``state.plan()`` -> if TERMINAL/STOP_AT_GATE/BLOCKED, stop with a reason ->
else run the handler for the current state, then loop. A handler that cannot proceed
deterministically (ambiguous source language, MT needed but off) returns a HALT so the loop
stops cleanly with an actionable message instead of forcing a wrong decision.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import state as state_mod
from .paths import ProjectPaths
from .util import load_json


# --- per-state handlers ------------------------------------------------------
# Each handler runs the deterministic step(s) for one state and returns a dict:
#   {"ran": [<verb strings actually executed>], "halt": <str|None>}
# A non-null "halt" stops the loop with that human-facing reason (no error). Handlers never
# advance the top state directly unless via a module fn's own --advance/quorum logic; where an
# edge has no self-advancing verb (caption/sync), the loop calls state.transition afterward.


def _active_tracks(state: dict[str, Any]) -> dict[str, Any]:
    return {lang: t for lang, t in state.get("language_tracks", {}).items()
            if t.get("status") != "failed"}


def _dub_langs(state: dict[str, Any]) -> list[str]:
    return [lang for lang, t in _active_tracks(state).items() if t.get("dub_enabled")]


def _h_ingest(root, pid, *, mt, dry, actor, log) -> dict[str, Any]:
    from . import ingest as ingest_mod
    if dry:
        log(f"ingest run {pid} (download + extract WAV + advance)")
        return {"ran": ["ingest run"], "halt": None}
    ingest_mod.ingest_project(root, pid, actor=actor, advance=True)
    return {"ran": ["ingest run"], "halt": None}


def _h_language_id(root, pid, *, mt, dry, actor, log) -> dict[str, Any]:
    """Source language is a human decision when detection is unavailable. If init already
    recorded it, advance; otherwise HALT and let the human confirm via /new-video or langid."""
    paths = ProjectPaths(root, pid).require()
    state = load_json(paths.state)
    src = state.get("source_language")
    if src:
        if dry:
            log(f"project transition {pid} --to TRANSCRIPTION (source_language={src} already set)")
        else:
            state_mod.transition(root, pid, "TRANSCRIPTION", actor,
                                 reason="source language already confirmed")
        return {"ran": ["transition->TRANSCRIPTION"], "halt": None}
    return {"ran": [], "halt": (
        "source language not confirmed — this is a human decision (ASR auto-detect is "
        "unavailable). Confirm it with `langid set` / the /new-video flow, then re-run.")}


def _h_transcription(root, pid, *, mt, dry, actor, log) -> dict[str, Any]:
    from . import transcript as transcript_mod
    if dry:
        log(f"transcript run {pid} --advance (ASR via mlx_whisper, deterministic)")
        return {"ran": ["transcript run"], "halt": None}
    transcript_mod.run_transcription(root, pid, actor=actor, advance=True)
    return {"ran": ["transcript run"], "halt": None}


def _h_transcript_qa_gate(root, pid, *, mt, dry, actor, log) -> dict[str, Any]:
    """Prepare the transcript gate packet deterministically: the transcript-qa report, and (in
    --mt mode) the English review-gloss draft. Then the loop STOPS at this human gate. The
    AI context-check pass on the gloss (rule 11) is deferred to Claude's QA — it's advisory."""
    from . import transcript_qa
    from . import english_gloss
    ran: list[str] = []
    if dry:
        log(f"transcript qa {pid} (deterministic QA report)")
        ran.append("transcript qa")
        if mt:
            log(f"transcript english-export {pid} + MT-fill + english-import (gloss draft)")
            ran.append("english-gloss (mt)")
        return {"ran": ran, "halt": None}
    transcript_qa.run_transcript_qa(root, pid, actor=actor)
    ran.append("transcript qa")
    if mt:
        try:
            _mt_fill_gloss(root, pid, actor=actor, log=log)
            ran.append("english-gloss (mt)")
        except Exception as exc:  # noqa: BLE001 - gloss is advisory; never block the run
            log(f"  (english gloss MT-fill skipped: {exc})")
    return {"ran": ran, "halt": None}


def _h_segment_resolution(root, pid, *, mt, dry, actor, log) -> dict[str, Any]:
    from . import segments as segments_mod
    if dry:
        log(f"segments resolve {pid} --advance")
        return {"ran": ["segments resolve"], "halt": None}
    segments_mod.run_resolve(root, pid, actor=actor, advance=True)
    return {"ran": ["segments resolve"], "halt": None}


def _h_translation(root, pid, *, mt, dry, actor, log) -> dict[str, Any]:
    """Per translatable track: export -> (MT-fill if --mt) -> import(advance). Also runs the
    deterministic translate qa aggregate. Without --mt, HALT: worksheet-fill needs the Claude
    route or an MT engine. auto_translate + en are all MT-filled here; en still hits its human
    gate afterward, so MT is just the cheap draft."""
    from . import translate as translate_mod
    paths = ProjectPaths(root, pid).require()
    state = load_json(paths.state)
    tracks = _active_tracks(state)
    # Source-language track: export writes verbatim captions (no fill). Everything else needs a
    # translation. If MT is off and any non-source track is unfilled, we can't proceed alone.
    to_fill = [lang for lang, t in tracks.items() if not t.get("skip_translation")]
    if not mt and to_fill:
        return {"ran": [], "halt": (
            "translation worksheet-fill needs an author. Re-run with --mt (MT engine) to fill "
            "without Claude, or use the Claude-driven create-closed-captions skill. Tracks "
            f"awaiting translation: {', '.join(sorted(to_fill))}.")}
    ran: list[str] = []
    for lang, t in tracks.items():
        if dry:
            if t.get("skip_translation"):
                log(f"translate export {pid} --language {lang} (source verbatim captions)")
            else:
                log(f"translate export {pid} --language {lang} + MT-fill + translate import --advance")
            ran.append(f"translate {lang}")
            continue
        if t.get("skip_translation"):
            translate_mod.export_worksheet(root, pid, lang, actor=actor)  # writes verbatim caps
            ran.append(f"translate export {lang} (verbatim)")
            continue
        translate_mod.export_worksheet(root, pid, lang, actor=actor)
        translate_mod.machine_fill_worksheet(root, pid, lang, actor=actor)
        translate_mod.import_worksheet(root, pid, lang, actor=actor, advance=True)
        ran.append(f"translate {lang} (mt)")
    if not dry:
        translate_mod.run_translation_qa(root, pid, actor=actor)
    ran.append("translate qa")
    return {"ran": ran, "halt": None}


def _h_caption_timing(root, pid, *, mt, dry, actor, log) -> dict[str, Any]:
    """Render SRT/VTT for every active track, then advance to CAPTION_VALIDATION."""
    from . import translate as translate_mod
    paths = ProjectPaths(root, pid).require()
    state = load_json(paths.state)
    ran: list[str] = []
    for lang in _active_tracks(state):
        if dry:
            log(f"captions build {pid} --language {lang}")
        else:
            translate_mod.build_captions(root, pid, lang, actor=actor)
        ran.append(f"captions build {lang}")
    if dry:
        log(f"project transition {pid} --to CAPTION_VALIDATION")
    else:
        state_mod.transition(root, pid, "CAPTION_VALIDATION", actor, reason="captions rendered")
    return {"ran": ran + ["transition->CAPTION_VALIDATION"], "halt": None}


def _h_caption_validation(root, pid, *, mt, dry, actor, log) -> dict[str, Any]:
    """Write the deterministic caption gate report, then advance to DUBBING (or PACKAGE for a
    caption-only project). The caption report must be PASS for the edge; if it isn't, plan()
    will report BLOCKED on the next loop and stop with the report decision."""
    from . import translate as translate_mod
    paths = ProjectPaths(root, pid).require()
    state = load_json(paths.state)
    has_dub = bool(_dub_langs(state))
    target = "DUBBING" if has_dub else "PACKAGE"
    if dry:
        log(f"captions validate {pid} (caption gate report)")
        log(f"project transition {pid} --to {target}")
        return {"ran": ["captions validate", f"transition->{target}"], "halt": None,
                "next": target}
    translate_mod.run_caption_validation(root, pid, actor=actor)
    # Only transition if the edge is now permitted (caption report PASS); else let the loop's
    # next plan() surface BLOCKED with the report decision.
    blockers = state_mod.transition_blockers(root, pid, "CAPTION_VALIDATION", target)
    if blockers:
        return {"ran": ["captions validate"], "halt": (
            f"caption validation did not clear the edge to {target}: {'; '.join(blockers)}")}
    state_mod.transition(root, pid, target, actor, reason="captions validated")
    return {"ran": ["captions validate", f"transition->{target}"], "halt": None}


def _h_dubbing(root, pid, *, mt, dry, actor, log) -> dict[str, Any]:
    """Synthesize a dub for every dub-enabled track (TTS subprocess, deterministic), advancing
    each to AUDIO_SYNC_ADJUST via the run_dub --advance quorum."""
    from . import dubbing as dubbing_mod
    paths = ProjectPaths(root, pid).require()
    state = load_json(paths.state)
    dubs = _dub_langs(state)
    ran: list[str] = []
    for lang in dubs:
        if dry:
            log(f"dub run {pid} --language {lang} --advance (neutral voice; TTS)")
        else:
            dubbing_mod.run_dub(root, pid, lang, actor=actor, advance=True)
        ran.append(f"dub run {lang}")
    return {"ran": ran, "halt": None}


def _h_audio_sync_adjust(root, pid, *, mt, dry, actor, log) -> dict[str, Any]:
    """Aggregate the audio-sync gate report across dub tracks (advances each to AUDIO_QA_GATE).
    Then the loop STOPS at the human audio_qa gate."""
    from . import dubbing as dubbing_mod
    if dry:
        log(f"dub qa {pid} (audio-sync gate report)")
        return {"ran": ["dub qa"], "halt": None}
    dubbing_mod.run_audio_qa(root, pid, actor=actor)
    return {"ran": ["dub qa"], "halt": None}


def _h_video_mux(root, pid, *, mt, dry, actor, log) -> dict[str, Any]:
    """Mux each dub-enabled track's dub over the source (advances to FINAL_QA_GATE via quorum)."""
    from . import packaging as packaging_mod
    paths = ProjectPaths(root, pid).require()
    state = load_json(paths.state)
    ran: list[str] = []
    for lang in _dub_langs(state):
        if dry:
            log(f"package mux {pid} --language {lang} --advance")
        else:
            packaging_mod.run_mux(root, pid, lang, actor=actor, advance=True)
        ran.append(f"package mux {lang}")
    return {"ran": ran, "halt": None}


def _h_final_qa_gate(root, pid, *, mt, dry, actor, log) -> dict[str, Any]:
    """Write the deterministic final gate report; then the loop STOPS at the human final_qa gate."""
    from . import packaging as packaging_mod
    if dry:
        log(f"package final-qa {pid} (final gate report)")
        return {"ran": ["package final-qa"], "halt": None}
    packaging_mod.run_final_qa(root, pid, actor=actor)
    return {"ran": ["package final-qa"], "halt": None}


def _h_package(root, pid, *, mt, dry, actor, log) -> dict[str, Any]:
    """Assemble deliverable packages. PACKAGE->READY_FOR_REVIEW is rights-gated (not a human
    APPROVAL but a human rights decision), so run_package assembles and the loop will then
    surface BLOCKED until a human sets rights (rule 4)."""
    from . import packaging as packaging_mod
    if dry:
        log(f"package build {pid} --advance (rights-gated at READY_FOR_REVIEW)")
        return {"ran": ["package build"], "halt": None}
    packaging_mod.run_package(root, pid, actor=actor, advance=True)
    return {"ran": ["package build"], "halt": None}


# state -> handler. Only the transcribe->translate->dub->package span; the Phase-6 distribution
# tail is intentionally NOT automated here (it is outward-facing and human-gated end to end).
_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "INGEST": _h_ingest,
    "LANGUAGE_ID": _h_language_id,
    "TRANSCRIPTION": _h_transcription,
    "TRANSCRIPT_QA_GATE": _h_transcript_qa_gate,
    "SEGMENT_RESOLUTION": _h_segment_resolution,
    "TRANSLATION": _h_translation,
    "CAPTION_TIMING": _h_caption_timing,
    "CAPTION_VALIDATION": _h_caption_validation,
    "DUBBING": _h_dubbing,
    "AUDIO_SYNC_ADJUST": _h_audio_sync_adjust,
    "VIDEO_MUX": _h_video_mux,
    "FINAL_QA_GATE": _h_final_qa_gate,
    "PACKAGE": _h_package,
}

# States at which, once reached, autopilot has prepared the gate packet and must stop for the
# human. (plan() will also say STOP_AT_GATE at the *edge*; these let a handler run first.)
_GATE_STATES = {"TRANSCRIPT_QA_GATE", "TRANSLATION_QA_GATE", "AUDIO_QA_GATE", "FINAL_QA_GATE"}


def _mt_fill_gloss(root: Path, project_id: str, *, actor: str, log) -> None:
    """Produce the English review-gloss draft deterministically via MT: export -> fill each
    empty cue -> import. Advisory (rule 11); the AI context pass is Claude's later QA."""
    from . import english_gloss
    from .engines import mt as mt_mod
    paths = ProjectPaths(root, project_id).require()
    english_gloss.export_gloss_worksheet(root, project_id, actor=actor)
    ws_path = english_gloss._gloss_worksheet_path(paths)  # canonical worksheet location
    worksheet = load_json(ws_path)
    src = worksheet.get("source_language")
    for cue in worksheet.get("cues", []):
        if str(cue.get("target_text", "")).strip():
            continue
        cue["target_text"] = mt_mod.translate_text(
            str(cue.get("source_text", "")), src, "en", root=root)
    from .util import atomic_write_json, project_lock
    with project_lock(paths.lock):
        atomic_write_json(ws_path, worksheet)
    english_gloss.import_gloss_worksheet(root, project_id, from_path=str(ws_path), actor=actor)


def autopilot(
    root: Path,
    project_id: str,
    *,
    mt: bool = False,
    until: str | None = None,
    dry_run: bool = False,
    max_steps: int = 200,
    actor: str = "agent",
) -> dict[str, Any]:
    """Run deterministic pipeline steps until a human gate / blocker / terminal state / ``until``.

    Returns a summary: the ordered steps run, the final state, and a human-facing ``stop_reason``
    plus ``stop_kind`` (GATE / BLOCKED / TERMINAL / HALT / UNTIL). Never grants approvals or
    crosses a human gate — it only runs PROCEED-eligible non-gate steps."""
    ProjectPaths(root, project_id).require()
    steps: list[str] = []
    narration: list[str] = []

    def log(msg: str) -> None:
        narration.append(msg)

    if dry_run:
        return _dry_walk(root, project_id, mt=mt, actor=actor, until=until, max_steps=max_steps)

    for _ in range(max_steps):
        p = state_mod.plan(root, project_id)
        current = p["current_state"]
        action = p["autonomy_action"]

        if until and current == until:
            return _summary(steps, narration, current, "UNTIL",
                            f"reached requested stop state {until}", p)
        if action == "TERMINAL":
            return _summary(steps, narration, current, "TERMINAL",
                            f"project is at terminal state {current}", p)
        if action == "STOP_AT_GATE":
            gate = p.get("required_gate")
            # Run the handler first (if any) so the gate packet/report is freshly prepared, then
            # stop for the human. Handlers for gate states only produce reports — never cross.
            handler = _HANDLERS.get(current)
            if handler and current in _GATE_STATES:
                res = handler(root, project_id, mt=mt, dry=dry_run, actor=actor, log=log)
                steps += res["ran"]
                if res["halt"]:
                    return _summary(steps, narration, current, "HALT", res["halt"], p)
            return _summary(steps, narration, current, "GATE",
                            f"human gate '{gate}' — {current}. Prepare packet + disclose/confirm "
                            f"(rule 13), then approve to advance to {p.get('recommended_target')}.",
                            p)
        if action == "BLOCKED":
            reason = "; ".join(
                b for c in p.get("candidates", []) for b in c.get("blockers", [])
            ) or "no permitted forward transition"
            return _summary(steps, narration, current, "BLOCKED", reason, p)

        # PROCEED: run the deterministic handler for this state.
        handler = _HANDLERS.get(current)
        if handler is None:
            return _summary(steps, narration, current, "HALT",
                            f"no autopilot handler for state {current} (outside the automated "
                            "transcribe->translate->dub->package span)", p)
        try:
            res = handler(root, project_id, mt=mt, dry=False, actor=actor, log=log)
        except Exception as exc:  # noqa: BLE001 — surface engine/config failures as a clean HALT
            # A deterministic step failed (e.g. a TTS/MT engine misconfig). Stop cleanly with the
            # error as the halt reason rather than crashing — no state was advanced across a gate,
            # and the operator sees exactly which step to fix, then re-runs (idempotent).
            return _summary(steps, narration, current, "HALT",
                            f"step for {current} failed: {type(exc).__name__}: {exc}", p)
        steps += res["ran"]
        if res["halt"]:
            return _summary(steps, narration, current, "HALT", res["halt"], p)

    return _summary(steps, narration, state_mod.plan(root, project_id)["current_state"],
                    "HALT", f"max_steps ({max_steps}) reached", None)


def _forward_target(cfg: dict[str, Any], current: str) -> str | None:
    """The single forward edge for a state: the first transition that isn't a control edge
    (PAUSED/CANCELLED/ERROR) or a backward edge. The workflow graph lists the forward target
    first, so this is deterministic and matches how plan() picks the PROCEED/gate target."""
    control = {"PAUSED", "CANCELLED", "ERROR"}
    order = list(cfg.get("transitions", {}))
    here = order.index(current) if current in order else -1
    for tgt in cfg.get("transitions", {}).get(current, []):
        if tgt in control:
            continue
        ti = order.index(tgt) if tgt in order else -1
        if ti > here:  # forward only (skip retry/back edges)
            return tgt
    return None


def _dry_walk(root: Path, project_id: str, *, mt: bool, actor: str,
              until: str | None, max_steps: int) -> dict[str, Any]:
    """Preview the FULL scripted run without mutating state. Walks the static forward chain from
    the live current_state, listing each state's deterministic steps, and stops where a real run
    would: the first human gate edge, a terminal state, ``until``, or a state a handler can't do
    deterministically (e.g. TRANSLATION with --mt off). Read-only — no disk writes, no plan()
    dependence on advancing state."""
    cfg = state_mod.workflow_config(root)
    gate_edges = cfg.get("gate_edges", {})
    terminal = set(cfg.get("terminal_states", []))
    # Gate FROM-states: the state you sit in *at* a human gate (the edge out of it is gated).
    # Reaching one means a real run would stop for the human, even before running its handler.
    gate_states = {edge.split("->", 1)[0]: gate for edge, gate in gate_edges.items()}
    paths = ProjectPaths(root, project_id).require()
    live = load_json(paths.state)
    current = live["current_state"]
    steps: list[str] = []
    narration: list[str] = []

    def log(msg: str) -> None:
        narration.append(msg)

    for _ in range(max_steps):
        if until and current == until:
            return _dry_summary(steps, narration, current, "UNTIL",
                                f"would reach requested stop state {until}")
        if current in terminal:
            return _dry_summary(steps, narration, current, "TERMINAL",
                                f"terminal state {current}")
        handler = _HANDLERS.get(current)
        if handler is not None:
            # A gate-state handler prepares the packet (report/gloss); run it, then stop.
            res = handler(root, project_id, mt=mt, dry=True, actor=actor, log=log)
            steps += res["ran"]
            if res["halt"]:
                return _dry_summary(steps, narration, current, "HALT", res["halt"])
            hint = res.get("next")
        else:
            hint = None
        if current in gate_states:
            return _dry_summary(steps, narration, current, "GATE",
                                f"would stop at human gate '{gate_states[current]}' "
                                f"(edge out of {current})")
        if handler is None:
            return _dry_summary(steps, narration, current, "HALT",
                                f"no autopilot handler for state {current}")
        nxt = hint or _forward_target(cfg, current)
        if nxt is None:
            return _dry_summary(steps, narration, current, "HALT",
                                f"no forward transition from {current}")
        current = nxt

    return _dry_summary(steps, narration, current, "HALT", f"max_steps ({max_steps}) reached")


def _dry_summary(steps, narration, current, kind, reason) -> dict[str, Any]:
    return {
        "current_state": current,
        "stop_kind": kind,
        "stop_reason": reason,
        "dry_run": True,
        "steps_run": steps,
        "narration": narration,
    }


def _summary(steps, narration, current, kind, reason, plan) -> dict[str, Any]:
    out: dict[str, Any] = {
        "current_state": current,
        "stop_kind": kind,
        "stop_reason": reason,
        "steps_run": steps,
        "narration": narration,
    }
    if plan is not None:
        out["recommended_target"] = plan.get("recommended_target")
        if plan.get("required_gate"):
            out["required_gate"] = plan["required_gate"]
    return out
