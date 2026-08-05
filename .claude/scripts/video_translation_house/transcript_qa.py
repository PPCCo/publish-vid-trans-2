"""Deterministic transcription QA + gate-report writer.

This module does NOT approve the transcript — the transcript_qa gate is human-bound
(CLAUDE.md rule 2). What it does is produce two artifacts a human reviewer relies on:

  * transcript/qa-report.json  — full detail: every flagged cue, metrics, findings.
  * reviews/transcript-qa-gate-latest.json — a review-shaped report whose ``decision``
    (PASS / CONDITIONAL_PASS / FAIL) is read by state.transition_blockers for the
    TRANSCRIPT_QA_GATE -> SEGMENT_RESOLUTION edge (gate_reports: ["transcript-qa"]).

The automated decision is deliberately conservative:
  * blocker findings (timing corruption, empty transcript)   -> FAIL
  * major findings (large low-confidence share, long silence) -> CONDITIONAL_PASS
  * only notes/minors                                         -> PASS
A CONDITIONAL_PASS still requires the human gate approval before the edge opens; the
report just tells the reviewer where to look. Sensitive religious/political term hits
are emitted as *notes* — never auto-blockers — because suppressing them is an editorial
judgment reserved for a human, not the machine.

If an English review-gloss of the source transcript has been produced at this stage
(transcript/english-gloss.json — see english_gloss.py), its AI context/word-sense findings
are folded into this same report (advisory: a gloss blocker->FAIL, major->CONDITIONAL_PASS),
so the human sees them in the one report the gate reads. A project without a gloss behaves
exactly as before.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from .events import append_event
from .paths import ProjectPaths
from .transcript import (
    LONG_CUE_MS,
    MIN_CUES_PER_SECOND,
    REPETITION_RUN_MIN,
    _normalize_cue_text,
    load_transcript,
)
from .util import atomic_write_json, load_json, project_lock, utc_now
from .validation import require_valid

SCHEMA_VERSION = "1.0"

# Tunables (kept as module constants so they're auditable and testable).
LOW_CONFIDENCE_LOGPROB = -1.0      # avg_logprob below this = shaky recognition
HIGH_NO_SPEECH_PROB = 0.6          # cue is probably music/silence, not speech
LONG_SILENCE_GAP_MS = 5000         # inter-cue gap that may mean dropped speech
LOW_CONFIDENCE_SHARE_MAJOR = 0.20  # >20% shaky cues -> major finding
# LONG_CUE_MS / MIN_CUES_PER_SECOND / REPETITION_RUN_MIN are imported from transcript.py above
# so the QA gate and the transcription auto-retry ladder share ONE definition of "too coarse" /
# "repetition loop" (the ladder decides whether to retry; QA decides whether to block).

# Sensitive-term lexicon. Hits are NOTES for human editorial review (translation of
# religious/political references is high-stakes and context-dependent), never blockers.
# Kept small + transliterated; the human QA step is where nuance lives.
_SENSITIVE_TERMS = {
    "religious": [
        "allah", "quran", "qur'an", "prophet", "muhammad", "imam", "ayatollah",
        "jihad", "kafir", "shahid", "shariah", "sharia", "caliph", "ummah", "fatwa",
        "الله", "قرآن", "امام", "جهاد", "شهيد", "شریعت", "خدا", "پیامبر",
    ],
    "political": [
        "martyr", "regime", "revolution", "resistance", "zionist", "infidel",
        "great satan", "death to", "supreme leader", "occupation",
        "انقلاب", "رژیم", "مقاومت", "رهبر",
    ],
}


def _finding(severity: str, category: str, summary: str, **extra: Any) -> dict[str, Any]:
    f = {"severity": severity, "category": category, "summary": summary}
    f.update({k: v for k, v in extra.items() if v is not None})
    return f


def _timing_findings(cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    prev_end = 0
    for cue in cues:
        start, end = cue.get("start_ms", 0), cue.get("end_ms", 0)
        cid = cue.get("id")
        if end < start:
            findings.append(_finding(
                "blocker", "timing", f"cue {cid} ends before it starts ({start}->{end}ms)",
                cue_id=cid, timestamp_ms=start))
        elif end == start and cue.get("text", "").strip():
            findings.append(_finding(
                "minor", "timing", f"cue {cid} has zero duration", cue_id=cid, timestamp_ms=start))
        if start < prev_end:
            # No diarization is run (ANALYSIS.md B1/C7 — pyannote's HF-gated stack is out of
            # scope here), so cue-timing overlap is the only deterministic signal available
            # for "two people may be talking at once." Flag it for a human to listen to rather
            # than silently letting ASR's single-speaker assumption paper over cross-talk.
            findings.append(_finding(
                "minor", "possible-overlapping-speech",
                f"cue {cid} overlaps the previous cue by {prev_end - start}ms "
                "(no diarization run — may be simultaneous/cross-talk speech; human review recommended)",
                cue_id=cid, timestamp_ms=start))
        gap = start - prev_end
        if prev_end and gap >= LONG_SILENCE_GAP_MS:
            findings.append(_finding(
                "note", "silence", f"{gap}ms gap before cue {cid} (possible dropped speech / music)",
                cue_id=cid, timestamp_ms=prev_end))
        prev_end = max(prev_end, end)
    return findings


def _confidence_findings(cues: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    low = [c for c in cues if (c.get("confidence") is not None and c["confidence"] < LOW_CONFIDENCE_LOGPROB)]
    music = [c for c in cues if (c.get("no_speech_prob") or 0.0) >= HIGH_NO_SPEECH_PROB]
    share = (len(low) / len(cues)) if cues else 0.0
    for cue in low:
        findings.append(_finding(
            "minor", "confidence",
            f"cue {cue.get('id')} low ASR confidence (avg_logprob={cue['confidence']:.2f})",
            cue_id=cue.get("id"), timestamp_ms=cue.get("start_ms"),
            evidence=cue.get("text", "")[:120]))
    for cue in music:
        findings.append(_finding(
            "note", "non-speech",
            f"cue {cue.get('id')} likely music/silence (no_speech_prob={cue['no_speech_prob']:.2f})",
            cue_id=cue.get("id"), timestamp_ms=cue.get("start_ms")))
    if share >= LOW_CONFIDENCE_SHARE_MAJOR:
        findings.append(_finding(
            "major", "confidence",
            f"{share:.0%} of cues are low-confidence — recommend human re-listen before translation"))
    metrics = {
        "cue_count": len(cues),
        "low_confidence_cues": len(low),
        "low_confidence_share": round(share, 4),
        "non_speech_cues": len(music),
    }
    return findings, metrics


def _sensitive_findings(cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for cue in cues:
        text = cue.get("text", "")
        lowered = text.lower()
        for category, terms in _SENSITIVE_TERMS.items():
            hit = next((t for t in terms if t in lowered or t in text), None)
            if hit:
                findings.append(_finding(
                    "note", f"sensitive-{category}",
                    f"cue {cue.get('id')} references a sensitive {category} term ({hit!r}) — "
                    f"human editorial review of translation recommended",
                    cue_id=cue.get("id"), timestamp_ms=cue.get("start_ms"),
                    evidence=text[:160]))
                break  # one note per cue per pass is enough signal
    return findings


def _repetition_findings(cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Blocker on a run of >= REPETITION_RUN_MIN identical consecutive cue texts — the
    repetition-hallucination loop (decoder parroting its own output on hard audio). This is the
    check that would have caught the نصیحت loop that slipped through the timing/confidence checks."""
    findings: list[dict[str, Any]] = []
    run_text: str | None = None
    run_len = 0
    run_start: dict[str, Any] | None = None

    def flush() -> None:
        if run_len >= REPETITION_RUN_MIN and run_start is not None:
            findings.append(_finding(
                "blocker", "repetition",
                f"cue text repeats {run_len}x consecutively starting at cue "
                f"{run_start.get('id')} — likely a repetition-hallucination loop",
                cue_id=run_start.get("id"), timestamp_ms=run_start.get("start_ms"),
                evidence=(run_start.get("text", "") or "")[:120]))

    for cue in cues:
        norm = _normalize_cue_text(cue.get("text", ""))
        if norm and norm == run_text:
            run_len += 1
        else:
            flush()
            run_text, run_len, run_start = norm, 1, cue
    flush()
    return findings


def _granularity_findings(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Major finding when the transcript is too coarse for the audio length (fewer than
    ~MIN_CUES_PER_SECOND cues/second — e.g. word-timestamps off produced 9 giant cues), plus a
    note per individual over-long cue. Coarse output is usable-with-review, not corrupt, so it's
    CONDITIONAL_PASS (human decides), never an auto-blocker."""
    findings: list[dict[str, Any]] = []
    cues = doc.get("cues", []) or []
    duration = doc.get("duration_seconds")
    expected = duration * MIN_CUES_PER_SECOND if duration else None
    if expected is not None and len(cues) < expected:
        findings.append(_finding(
            "major", "granularity",
            f"only {len(cues)} cues for {duration:.0f}s of audio (expected ~{expected:.0f}) — "
            "transcript is too coarse for caption timing; human should re-run with finer settings"))
    long_cues = 0
    for cue in cues:
        span = int(cue.get("end_ms", 0)) - int(cue.get("start_ms", 0))
        if span > LONG_CUE_MS:
            long_cues += 1
            findings.append(_finding(
                "note", "granularity",
                f"cue {cue.get('id')} spans {span}ms (> {LONG_CUE_MS}ms) — likely a merge over a "
                "hallucinated/blank stretch",
                cue_id=cue.get("id"), timestamp_ms=cue.get("start_ms")))
    metrics = {
        "expected_min_cues": round(expected) if expected is not None else None,
        "over_long_cues": long_cues,
    }
    return findings, metrics


def _decide(findings: list[dict[str, Any]]) -> str:
    severities = {f["severity"] for f in findings}
    if "blocker" in severities:
        return "FAIL"
    if "major" in severities:
        return "CONDITIONAL_PASS"
    return "PASS"


def analyze_transcript(doc: dict[str, Any]) -> dict[str, Any]:
    """Pure function: transcript doc -> {decision, findings, metrics}. No I/O; testable."""
    cues = doc.get("cues", [])
    findings: list[dict[str, Any]] = []
    if not cues:
        findings.append(_finding("blocker", "empty", "transcript has no cues"))
        return {"decision": "FAIL", "findings": findings, "metrics": {"cue_count": 0}}
    findings += _timing_findings(cues)
    conf_findings, metrics = _confidence_findings(cues)
    findings += conf_findings
    findings += _repetition_findings(cues)
    gran_findings, gran_metrics = _granularity_findings(doc)
    findings += gran_findings
    metrics.update({k: v for k, v in gran_metrics.items() if v is not None})
    findings += _sensitive_findings(cues)
    return {"decision": _decide(findings), "findings": findings, "metrics": metrics}


def run_transcript_qa(
    root: Path,
    project_id: str,
    *,
    language: str | None = None,
    actor: str = "agent",
) -> dict[str, Any]:
    """Analyze the current transcript and write both the detail report and the gate report."""
    paths = ProjectPaths(root, project_id).require()
    doc = load_transcript(root, project_id, language)
    lang = doc["language"]

    from . import artifacts as artifacts_mod

    transcript_hash = None
    active = load_json(paths.state).get("active_artifacts", {})
    transcript_hash = active.get(f"transcript@{lang}") or active.get("transcript")

    analysis = analyze_transcript(doc)

    # Fold in the English review-gloss context findings if a gloss has been produced at this
    # stage (advisory — same report the gate reads). A missing gloss simply contributes no
    # findings, so a project without one behaves exactly as before (backward compatible).
    from . import english_gloss as gloss_mod

    gloss_doc = gloss_mod.load_gloss(root, project_id)
    if gloss_doc is not None:
        gloss_findings = gloss_mod.analyze_gloss_context(doc, gloss_doc)
        analysis["findings"] = analysis["findings"] + gloss_findings
        analysis["decision"] = _decide(analysis["findings"])
        analysis["metrics"]["gloss_present"] = True
        analysis["metrics"]["gloss_context_findings"] = len(gloss_findings)
    else:
        analysis["metrics"]["gloss_present"] = False

    now = utc_now()
    hashes = [transcript_hash] if transcript_hash else []

    detail = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "report_type": "transcript-qa",
        "language": lang,
        "artifact_hashes": hashes,
        "decision": analysis["decision"],
        "metrics": analysis["metrics"],
        "created_at": now,
        "created_by": actor,
        "findings": analysis["findings"],
    }
    require_valid(root, detail, "review.schema.json")

    with project_lock(paths.lock):
        paths.transcript_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(paths.transcript_dir / "qa-report.json", detail)
        # The state machine's gate_reports map keys this edge by REPORT TYPE
        # ("transcript-qa"), and transition_blockers reads paths.gate_report(report_type).
        # The file MUST therefore be named for the report type, not the gate name
        # (which is "transcript_qa" with an underscore) — otherwise the blocker can't
        # find it and reports the report as missing.
        gate_path = paths.gate_report("transcript-qa")
        gate_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(gate_path, detail)

    # Register the QA report as an artifact for provenance (best-effort; report exists on disk).
    with contextlib.suppress(Exception):
        artifacts_mod.register_artifact(
            root, project_id, paths.transcript_dir / "qa-report.json",
            "transcript-qa-report", "TRANSCRIPT_QA_GATE", actor, language=lang, active=False,
        )

    append_event(paths.events, project_id, "TRANSCRIPT_QA_REPORTED", actor, {
        "language": lang,
        "decision": analysis["decision"],
        "findings": len(analysis["findings"]),
        "blockers": sum(1 for f in analysis["findings"] if f["severity"] == "blocker"),
    })
    return {
        "project_id": project_id,
        "language": lang,
        "decision": analysis["decision"],
        "findings": analysis["findings"],
        "metrics": analysis["metrics"],
        "gate_report": _rel(gate_path, paths),
        "human_gate": "transcript_qa still requires a human approval before the edge opens",
    }


def _rel(path: Path, paths: ProjectPaths) -> str:
    return path.resolve().relative_to(paths.directory.resolve()).as_posix()
