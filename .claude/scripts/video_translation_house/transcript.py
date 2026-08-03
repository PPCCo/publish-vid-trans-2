"""Transcription orchestration: source audio -> canonical cue-timed transcript.

Flow (guarded to state TRANSCRIPTION):
  1. Resolve the source language (state.source_language, set at LANGUAGE_ID).
  2. Run the ASR adapter (subprocess; engines opt-in) OR accept an already-produced
     cue list (manual/pre-transcribed path — keeps the pipeline usable with no engine).
  3. Normalize + lightly re-segment into sentence-ish cues.
  4. Write transcript/source.<lang>.json (schema-validated before persist).
  5. Register it as a content-addressed artifact and emit TRANSCRIPTION_COMPLETED.
  6. Optionally advance TRANSCRIPTION -> TRANSCRIPT_QA_GATE.

The cue-level timing here is the contract every downstream stage (captions, dubbing)
consumes. Word-level timing is passed through when the engine produced it, but nothing
downstream may *require* it (ANALYSIS B3).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import artifacts as artifacts_mod
from .engines.asr import ASRResult, TranscriptCue, transcribe
from .errors import ConfigurationError
from .events import append_event
from .paths import ProjectPaths
from .state import transition
from .util import atomic_write_json, load_json, project_lock, utc_now
from .validation import require_valid

SCHEMA_VERSION = "1.0"

# Sentence-final punctuation across Latin, Arabic-script (؟ ۔), and CJK.
_SENTENCE_END = re.compile(r"[.!?۔؟。！？]+[\"'”』」）)]*\s*$")


def transcript_filename(language: str) -> str:
    return f"source.{language}.json"


def _source_language(paths: ProjectPaths) -> str:
    state = load_json(paths.state)
    lang = state.get("source_language")
    if not lang:
        raise ConfigurationError(
            "source_language is not set; run `langid set` (LANGUAGE_ID stage) first"
        )
    return str(lang)


def _resegment_cues(cues: list[TranscriptCue]) -> list[TranscriptCue]:
    """Merge trailing fragments so each cue ends on sentence-final punctuation where the
    engine over-split. Conservative: never crosses a >800ms pause, never merges across a
    high no_speech_prob boundary, and re-numbers ids + concatenates word lists."""
    if not cues:
        return cues
    merged: list[TranscriptCue] = []
    for cue in cues:
        if (
            merged
            and not _SENTENCE_END.search(merged[-1].text)
            and cue.start_ms - merged[-1].end_ms <= 800
            and (cue.no_speech_prob or 0.0) < 0.5
        ):
            prev = merged[-1]
            prev.text = (prev.text + " " + cue.text).strip()
            prev.end_ms = cue.end_ms
            if cue.words:
                prev.words = (prev.words or []) + cue.words
            if cue.no_speech_prob is not None:
                prev.no_speech_prob = max(prev.no_speech_prob or 0.0, cue.no_speech_prob)
        else:
            merged.append(cue)
    for new_id, cue in enumerate(merged):
        cue.id = new_id
    return merged


def build_transcript_doc(
    project_id: str,
    language: str,
    result: ASRResult,
    *,
    dialect: str | None = None,
    actor: str = "agent",
) -> dict[str, Any]:
    cues = _resegment_cues(list(result.cues))
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "language": language,
        "dialect": dialect,
        "engine": {
            "provider": result.provider,
            "model": result.model,
            "word_alignment": result.word_alignment,
            "version": None,
        },
        "duration_seconds": result.duration_seconds,
        "cues": [c.to_dict() for c in cues],
        "has_word_timing": result.has_word_timing,
        "created_at": utc_now(),
        "created_by": actor,
    }


def write_transcript(
    root: Path,
    project_id: str,
    doc: dict[str, Any],
    *,
    actor: str = "agent",
) -> dict[str, Any]:
    """Validate, persist, and register a transcript doc. Shared by the ASR path and any
    manual/pre-transcribed path (which builds the doc itself, no engine needed)."""
    paths = ProjectPaths(root, project_id).require()
    language = doc["language"]
    require_valid(root, doc, "transcript.schema.json")
    dest = paths.transcript_dir / transcript_filename(language)
    with project_lock(paths.lock):
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(dest, doc)
    artifact = artifacts_mod.register_artifact(
        root, project_id, dest, "transcript", "TRANSCRIPTION", actor, language=language,
    )
    append_event(paths.events, project_id, "TRANSCRIPTION_COMPLETED", actor, {
        "language": language,
        "provider": doc["engine"]["provider"],
        "cues": len(doc["cues"]),
        "has_word_timing": doc["has_word_timing"],
        "transcript_sha256": artifact["sha256"],
    })
    return {"transcript": _rel(dest, paths), "artifact": artifact, "cues": len(doc["cues"])}


def run_transcription(
    root: Path,
    project_id: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    word_timestamps: bool = True,
    actor: str = "agent",
    advance: bool = False,
) -> dict[str, Any]:
    """Transcribe the project's source audio and write the canonical transcript."""
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    if state["current_state"] != "TRANSCRIPTION":
        raise ConfigurationError(
            f"transcription expects state TRANSCRIPTION, project is at {state['current_state']}"
        )
    language = _source_language(paths)
    dialect = None
    meta_path = paths.source_dir / "metadata.json"
    if meta_path.exists():
        dialect = load_json(meta_path).get("dialect")

    audio = paths.source_dir / "audio.wav"
    if not audio.is_file():
        raise ConfigurationError(f"source audio missing: {_rel(audio, paths)} (run ingest first)")

    result = transcribe(
        audio, paths.transcript_dir / "engine",
        provider=provider, model=model, language=language, word_timestamps=word_timestamps,
    )
    doc = build_transcript_doc(project_id, language, result, dialect=dialect, actor=actor)
    written = write_transcript(root, project_id, doc, actor=actor)

    advanced_to = "TRANSCRIPTION"
    if advance:
        transition(root, project_id, "TRANSCRIPT_QA_GATE", actor, reason="transcription complete")
        advanced_to = "TRANSCRIPT_QA_GATE"

    return {
        "project_id": project_id,
        "language": language,
        "provider": result.provider,
        "has_word_timing": result.has_word_timing,
        **written,
        "advanced_to": advanced_to,
    }


def load_transcript(root: Path, project_id: str, language: str | None = None) -> dict[str, Any]:
    paths = ProjectPaths(root, project_id).require()
    if language is None:
        language = _source_language(paths)
    path = paths.transcript_dir / transcript_filename(language)
    if not path.is_file():
        raise ConfigurationError(f"no transcript at {_rel(path, paths)}; run `transcript run` first")
    return load_json(path)


def _rel(path: Path, paths: ProjectPaths) -> str:
    return path.resolve().relative_to(paths.directory.resolve()).as_posix()
