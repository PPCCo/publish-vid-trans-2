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
from . import obs
from .engines.asr import ASRResult, TranscriptCue, transcribe
from .errors import ConfigurationError
from .events import append_event
from .paths import ProjectPaths
from .state import transition
from .util import atomic_write_json, load_json, load_tools_config, project_lock, utc_now
from .validation import require_valid

SCHEMA_VERSION = "1.0"

# Sentence-final punctuation across Latin, Arabic-script (؟ ۔), and CJK.
_SENTENCE_END = re.compile(r"[.!?۔؟。！？]+[\"'”』」）)]*\s*$")

# --- Transcript quality thresholds (auditable module constants; mirror transcript_qa.py) ---
# A speech lecture averages well under 10s per natural sentence-cue; a transcript with fewer
# than ~1 cue / 10s of audio is almost always coarse (word-timestamps off, or a merge over a
# blank/hallucinated stretch) and unusable for caption timing downstream.
MIN_CUES_PER_SECOND = 1 / 10
# A single cue longer than this is almost always a merge across a hallucinated/blank stretch.
LONG_CUE_MS = 30000
# The same normalized cue text repeating this many times in a row is a repetition-hallucination
# loop (the decoder parroting its own output on hard audio — e.g. recitation/music).
REPETITION_RUN_MIN = 3


def _normalize_cue_text(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def validate_transcript_quality(doc: dict[str, Any]) -> list[str]:
    """Pure, testable transcript sanity check. Returns a list of human-readable problem
    strings (empty = clean). This is the auto-retry ladder's accept/reject signal — it must
    catch the failure modes a deterministic decode can still produce (repetition loops, coarse
    output, giant merged cues), NOT editorial judgment (that's the human QA gate)."""
    problems: list[str] = []
    cues = doc.get("cues", []) or []
    if not cues:
        return ["transcript has no cues"]

    duration = doc.get("duration_seconds")
    if duration:
        expected = duration * MIN_CUES_PER_SECOND
        if len(cues) < expected:
            problems.append(
                f"too few cues for {duration:.0f}s of audio: {len(cues)} < ~{expected:.0f} "
                f"expected ({MIN_CUES_PER_SECOND * 1000:.0f} per 10s) — likely coarse/merged output"
            )

    # Over-long cues (each one is a signal on its own).
    for cue in cues:
        span = int(cue.get("end_ms", 0)) - int(cue.get("start_ms", 0))
        if span > LONG_CUE_MS:
            problems.append(
                f"cue {cue.get('id')} spans {span}ms (> {LONG_CUE_MS}ms) — likely a merge over a "
                "hallucinated/blank stretch"
            )

    # Consecutive-repetition run (the نصیحت-style loop).
    run_text: str | None = None
    run_len = 0
    run_start_id: Any = None
    for cue in cues:
        norm = _normalize_cue_text(cue.get("text", ""))
        if norm and norm == run_text:
            run_len += 1
        else:
            if run_len >= REPETITION_RUN_MIN:
                problems.append(
                    f"repetition-hallucination run: cue text repeats {run_len}x consecutively "
                    f"starting at cue {run_start_id} ({run_text[:40]!r})"
                )
            run_text, run_len, run_start_id = norm, 1, cue.get("id")
    if run_len >= REPETITION_RUN_MIN:
        problems.append(
            f"repetition-hallucination run: cue text repeats {run_len}x consecutively "
            f"starting at cue {run_start_id} ({run_text[:40]!r})"
        )
    return problems


# Escalating decode-configuration ladder for the auto-retry mode. Each rung uses *different*
# decode settings so a retry can actually change the result (mlx-whisper decodes
# deterministically at temperature 0, so re-running identical settings would reproduce the same
# transcript). Rung 1 is the normal default; 2-3 progressively fight repetition-hallucination
# loops; 4 falls back to coarse segment-only timing as a last resort.
_RETRY_LADDER: tuple[dict[str, Any], ...] = (
    {"word_timestamps": True, "condition_on_previous_text": True},
    {"word_timestamps": True, "condition_on_previous_text": False},
    {"word_timestamps": True, "condition_on_previous_text": False,
     "hallucination_silence_threshold": 2.0},
    {"word_timestamps": False, "condition_on_previous_text": False},
)


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


def _backup_engine_attempt(
    paths: ProjectPaths, attempt_n: int, ts: str, engine_json: dict[str, Any] | None,
    candidate_doc: dict[str, Any], problems: list[str],
) -> Path:
    """Persist a rejected retry attempt (engine JSON + candidate transcript doc + the problems
    that rejected it) under transcript/engine/attempts/ so a human can inspect what each rung
    produced. Never overwrites the canonical transcript — this is scratch/audit output only."""
    ts_safe = ts.replace(":", "").replace("-", "")
    attempts_dir = paths.transcript_dir / "engine" / "attempts"
    dest = attempts_dir / f"attempt-{attempt_n}-{ts_safe}.json"
    with project_lock(paths.lock):
        attempts_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(dest, {
            "attempt": attempt_n,
            "created_at": ts,
            "problems": problems,
            "engine_json": engine_json,
            "candidate_doc": candidate_doc,
        })
    return dest


def run_transcription(
    root: Path,
    project_id: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    word_timestamps: bool = True,
    condition_on_previous_text: bool = True,
    hallucination_silence_threshold: float | None = None,
    temperature: float | None = None,
    retry: bool = True,
    actor: str = "agent",
    advance: bool = False,
) -> dict[str, Any]:
    """Transcribe the project's source audio and write the canonical transcript.

    Auto-retry mode (``retry=True``, the default): runs an escalating ladder of decode
    configurations (``_RETRY_LADDER``), validating each with ``validate_transcript_quality``.
    The first clean attempt wins; rejected attempts are backed up under
    ``transcript/engine/attempts/`` for inspection. If every rung fails validation, the best
    attempt (most cues, fewest problems) is persisted anyway — never silently — and its
    unresolved problems are surfaced in the return payload and a ``TRANSCRIPTION_QUALITY_WARNING``
    event so the QA gate and human reviewer see them.

    Single-config mode: if the caller overrides any decode flag (``retry=False``, or a non-default
    ``condition_on_previous_text`` / ``hallucination_silence_threshold`` / ``temperature``), only
    that one configuration is run — the ladder is the automatic default, not a straitjacket."""
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

    # Resolve the ASR model from tools config when the caller didn't pass one, so the
    # autopilot/manual route (which passes no --model) and the Claude CLI route both use the
    # staged offline model instead of the engine's built-in default (mlx → whisper-tiny, which
    # isn't staged and would trigger a blocked HuggingFace fetch). An explicit --model still wins.
    if model is None:
        model = load_tools_config(root).get("asr", {}).get("model") or None

    # An explicit decode override (or --no-retry) collapses the ladder to a single configuration.
    overridden = (
        not retry
        or not condition_on_previous_text
        or hallucination_silence_threshold is not None
        or temperature is not None
        or not word_timestamps
    )
    if overridden:
        configs: tuple[dict[str, Any], ...] = ({
            "word_timestamps": word_timestamps,
            "condition_on_previous_text": condition_on_previous_text,
            "hallucination_silence_threshold": hallucination_silence_threshold,
            "temperature": temperature,
        },)
    else:
        configs = _RETRY_LADDER

    engine_dir = paths.transcript_dir / "engine"
    best: dict[str, Any] | None = None  # {"doc", "result", "problems"}
    chosen_config: dict[str, Any] | None = None

    for attempt_n, cfg in enumerate(configs, start=1):
        # Long blocking ASR subprocess (no natural item count) — label the phase so the heartbeat's
        # elapsed-time line is meaningful; show the retry-ladder attempt when there is more than one.
        obs.phase(f"transcribing ({provider or 'asr'} attempt {attempt_n}/{len(configs)})")
        result = transcribe(
            audio, engine_dir,
            provider=provider, model=model, language=language,
            word_timestamps=cfg.get("word_timestamps", True),
            condition_on_previous_text=cfg.get("condition_on_previous_text", True),
            hallucination_silence_threshold=cfg.get("hallucination_silence_threshold"),
            temperature=cfg.get("temperature"),
        )
        doc = build_transcript_doc(project_id, language, result, dialect=dialect, actor=actor)
        problems = validate_transcript_quality(doc)
        if not problems:
            best = {"doc": doc, "result": result, "problems": []}
            chosen_config = cfg
            break
        # Track the best-so-far (most cues, then fewest problems) in case all rungs fail.
        score = (len(doc["cues"]), -len(problems))
        if best is None or score > (len(best["doc"]["cues"]), -len(best["problems"])):
            best = {"doc": doc, "result": result, "problems": problems}
            chosen_config = cfg
        # Only bother backing up rejected attempts when we have more rungs to try.
        if len(configs) > 1:
            _backup_engine_attempt(paths, attempt_n, utc_now(), result_engine_json(engine_dir, audio),
                                   doc, problems)

    assert best is not None  # configs is always non-empty
    doc = best["doc"]
    result = best["result"]
    unresolved = best["problems"]
    written = write_transcript(root, project_id, doc, actor=actor)

    if unresolved:
        append_event(paths.events, project_id, "TRANSCRIPTION_QUALITY_WARNING", actor, {
            "language": language,
            "problems": unresolved,
            "attempts_tried": len(configs),
            "cues": len(doc["cues"]),
        })

    advanced_to = "TRANSCRIPTION"
    if advance:
        transition(root, project_id, "TRANSCRIPT_QA_GATE", actor, reason="transcription complete")
        advanced_to = "TRANSCRIPT_QA_GATE"

    return {
        "project_id": project_id,
        "language": language,
        "provider": result.provider,
        "has_word_timing": result.has_word_timing,
        "attempts_tried": len(configs),
        "decode_config": chosen_config,
        "quality_problems": unresolved,
        **written,
        "advanced_to": advanced_to,
    }


def result_engine_json(engine_dir: Path, audio: Path) -> dict[str, Any] | None:
    """Best-effort read of the raw engine JSON the last transcribe() left in ``engine_dir``
    (for attempt backups). Returns None if it can't be located/parsed — backups are audit-only."""
    try:
        produced = sorted(engine_dir.glob(f"{audio.stem}*.json"))
        if produced:
            return load_json(produced[-1])
    except Exception:  # noqa: BLE001 - backup is best-effort, never fail the run over it
        return None
    return None


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
