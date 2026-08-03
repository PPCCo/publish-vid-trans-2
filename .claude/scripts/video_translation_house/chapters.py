"""Titled breakpoints / video chapters via the worksheet export/import round-trip.

Task 1 of the Phase-6 extension. Same contract as translate.py: the CLI never calls an
LLM. It

  1. export_chapter_worksheet -> emits chapters/<lang>.chapters-worksheet.json (the full
     cue list + a heuristic candidate-boundary pre-fill at large inter-cue gaps + empty
     `chapters` slots + instructions). The generate-chapters skill/agent groups cues into
     3-12 titled chapters based on the caption text.
  2. (the agent fills `chapters` with {start_ms, title} rows.)
  3. import_chapter_worksheet -> validates (first chapter at 0, monotonic non-overlapping,
     within duration), writes canonical chapters/chapters.<lang>.json, registers it as
     `chapters@<lang>`.

render_youtube_description_timecodes() renders the block YouTube parses into chapter
markers (first line MUST be 00:00). Pure/deterministic; no network, no LLM, no ML import.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import artifacts as artifacts_mod
from .errors import ConfigurationError, DistributionError
from .events import append_event
from .paths import ProjectPaths
from .translate import _rel, load_captions
from .util import atomic_write_json, load_json, project_lock, utc_now

SCHEMA_VERSION = "1.0"

# States from which chapters may be prepared: the captions exist by CAPTION_VALIDATION, and
# chapters feed platform packaging, so allow anywhere from caption validation onward.
_STATES_ALLOWING_CHAPTERS = {
    "CAPTION_VALIDATION", "DUBBING", "AUDIO_SYNC_ADJUST", "AUDIO_QA_GATE", "VIDEO_MUX",
    "FINAL_QA_GATE", "PACKAGE", "READY_FOR_REVIEW", "PLATFORM_PACKAGING",
}

# A gap this large between the end of one cue and the start of the next is a natural
# candidate chapter boundary (a pause/scene change). Purely a hint for the worksheet.
_CANDIDATE_GAP_MS = 8000
# Don't suggest more candidate boundaries than this (keeps the pre-fill sane on long talks).
_MAX_CANDIDATES = 12


def worksheet_filename(language: str) -> str:
    return f"{language}.chapters-worksheet.json"


def chapters_filename(language: str) -> str:
    return f"chapters.{language}.json"


def _duration_ms(caption_doc: dict[str, Any]) -> int:
    cues = caption_doc.get("cues", [])
    return max((int(c["end_ms"]) for c in cues), default=0)


def _candidate_boundaries(caption_doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Heuristic candidate chapter starts at large inter-cue gaps, so the agent refines a
    pre-filled list rather than starting blank. Always includes 0. Deterministic."""
    cues = caption_doc.get("cues", [])
    candidates: list[dict[str, Any]] = []
    if cues:
        candidates.append({"start_ms": 0, "cue_id": cues[0].get("id"),
                           "first_cue_text": cues[0].get("target_text", "")[:80]})
    prev_end: int | None = None
    for cue in cues:
        start = int(cue["start_ms"])
        if prev_end is not None and (start - prev_end) >= _CANDIDATE_GAP_MS:
            candidates.append({"start_ms": start, "cue_id": cue.get("id"),
                               "first_cue_text": cue.get("target_text", "")[:80]})
        prev_end = int(cue["end_ms"])
    # Cap candidates (always keep the 0 boundary + the widest gaps is overkill; simple head).
    return candidates[:_MAX_CANDIDATES]


def export_chapter_worksheet(
    root: Path,
    project_id: str,
    language: str,
    *,
    actor: str = "agent",
) -> dict[str, Any]:
    """Emit a chapter worksheet for one language from its canonical captions.

    The worksheet carries the full cue list (id/start_ms/text) for context, a heuristic
    candidate-boundary pre-fill, and an empty `chapters` list for the agent to fill.
    """
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    if state["current_state"] not in _STATES_ALLOWING_CHAPTERS:
        raise ConfigurationError(
            f"chapters export expects the project at/through CAPTION_VALIDATION, "
            f"is at {state['current_state']}"
        )
    caption_doc = load_captions(root, project_id, language)  # raises if captions missing
    duration_ms = _duration_ms(caption_doc)
    cues = [
        {"id": c.get("id"), "start_ms": int(c["start_ms"]), "end_ms": int(c["end_ms"]),
         "text": c.get("target_text", "")}
        for c in caption_doc.get("cues", [])
    ]
    worksheet = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "language": language,
        "source_language": caption_doc.get("source_language"),
        "duration_ms": duration_ms,
        "instructions": (
            "Group the cues below into 3-12 titled chapters that reflect the video's "
            "structure. Fill the `chapters` list with {start_ms, title} rows. The FIRST "
            "chapter must start at 0. start_ms values must be strictly increasing, each "
            "aligned to a cue's start_ms, and all < duration_ms. Titles should be concise "
            "(<= ~40 chars), descriptive, and in the caption language. `candidate_boundaries` "
            "are automatic suggestions at long pauses — refine or replace them; they are hints, "
            "not requirements."
        ),
        "candidate_boundaries": _candidate_boundaries(caption_doc),
        "chapters": [],
        "cues": cues,
    }
    dest = paths.chapters_dir / worksheet_filename(language)
    with project_lock(paths.lock):
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(dest, worksheet)
    append_event(paths.events, project_id, "CHAPTERS_WORKSHEET_EXPORTED", actor, {
        "language": language, "cues": len(cues), "candidates": len(worksheet["candidate_boundaries"]),
    })
    return {"worksheet": _rel(dest, paths), "language": language, "cues": len(cues),
            "duration_ms": duration_ms}


def _validate_chapters(rows: list[dict[str, Any]], duration_ms: int) -> None:
    """Deterministic boundary validation (raises DistributionError on any breach)."""
    if not rows:
        raise DistributionError("worksheet has no chapters; fill at least one {start_ms, title}")
    if int(rows[0]["start_ms"]) != 0:
        raise DistributionError(
            f"first chapter must start at 0ms (got {rows[0]['start_ms']}ms)"
        )
    prev = -1
    for i, row in enumerate(rows):
        start = int(row["start_ms"])
        title = str(row.get("title", "")).strip()
        if not title:
            raise DistributionError(f"chapter {i} has an empty title")
        if start <= prev:
            raise DistributionError(
                f"chapter start_ms must be strictly increasing; chapter {i} start {start}ms "
                f"is not after the previous {prev}ms"
            )
        if duration_ms and start >= duration_ms:
            raise DistributionError(
                f"chapter {i} start {start}ms is at/after the video duration {duration_ms}ms"
            )
        prev = start


def import_chapter_worksheet(
    root: Path,
    project_id: str,
    language: str,
    *,
    from_path: str | Path | None = None,
    actor: str = "agent",
) -> dict[str, Any]:
    """Ingest an agent-filled chapter worksheet into canonical chapters/chapters.<lang>.json.

    Validates boundaries, writes the canonical doc, and registers `chapters@<lang>`. Does not
    advance any lifecycle state (chapters are an input to PLATFORM_PACKAGING, not a stage)."""
    paths = ProjectPaths(root, project_id).require()
    ws_path = Path(from_path) if from_path else (paths.chapters_dir / worksheet_filename(language))
    if not ws_path.is_absolute():
        ws_path = paths.directory / ws_path
    if not ws_path.is_file():
        raise DistributionError(f"chapter worksheet not found: {ws_path}")
    worksheet = load_json(ws_path)
    if worksheet.get("language") != language:
        raise DistributionError(
            f"worksheet language {worksheet.get('language')!r} != requested {language!r}"
        )
    duration_ms = int(worksheet.get("duration_ms", 0))
    rows = [{"start_ms": int(r["start_ms"]), "title": str(r["title"]).strip()}
            for r in worksheet.get("chapters", [])]
    _validate_chapters(rows, duration_ms)

    doc = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "language": language,
        "source_language": worksheet.get("source_language"),
        "duration_ms": duration_ms,
        "chapters": rows,
        "created_at": utc_now(),
        "created_by": actor,
    }
    from .validation import require_valid

    require_valid(root, doc, "chapters.schema.json")
    dest = paths.chapters_dir / chapters_filename(language)
    with project_lock(paths.lock):
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(dest, doc)
    artifact = artifacts_mod.register_artifact(
        root, project_id, dest, "chapters", "PLATFORM_PACKAGING", actor, language=language,
    )
    append_event(paths.events, project_id, "CHAPTERS_IMPORTED", actor, {
        "language": language, "chapters": len(rows), "chapters_sha256": artifact["sha256"],
    })
    return {"chapters": _rel(dest, paths), "artifact": artifact, "language": language,
            "count": len(rows)}


def load_chapters(root: Path, project_id: str, language: str) -> dict[str, Any] | None:
    """Return the canonical chapters doc for a language, or None if not produced."""
    paths = ProjectPaths(root, project_id).require()
    path = paths.chapters_dir / chapters_filename(language)
    if not path.is_file():
        return None
    return load_json(path)


def _fmt_timecode(ms: int) -> str:
    """YouTube chapter timecode: M:SS below an hour, H:MM:SS at/above (leading unit unpadded)."""
    total_seconds = max(0, int(ms)) // 1000
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def render_youtube_description_timecodes(chapters_doc: dict[str, Any]) -> str:
    """Render the `<timecode> <title>` block YouTube parses into chapters.

    The first line MUST be a 0:00 timecode for YouTube to recognize the block. Returns an
    empty string when there are no chapters."""
    rows = chapters_doc.get("chapters", []) if chapters_doc else []
    if not rows:
        return ""
    return "\n".join(f"{_fmt_timecode(int(r['start_ms']))} {r['title']}" for r in rows)
