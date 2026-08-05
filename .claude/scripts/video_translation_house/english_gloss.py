"""English review-gloss of the source transcript, produced AT the TRANSCRIPT_QA_GATE stage.

This is a *review aid*, not a deliverable. Before a human validates the source-language
transcript, the framework produces a faithful English gloss of the source speech and an AI
context/word-sense pass runs over it (auto-fixing the gloss, flagging source problems). The
gloss lets a reviewer who can't read the source language validate meaning, and its context
findings are folded into the existing ``transcript-qa`` gate report (advisory: blocker->FAIL,
major->CONDITIONAL_PASS) so no new required gate report or state-machine edge is introduced.

Follows the same export/import contract as translate.py (the CLI never calls an LLM):

  1. export_gloss_worksheet -> emits transcript/english-gloss.worksheet.json (source cues +
     empty English ``target_text`` slots + editorial flags). Idempotent: never clobbers a
     partially-filled worksheet.
  2. (the english-context-check skill/agent fills target_text with an English gloss and adds a
     per-cue context_note / context-mismatch flag where a word's sense is wrong or ambiguous —
     fixing the ENGLISH gloss in place, never the source transcript.)
  3. import_gloss_worksheet -> validates + writes canonical transcript/english-gloss.json
     (captions.schema.json shape) and registers it as an ``english-gloss`` artifact
     (active=False — a review aid, not a track deliverable). Does NOT advance any language
     track or the top-level state.

Distinct from the later per-language TRANSLATION stage: this gloss is registered as
``english-gloss`` (not ``captions-json``), lives under transcript/, and never feeds the en
caption translation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import artifacts as artifacts_mod
from . import captions as captions_mod
from .errors import ConfigurationError, TranslationError
from .events import append_event
from .paths import ProjectPaths
from .transcript import load_transcript
from .translate import _flag_cue
from .util import atomic_write_json, load_json, project_lock, utc_now
from .validation import require_valid

SCHEMA_VERSION = "1.0"
GLOSS_LANGUAGE = "en"
GLOSS_WORKSHEET = "english-gloss.worksheet.json"
GLOSS_DOC = "english-gloss.json"

# The stage at which the gloss is produced (state machine name).
GLOSS_STAGE = "TRANSCRIPT_QA_GATE"

# Tunables for the deterministic context checks (kept as constants so they're auditable +
# testable, matching the transcript_qa.py idiom). The *semantic* judgement is the agent's job
# (recorded as context_note / context-mismatch flags on cues); these thresholds only turn
# mechanical signals into review findings.
LENGTH_RATIO_LOW = 0.25   # English < 1/4 the source length (chars) => possible dropped meaning
LENGTH_RATIO_HIGH = 4.0   # English > 4x the source length       => possible over-expansion
MIN_LEN_FOR_RATIO = 12    # ignore tiny cues where a ratio is meaningless

# Agent-authored flag that marks a cue whose SOURCE transcript looks wrong in context (an ASR
# error the gloss can't faithfully render). Surfaced as a major finding for the human.
CONTEXT_MISMATCH_FLAG = "context-mismatch"


def _rel(path: Path, paths: ProjectPaths) -> str:
    return path.resolve().relative_to(paths.directory.resolve()).as_posix()


def _gloss_worksheet_path(paths: ProjectPaths) -> Path:
    return paths.transcript_dir / GLOSS_WORKSHEET


def _gloss_doc_path(paths: ProjectPaths) -> Path:
    return paths.transcript_dir / GLOSS_DOC


def gloss_exists(root: Path, project_id: str) -> bool:
    """Best-effort: does a canonical English gloss doc exist for this project?"""
    paths = ProjectPaths(root, project_id).require()
    return _gloss_doc_path(paths).exists()


def load_gloss(root: Path, project_id: str) -> dict[str, Any] | None:
    """Load the canonical English gloss doc, or None if it hasn't been produced yet."""
    paths = ProjectPaths(root, project_id).require()
    dest = _gloss_doc_path(paths)
    if not dest.exists():
        return None
    return load_json(dest)


# --- 1. export ---------------------------------------------------------------

def export_gloss_worksheet(root: Path, project_id: str, *, actor: str = "agent") -> dict[str, Any]:
    """Emit an English review-gloss worksheet from the source transcript.

    Mirrors translate.export_worksheet's cue shape but is scoped to the source->English review
    gloss. Idempotent: if the worksheet already exists it is returned untouched (so a partially
    filled worksheet is never clobbered by a re-run of the skill)."""
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    if state["current_state"] != GLOSS_STAGE:
        raise ConfigurationError(
            f"english gloss export expects the project at {GLOSS_STAGE}, "
            f"is at {state['current_state']}"
        )

    dest = _gloss_worksheet_path(paths)
    if dest.exists():
        return {
            "worksheet": _rel(dest, paths),
            "language": GLOSS_LANGUAGE,
            "already_exists": True,
            "note": "worksheet already present — not clobbered (fill it, then english-import)",
        }

    transcript = load_transcript(root, project_id)  # source language
    source_language = transcript["language"]

    cues = []
    for cue in transcript["cues"]:
        cues.append({
            "id": cue["id"],
            "start_ms": cue["start_ms"],
            "end_ms": cue["end_ms"],
            "source_text": cue["text"],
            "target_text": "",
            "flags": _flag_cue(cue["text"]),
        })

    worksheet = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "language": GLOSS_LANGUAGE,
        "source_language": source_language,
        "instructions": (
            "This is a faithful ENGLISH GLOSS of the source speech for MEANING REVIEW at the "
            "transcript QA gate — not a broadcast translation. Fill target_text for every cue "
            "with plain, meaning-first English (do not merge or split cues; timing is fixed). "
            "Then run a context/word-sense pass: read the whole gloss to establish the speech's "
            "context, and for each cue verify the English words make sense in that context. "
            "Where an English word choice is wrong/ambiguous, FIX target_text in place and add a "
            "short 'context_note'. Where the problem is in the SOURCE transcript (a likely ASR "
            f"error), do NOT edit the source — set flag '{CONTEXT_MISMATCH_FLAG}' and a "
            "'context_note' on that cue so it becomes a finding for the human. Give cues flagged "
            "'editorial-religious'/'editorial-political' extra care."
        ),
        "cues": cues,
    }
    with project_lock(paths.lock):
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(dest, worksheet)
    append_event(paths.events, project_id, "ENGLISH_GLOSS_WORKSHEET_EXPORTED", actor, {
        "source_language": source_language, "cues": len(cues),
        "flagged": sum(1 for c in cues if c["flags"]),
    })
    return {"worksheet": _rel(dest, paths), "language": GLOSS_LANGUAGE, "cues": len(cues),
            "source_language": source_language}


# --- 2. import ---------------------------------------------------------------

def _build_gloss_doc(project_id: str, worksheet: dict[str, Any], *, actor: str) -> dict[str, Any]:
    cues = []
    for wc in worksheet["cues"]:
        cue: dict[str, Any] = {
            "id": wc["id"],
            "start_ms": wc["start_ms"],
            "end_ms": wc["end_ms"],
            "source_text": wc["source_text"],
            "target_text": wc.get("target_text", ""),
        }
        for optional in ("context_note", "flags"):
            if wc.get(optional) not in (None, [], ""):
                cue[optional] = wc[optional]
        cues.append(cue)
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "language": GLOSS_LANGUAGE,
        "source_language": worksheet["source_language"],
        "script_class": captions_mod.script_class(GLOSS_LANGUAGE),
        "engine": {"provider": worksheet.get("translator", "agent"), "model": None, "version": None},
        "has_word_timing": False,
        "cues": cues,
        "created_at": utc_now(),
        "created_by": actor,
    }


def import_gloss_worksheet(
    root: Path,
    project_id: str,
    *,
    from_path: str | Path | None = None,
    actor: str = "agent",
) -> dict[str, Any]:
    """Validate a filled English-gloss worksheet and write the canonical gloss doc.

    Does NOT advance any language track or the top-level state — the gloss is a review aid the
    transcript-qa gate report folds in, not a pipeline deliverable."""
    paths = ProjectPaths(root, project_id).require()
    src = paths.resolve_inside(from_path) if from_path else _gloss_worksheet_path(paths)
    if not src.exists():
        raise ConfigurationError(f"english-gloss worksheet not found: {src}")
    worksheet = load_json(src)
    if worksheet.get("language") != GLOSS_LANGUAGE:
        raise TranslationError(
            f"gloss worksheet language is {worksheet.get('language')!r}, expected {GLOSS_LANGUAGE!r}")

    missing = [c.get("id") for c in worksheet.get("cues", []) if not str(c.get("target_text", "")).strip()]
    if missing:
        raise TranslationError(
            f"english-gloss worksheet has {len(missing)} cue(s) with empty target_text: "
            f"{missing[:10]}{'…' if len(missing) > 10 else ''}")

    doc = _build_gloss_doc(project_id, worksheet, actor=actor)
    require_valid(root, doc, "captions.schema.json")

    dest = _gloss_doc_path(paths)
    with project_lock(paths.lock):
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(dest, doc)
    artifact = artifacts_mod.register_artifact(
        root, project_id, dest, "english-gloss", GLOSS_STAGE, actor,
        language=GLOSS_LANGUAGE, active=False,
    )
    append_event(paths.events, project_id, "ENGLISH_GLOSS_IMPORTED", actor, {
        "source_language": doc["source_language"], "cues": len(doc["cues"]),
        "gloss_sha256": artifact["sha256"],
        "context_mismatches": sum(1 for c in doc["cues"]
                                  if CONTEXT_MISMATCH_FLAG in (c.get("flags") or [])),
    })
    return {
        "language": GLOSS_LANGUAGE,
        "source_language": doc["source_language"],
        "cues": len(doc["cues"]),
        "gloss": _rel(dest, paths),
        "artifact": artifact,
    }


# --- 3. deterministic context checks (pure) ----------------------------------

def _finding(severity: str, category: str, summary: str, **extra: Any) -> dict[str, Any]:
    f = {"severity": severity, "category": category, "summary": summary}
    f.update({k: v for k, v in extra.items() if v is not None})
    return f


def analyze_gloss_context(source_doc: dict[str, Any], gloss_doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure function: (source transcript doc, english gloss doc) -> list of review findings.

    Deterministic checks only. The real word-sense/context judgement is the agent's job and is
    carried on the gloss cues as ``context_note`` strings and ``context-mismatch`` flags; this
    function turns those (plus a couple of mechanical signals) into review-schema findings that
    transcript_qa folds into the transcript-qa gate report. All categories are prefixed
    ``english-context`` so they stay distinguishable from source-transcript findings."""
    findings: list[dict[str, Any]] = []
    cues = gloss_doc.get("cues", []) or []
    if not cues:
        findings.append(_finding(
            "blocker", "english-context/empty-gloss",
            "english gloss has no cues"))
        return findings

    for cue in cues:
        cid = cue.get("id")
        ts = cue.get("start_ms")
        source_text = (cue.get("source_text") or "").strip()
        target_text = (cue.get("target_text") or "").strip()
        flags = cue.get("flags") or []
        note = cue.get("context_note")

        if not target_text:
            findings.append(_finding(
                "blocker", "english-context/empty-gloss",
                f"cue {cid} has no English gloss", cue_id=cid, timestamp_ms=ts))
            continue

        # Agent flagged a source-transcript problem it couldn't faithfully render.
        if CONTEXT_MISMATCH_FLAG in flags:
            findings.append(_finding(
                "major", "english-context/context-mismatch",
                f"cue {cid} flagged by the context pass: the source text does not make sense in "
                "context (likely an ASR error) — human should verify the source transcript",
                cue_id=cid, timestamp_ms=ts,
                detail=note, evidence=source_text[:160]))
        elif note:
            # A context decision the agent recorded — surface it so the human sees the reasoning.
            findings.append(_finding(
                "note", "english-context/context-note",
                f"cue {cid}: {note}", cue_id=cid, timestamp_ms=ts))

        # English identical to a non-trivial source string => almost certainly not glossed.
        if source_text and target_text == source_text and len(source_text) >= MIN_LEN_FOR_RATIO:
            findings.append(_finding(
                "note", "english-context/untranslated-suspect",
                f"cue {cid} English gloss is identical to the source text — verify it was glossed",
                cue_id=cid, timestamp_ms=ts, evidence=target_text[:120]))

        # Gross length mismatch => possible dropped or over-expanded meaning.
        if len(source_text) >= MIN_LEN_FOR_RATIO and source_text:
            ratio = len(target_text) / len(source_text)
            if ratio <= LENGTH_RATIO_LOW or ratio >= LENGTH_RATIO_HIGH:
                findings.append(_finding(
                    "note", "english-context/length-outlier",
                    f"cue {cid} English length is {ratio:.1f}x the source — possible dropped or "
                    "over-expanded meaning; human should compare",
                    cue_id=cid, timestamp_ms=ts))
    return findings
