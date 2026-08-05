"""Per-language translation + caption orchestration (TRANSLATION -> CAPTION_VALIDATION).

Follows the framework's export/import contract (the same shape as transcript import and
publish-book's book-draft -> artifact-register): the CLI never calls an LLM. It

  1. export_worksheet  -> emits captions/<lang>.worksheet.json (source cues + injected
     glossary + empty target slots + flags for cues needing high-stakes editorial care).
  2. (the translation skill/agent fills target_text into the worksheet — Sonnet volume,
     Opus for flagged religious/political cues.)
  3. import_worksheet  -> validates the filled worksheet, builds canonical
     captions/captions.<lang>.json, registers it, and advances that language TRACK to
     TRANSLATION_QA_GATE. Lifecycle from here is per-target-language (ANALYSIS B2).
  4. build_captions    -> deterministic SRT/VTT from the canonical JSON.
  5. run_translation_qa / run_caption_validation -> AGGREGATE gate reports over all active
     tracks (a report is PASS only if every active track passes); approvals stay per-language.

Gate report filenames are keyed by REPORT TYPE ("translation-qa", "glossary", "caption"),
matching workflow_states.json.gate_reports — NOT by gate name. (This is the Phase-2 rule
that transition_blockers reads paths.gate_report(report_type).)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import artifacts as artifacts_mod
from . import captions as captions_mod
from . import glossary as glossary_mod
from .errors import ConfigurationError, TranslationError
from .events import append_event
from .paths import ProjectPaths
from .state import transition
from .transcript import load_transcript
from .transcript_qa import _SENSITIVE_TERMS
from .util import atomic_write_json, atomic_write_text, load_json, load_yaml, project_lock, utc_now
from .validation import require_valid

SCHEMA_VERSION = "1.0"

# Language tracks pass through these stages during Phase 3.
TRACK_STAGE_TRANSLATED = "TRANSLATION_QA_GATE"
TRACK_STAGE_CAPTIONED = "CAPTION_VALIDATION"


# --- helpers -----------------------------------------------------------------

def captions_filename(language: str) -> str:
    return f"captions.{language}.json"


def worksheet_filename(language: str) -> str:
    return f"{language}.worksheet.json"


def _rel(path: Path, paths: ProjectPaths) -> str:
    return path.resolve().relative_to(paths.directory.resolve()).as_posix()


def _project_glossary_id(paths: ProjectPaths) -> str | None:
    cfg = load_yaml(paths.config, {})
    gid = cfg.get("glossary_id")
    return str(gid) if gid else None


def _target_languages(state: dict[str, Any]) -> list[str]:
    return list(state.get("target_languages", []))


def _active_track_langs(state: dict[str, Any]) -> list[str]:
    tracks = state.get("language_tracks", {})
    return [lang for lang, t in tracks.items() if t.get("status") != "failed"]


def _translatable_track_langs(state: dict[str, Any]) -> list[str]:
    """Active tracks that actually undergo TRANSLATION — i.e. excluding the source-language
    track (marked ``skip_translation``), which gets verbatim captions instead of a translation.
    Used for the translation quorum + QA aggregation so the source track never stalls them."""
    tracks = state.get("language_tracks", {})
    return [lang for lang in _active_track_langs(state)
            if not tracks.get(lang, {}).get("skip_translation")]


def _flag_cue(text: str) -> list[str]:
    """Mark cues whose translation is high-stakes editorial (religious/political), so the
    worksheet steers the translator to a stronger model for them."""
    lowered = text.lower()
    flags: list[str] = []
    for category, terms in _SENSITIVE_TERMS.items():
        if any(t in lowered or t in text for t in terms):
            flags.append(f"editorial-{category}")
    return flags


def _set_track(root: Path, project_id: str, language: str, *, stage: str,
               status: str, actor: str, notes: str | None = None) -> None:
    """Update one language track through the CLI-owned state (schema-validated)."""
    paths = ProjectPaths(root, project_id).require()
    with project_lock(paths.lock):
        state = load_json(paths.state)
        tracks = state.setdefault("language_tracks", {})
        track = tracks.setdefault(language, {"stage": stage, "status": status, "updated_at": utc_now()})
        track["stage"] = stage
        track["status"] = status
        track["updated_at"] = utc_now()
        if notes is not None:
            track["notes"] = notes
        state["updated_at"] = utc_now()
        state["updated_by"] = actor
        require_valid(root, state, "state.schema.json")
        atomic_write_json(paths.state, state)


# --- 1. export ---------------------------------------------------------------

def export_worksheet(
    root: Path,
    project_id: str,
    language: str,
    *,
    actor: str = "agent",
) -> dict[str, Any]:
    """Emit a translation worksheet for one target language from the source transcript.

    The worksheet mirrors the transcript's cue timing (the contract) with empty
    ``target_text`` slots for the translator to fill, plus the injected glossary block and
    per-cue editorial flags. Guarded so the project is at/through TRANSLATION.
    """
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    if state["current_state"] not in {"TRANSLATION", "TRANSLATION_QA_GATE", "CAPTION_TIMING",
                                      "CAPTION_VALIDATION"}:
        raise ConfigurationError(
            f"translation export expects the project at/through TRANSLATION, "
            f"is at {state['current_state']}"
        )
    if language not in _target_languages(state):
        raise ConfigurationError(f"'{language}' is not a target language of {project_id}")

    transcript = load_transcript(root, project_id)  # source language
    source_language = transcript["language"]

    # Source language == target language: no translation needed. Instead of emitting an empty
    # worksheet for a human to "translate" source->source, write the canonical caption doc
    # directly with target_text == source_text verbatim, register it, and mark the track
    # captioned. Downstream (captions build, dubbing, packaging) then sees a normal,
    # already-"translated" track with no special-casing.
    if language == source_language:
        return _export_source_verbatim(root, project_id, paths, transcript, language, actor=actor)

    glossary_block = ""
    gid = _project_glossary_id(paths)
    if gid:
        glossary = glossary_mod.load_glossary(root, gid)
        glossary_block = glossary_mod.inject_prompt_block(glossary, language)

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
        "language": language,
        "source_language": source_language,
        "glossary_id": gid,
        "glossary_instructions": glossary_block,
        "instructions": (
            "Fill target_text for every cue. Keep meaning faithful to source_text; do not "
            "merge or split cues (timing is fixed). Use the glossary renderings; terms marked "
            "[MUST] are hard-gated. For cues flagged 'editorial-*', translate with extra care "
            "(these reference sensitive religious/political content)."
        ),
        "cues": cues,
    }
    dest = paths.captions_dir / worksheet_filename(language)
    with project_lock(paths.lock):
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(dest, worksheet)
    append_event(paths.events, project_id, "TRANSLATION_WORKSHEET_EXPORTED", actor, {
        "language": language, "cues": len(cues),
        "flagged": sum(1 for c in cues if c["flags"]),
    })
    return {"worksheet": _rel(dest, paths), "language": language, "cues": len(cues),
            "glossary_id": gid}


def _export_source_verbatim(
    root: Path, project_id: str, paths: ProjectPaths, transcript: dict[str, Any],
    language: str, *, actor: str,
) -> dict[str, Any]:
    """Write verbatim source-language captions (target_text == source_text) for the source
    track, register them, and advance the track straight to the captioned stage. This is the
    source-language skip of TRANSLATION — the track produces captions without a translation."""
    cues = [{
        "id": cue["id"],
        "start_ms": cue["start_ms"],
        "end_ms": cue["end_ms"],
        "source_text": cue["text"],
        "target_text": cue["text"],
    } for cue in transcript["cues"]]
    doc = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "language": language,
        "source_language": language,
        "script_class": captions_mod.script_class(language),
        "glossary_id": _project_glossary_id(paths),
        "engine": {"provider": "source-verbatim", "model": None, "version": None},
        "has_word_timing": False,
        "cues": cues,
        "created_at": utc_now(),
        "created_by": actor,
    }
    require_valid(root, doc, "captions.schema.json")
    dest = paths.captions_dir / captions_filename(language)
    with project_lock(paths.lock):
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(dest, doc)
    artifact = artifacts_mod.register_artifact(
        root, project_id, dest, "captions-json", "TRANSLATION", actor, language=language,
    )
    # The source track skips the TRANSLATION_QA_GATE stage entirely (nothing to review) and
    # goes straight to the captioned stage; it's excluded from the translation quorum.
    _set_track(root, project_id, language, stage=TRACK_STAGE_CAPTIONED,
               status="in_progress", actor=actor,
               notes="source language — verbatim captions, no translation")
    append_event(paths.events, project_id, "SOURCE_CAPTIONS_EXPORTED", actor, {
        "language": language, "cues": len(cues), "captions_sha256": artifact["sha256"],
    })
    return {
        "language": language,
        "cues": len(cues),
        "captions": _rel(dest, paths),
        "artifact": artifact,
        "source_language_skip": True,
        "note": "source language — verbatim captions written, no translation needed",
    }


# --- 3. import ---------------------------------------------------------------

def _build_caption_doc(project_id: str, worksheet: dict[str, Any], *, actor: str) -> dict[str, Any]:
    language = worksheet["language"]
    cues = []
    for wc in worksheet["cues"]:
        cue: dict[str, Any] = {
            "id": wc["id"],
            "start_ms": wc["start_ms"],
            "end_ms": wc["end_ms"],
            "source_text": wc["source_text"],
            "target_text": wc.get("target_text", ""),
        }
        for optional in ("translator_confidence", "back_translation", "flags", "lines"):
            if wc.get(optional) not in (None, [], ""):
                cue[optional] = wc[optional]
        cues.append(cue)
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "language": language,
        "source_language": worksheet["source_language"],
        "script_class": captions_mod.script_class(language),
        "glossary_id": worksheet.get("glossary_id"),
        "engine": {"provider": worksheet.get("translator", "agent"), "model": None, "version": None},
        "has_word_timing": False,
        "cues": cues,
        "created_at": utc_now(),
        "created_by": actor,
    }


def import_worksheet(
    root: Path,
    project_id: str,
    language: str,
    *,
    from_path: str | Path | None = None,
    actor: str = "agent",
    advance: bool = False,
) -> dict[str, Any]:
    """Ingest an agent-filled worksheet into canonical captions.<lang>.json + register it.

    Advances the language TRACK to TRANSLATION_QA_GATE. When ``advance`` and every active
    track has reached that stage, also advances the top-level state TRANSLATION ->
    TRANSLATION_QA_GATE (the slowest-track rule)."""
    paths = ProjectPaths(root, project_id).require()
    ws_path = Path(from_path) if from_path else (paths.captions_dir / worksheet_filename(language))
    if not ws_path.is_absolute():
        ws_path = paths.directory / ws_path
    if not ws_path.is_file():
        raise TranslationError(f"worksheet not found: {ws_path}")
    worksheet = load_json(ws_path)

    if worksheet.get("language") != language:
        raise TranslationError(
            f"worksheet language {worksheet.get('language')!r} != requested {language!r}"
        )
    missing = [c["id"] for c in worksheet.get("cues", []) if not str(c.get("target_text", "")).strip()]
    if missing:
        raise TranslationError(
            f"worksheet has {len(missing)} untranslated cue(s): "
            f"{missing[:10]}{'...' if len(missing) > 10 else ''}"
        )

    doc = _build_caption_doc(project_id, worksheet, actor=actor)
    require_valid(root, doc, "captions.schema.json")
    dest = paths.captions_dir / captions_filename(language)
    with project_lock(paths.lock):
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(dest, doc)
    artifact = artifacts_mod.register_artifact(
        root, project_id, dest, "captions-json", "TRANSLATION", actor, language=language,
    )
    _set_track(root, project_id, language, stage=TRACK_STAGE_TRANSLATED,
               status="in_progress", actor=actor, notes="translated; awaiting QA")
    append_event(paths.events, project_id, "TRANSLATION_IMPORTED", actor, {
        "language": language, "cues": len(doc["cues"]), "captions_sha256": artifact["sha256"],
    })

    state = load_json(paths.state)
    advanced = _maybe_advance_top(root, project_id, actor, advance, "TRANSLATION",
                                  "TRANSLATION_QA_GATE", TRACK_STAGE_TRANSLATED,
                                  quorum_langs=_translatable_track_langs(state))
    return {"captions": _rel(dest, paths), "artifact": artifact, "language": language,
            "cues": len(doc["cues"]), "advanced_to": advanced}


def _maybe_advance_top(root: Path, project_id: str, actor: str, advance: bool,
                       from_state: str, to_state: str, track_stage: str,
                       quorum_langs: list[str] | None = None) -> str:
    """Advance the top-level state only when requested AND every participating track has
    reached ``track_stage`` (slowest-track quorum). Otherwise leave the top-level state as is.

    ``quorum_langs`` restricts the quorum to the tracks that participate in this edge — e.g.
    the dubbing and mux edges count only dub-enabled tracks, so caption-only tracks (which
    legitimately skip those stages) do not stall the transition. Defaults to every active
    track when not given (the translation/caption edges, where all tracks participate)."""
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    if state["current_state"] != from_state:
        return state["current_state"]
    if not advance:
        return from_state
    tracks = state.get("language_tracks", {})
    quorum = quorum_langs if quorum_langs is not None else _active_track_langs(state)
    if all(tracks.get(lang, {}).get("stage") == track_stage for lang in quorum):
        transition(root, project_id, to_state, actor, reason="all active tracks reached " + track_stage)
        return to_state
    return from_state


# --- 4. build captions -------------------------------------------------------

def load_captions(root: Path, project_id: str, language: str) -> dict[str, Any]:
    paths = ProjectPaths(root, project_id).require()
    path = paths.captions_dir / captions_filename(language)
    if not path.is_file():
        raise ConfigurationError(
            f"no captions at {_rel(path, paths)}; run `translate import` first"
        )
    return load_json(path)


def build_captions(
    root: Path,
    project_id: str,
    language: str,
    *,
    formats: list[str] | None = None,
    actor: str = "agent",
) -> dict[str, Any]:
    """Render deterministic SRT/VTT from canonical captions and register each as an artifact.

    Re-running with the same canonical JSON produces byte-identical files (stable hashes)."""
    paths = ProjectPaths(root, project_id).require()
    doc = load_captions(root, project_id, language)
    formats = [f.lower() for f in (formats or ["srt", "vtt"])]
    outputs = []
    for fmt in formats:
        text = captions_mod.render(doc, fmt)
        dest = paths.captions_dir / f"captions.{language}.{fmt}"
        with project_lock(paths.lock):
            dest.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(dest, text)
        artifact = artifacts_mod.register_artifact(
            root, project_id, dest, f"captions-{fmt}", "CAPTION_TIMING", actor, language=language,
        )
        outputs.append({"format": fmt, "path": _rel(dest, paths), "sha256": artifact["sha256"]})
    _set_track(root, project_id, language, stage=TRACK_STAGE_CAPTIONED,
               status="in_progress", actor=actor, notes="captions rendered")
    append_event(paths.events, project_id, "CAPTIONS_RENDERED", actor, {
        "language": language, "formats": formats,
    })
    return {"language": language, "outputs": outputs}


# --- 5. aggregate gate reports ----------------------------------------------

def _active_captions(root: Path, project_id: str) -> list[dict[str, Any]]:
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    docs = []
    for lang in _active_track_langs(state):
        path = paths.captions_dir / captions_filename(lang)
        if path.is_file():
            docs.append(load_json(path))
    return docs


def _aggregate_decision(decisions: list[str]) -> str:
    if not decisions:
        return "FAIL"
    if "FAIL" in decisions:
        return "FAIL"
    if "CONDITIONAL_PASS" in decisions:
        return "CONDITIONAL_PASS"
    return "PASS"


def _write_gate_report(root: Path, project_id: str, report_type: str,
                       decision: str, findings: list[dict[str, Any]],
                       metrics: dict[str, Any], actor: str) -> Path:
    paths = ProjectPaths(root, project_id).require()
    report = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "report_type": report_type,
        "decision": decision,
        "metrics": metrics,
        "created_at": utc_now(),
        "created_by": actor,
        "findings": findings,
    }
    require_valid(root, report, "review.schema.json")
    gate_path = paths.gate_report(report_type)  # report-type-keyed filename (Phase-2 rule)
    with project_lock(paths.lock):
        gate_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(gate_path, report)
    return gate_path


def run_translation_qa(root: Path, project_id: str, *, actor: str = "agent") -> dict[str, Any]:
    """Aggregate translation-qa + glossary gate reports across all active language tracks.

    * glossary report: FAIL if any active track has a must_appear miss (blocker).
    * translation-qa report: back-translation round-trip signal (note-level) + carries
      the glossary blockers so the human sees them in one place.
    Both are single files keyed by report type; the human approval remains per-language.
    """
    paths = ProjectPaths(root, project_id).require()
    # Only translated tracks are subject to translation QA — the source-language track carries
    # verbatim captions (target == source by design), which would otherwise trip every
    # "untranslated-suspect"/"identical to source" check. Its readability is still checked in
    # run_caption_validation.
    translatable = set(_translatable_track_langs(load_json(paths.state)))
    docs = [d for d in _active_captions(root, project_id) if d["language"] in translatable]
    gid = _project_glossary_id(paths)
    glossary = glossary_mod.load_glossary(root, gid) if gid else None

    glossary_findings: list[dict[str, Any]] = []
    tqa_findings: list[dict[str, Any]] = []
    per_lang_metrics: dict[str, Any] = {}
    for doc in docs:
        lang = doc["language"]
        if glossary:
            g_find, g_metrics = glossary_mod.check_captions(glossary, doc)
            glossary_findings += g_find
            per_lang_metrics[lang] = g_metrics
        tqa_findings += _back_translation_findings(doc)

    glossary_decision = _aggregate_decision(
        ["FAIL" if any(f["severity"] == "blocker" for f in glossary_findings) else "PASS"]
    ) if glossary else "PASS"
    glossary_metrics = {"glossary_id": gid, "languages": per_lang_metrics} if glossary else \
        {"glossary_id": None, "note": "no glossary configured"}
    glossary_path = _write_gate_report(root, project_id, "glossary", glossary_decision,
                                       glossary_findings, glossary_metrics, actor)

    # translation-qa carries glossary blockers too, so the edge is PASS only when clean.
    tqa_all = tqa_findings + [f for f in glossary_findings if f["severity"] == "blocker"]
    tqa_decision = "FAIL" if any(f["severity"] == "blocker" for f in tqa_all) else \
        ("CONDITIONAL_PASS" if any(f["severity"] == "major" for f in tqa_all) else "PASS")
    tqa_path = _write_gate_report(root, project_id, "translation-qa", tqa_decision,
                                  tqa_all, {"languages": sorted(d["language"] for d in docs)}, actor)

    append_event(paths.events, project_id, "TRANSLATION_QA_REPORTED", actor, {
        "translation_qa_decision": tqa_decision, "glossary_decision": glossary_decision,
        "languages": sorted(d["language"] for d in docs),
    })
    return {
        "translation_qa": {"decision": tqa_decision, "report": _rel(tqa_path, paths)},
        "glossary": {"decision": glossary_decision, "report": _rel(glossary_path, paths)},
        "findings": tqa_all,
        "human_gate": "translation_qa still requires a per-language human approval",
    }


def _back_translation_findings(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Note-level signal where a supplied back-translation looks nothing like the source.

    We can't judge meaning deterministically, so this only flags empty target text and
    obviously-untranslated cues (target identical to source) for human attention."""
    findings: list[dict[str, Any]] = []
    lang = doc["language"]
    for cue in doc.get("cues", []):
        tgt = cue.get("target_text", "").strip()
        src = cue.get("source_text", "").strip()
        if not tgt:
            findings.append({"severity": "blocker", "category": "empty-translation",
                             "summary": f"cue {cue.get('id')} has no target text",
                             "language": lang, "cue_id": cue.get("id")})
        elif tgt == src and src:
            findings.append({"severity": "note", "category": "untranslated-suspect",
                             "summary": f"cue {cue.get('id')} target is identical to source",
                             "language": lang, "cue_id": cue.get("id"), "evidence": src[:120]})
    return findings


def run_caption_validation(root: Path, project_id: str, *, actor: str = "agent") -> dict[str, Any]:
    """Aggregate caption readability report across all active tracks (the `caption` gate)."""
    paths = ProjectPaths(root, project_id).require()
    docs = _active_captions(root, project_id)
    all_findings: list[dict[str, Any]] = []
    decisions: list[str] = []
    per_lang: dict[str, Any] = {}
    for doc in docs:
        result = captions_mod.validate_captions(doc)
        decisions.append(result["decision"])
        for f in result["findings"]:
            f.setdefault("language", doc["language"])
        all_findings += result["findings"]
        per_lang[doc["language"]] = result["metrics"]
    decision = _aggregate_decision(decisions)
    path = _write_gate_report(root, project_id, "caption", decision, all_findings,
                              {"languages": per_lang}, actor)
    append_event(paths.events, project_id, "CAPTION_VALIDATED", actor, {
        "decision": decision, "languages": sorted(d["language"] for d in docs),
    })
    return {"decision": decision, "report": _rel(path, paths), "findings": all_findings}
