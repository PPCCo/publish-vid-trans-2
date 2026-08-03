"""Translation glossary: load, inject into a worksheet, and hard-check captions.

Glossaries are channel/speaker-scoped and live at the repo root under ``glossary/<id>.json``
(NOT one global file — different speakers' canonical renderings collide otherwise). A
project opts in via ``project.yaml.glossary_id``; when that is null there is no glossary and
every check is a no-op PASS.

Enforcement (ANALYSIS B6, per the confirmed decision):
  * ``must_appear`` terms are HARD-gated — if the source term is used in a cue but no
    accepted target rendering appears anywhere in that language's captions, the glossary
    gate report FAILs (blocker finding).
  * All other term mismatches only WARN (minor/note) — they inform the human reviewer
    without blocking the deterministic gate.

This module never calls an LLM and never mutates state; it is pure I/O + string matching.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .util import load_json
from .validation import require_valid

SCHEMA_VERSION = "1.0"


def glossary_dir(root: Path) -> Path:
    return root / "glossary"


def glossary_path(root: Path, glossary_id: str) -> Path:
    return glossary_dir(root) / f"{glossary_id}.json"


def load_glossary(root: Path, glossary_id: str) -> dict[str, Any]:
    """Load and schema-validate a glossary by id. Raises if missing or invalid."""
    path = glossary_path(root, glossary_id)
    if not path.is_file():
        raise ConfigurationError(
            f"glossary '{glossary_id}' not found at {path.relative_to(root).as_posix()}"
        )
    doc = load_json(path)
    require_valid(root, doc, "glossary.schema.json")
    return doc


def _accepted_renderings(term: dict[str, Any], language: str) -> list[str]:
    """Canonical rendering + aliases for a term in one target language (may be empty)."""
    target = term.get("targets", {}).get(language)
    if not target:
        return []
    out = [target["rendering"], *target.get("aliases", [])]
    return [r for r in out if r]


def _source_forms(term: dict[str, Any]) -> list[str]:
    return [term["source"], *term.get("source_variants", [])]


def inject_prompt_block(glossary: dict[str, Any], language: str) -> str:
    """Render a deterministic, human/agent-readable term table for a translation worksheet.

    The translator (the skill/agent) reads this and is expected to use the given rendering.
    ``[MUST]`` marks hard-gated terms. Output is stable (glossary order preserved) so the
    worksheet hashes reproducibly.
    """
    lines = [
        f"# Glossary '{glossary['glossary_id']}' — required renderings for '{language}'",
        "# Use the target rendering shown. Terms marked [MUST] are hard-gated: the",
        "# canonical rendering (or an alias) must appear wherever the source term is used.",
        "",
    ]
    any_term = False
    for term in glossary.get("terms", []):
        renderings = _accepted_renderings(term, language)
        if not renderings:
            continue  # no rendering defined for this language -> nothing to instruct
        any_term = True
        tag = "[MUST] " if term.get("must_appear") else ""
        aliases = renderings[1:]
        alias_note = f"  (also acceptable: {', '.join(aliases)})" if aliases else ""
        lines.append(f"{tag}{term['source']} -> {renderings[0]}{alias_note}")
    if not any_term:
        lines.append(f"(no terms defined for language '{language}')")
    return "\n".join(lines) + "\n"


def check_captions(
    glossary: dict[str, Any],
    caption_doc: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Hard-check a captions doc against the glossary. Returns (findings, metrics).

    A term is *in scope* for a cue when any of its source forms appears in the cue's
    ``source_text``. For an in-scope ``must_appear`` term, at least one accepted target
    rendering must appear in that cue's ``target_text`` — otherwise a blocker finding.
    Non-required in-scope misses produce minor findings (warn only).
    """
    language = caption_doc["language"]
    cues = caption_doc.get("cues", [])
    findings: list[dict[str, Any]] = []
    required_total = 0
    required_hit = 0

    for term in glossary.get("terms", []):
        renderings = _accepted_renderings(term, language)
        if not renderings:
            continue
        source_forms = _source_forms(term)
        must = bool(term.get("must_appear"))
        for cue in cues:
            src = cue.get("source_text", "")
            if not any(form and form in src for form in source_forms):
                continue  # term not used in this cue
            tgt = cue.get("target_text", "")
            hit = any(r in tgt for r in renderings)
            if must:
                required_total += 1
                if hit:
                    required_hit += 1
                else:
                    findings.append({
                        "severity": "blocker",
                        "category": "glossary-missing-required",
                        "summary": (
                            f"cue {cue.get('id')}: required term {term['source']!r} present in "
                            f"source but its rendering {renderings[0]!r} is missing from the "
                            f"translation"
                        ),
                        "language": language,
                        "cue_id": cue.get("id"),
                        "timestamp_ms": cue.get("start_ms"),
                        "evidence": tgt[:160],
                    })
            elif not hit:
                findings.append({
                    "severity": "minor",
                    "category": "glossary-preferred-rendering",
                    "summary": (
                        f"cue {cue.get('id')}: source uses {term['source']!r}; preferred "
                        f"rendering {renderings[0]!r} not found (not hard-gated)"
                    ),
                    "language": language,
                    "cue_id": cue.get("id"),
                    "timestamp_ms": cue.get("start_ms"),
                    "evidence": tgt[:160],
                })

    metrics = {
        "required_occurrences": required_total,
        "required_hits": required_hit,
        "required_hit_rate": round(required_hit / required_total, 4) if required_total else 1.0,
        "preferred_misses": sum(1 for f in findings if f["severity"] == "minor"),
    }
    return findings, metrics
