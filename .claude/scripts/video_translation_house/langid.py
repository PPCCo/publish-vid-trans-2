"""Language identification: cheap, human-overridable, never blocking.

Per the plan (§4.2, §5.3) language ID prefills a field; it is explicitly allowed to be a
human-fillable step rather than an automation blocker. Automated detection runs through
the ASR adapter (whose engines are all opt-in), so when no engine is installed this
module reports `detected: null` and defers to a human `langid set` — it does not fail.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .events import append_event
from .paths import ProjectPaths
from .util import (
    KNOWN_LANGUAGES,
    atomic_write_json,
    load_json,
    load_tools_config,
    project_lock,
    utc_now,
)

# YouTube/info-json language tags map onto our ISO 639-1 set; keep this explicit so a
# surprising tag never silently becomes a "detected" language.
_METADATA_LANG_MAP = {
    "fa": "fa", "per": "fa", "fas": "fa", "persian": "fa", "farsi": "fa",
    "ar": "ar", "ara": "ar", "arabic": "ar",
    "ur": "ur", "urd": "ur", "urdu": "ur",
    "en": "en", "eng": "en", "english": "en",
}


def normalize_language(value: str | None) -> str | None:
    if not value:
        return None
    key = value.strip().lower()
    if key in _METADATA_LANG_MAP:
        return _METADATA_LANG_MAP[key]
    if key in KNOWN_LANGUAGES:
        return key
    # Accept bare ISO codes we simply haven't enumerated, but flag unknown free text.
    return key if len(key) <= 3 and key.isalpha() else None


def language_from_metadata(info: dict[str, Any]) -> tuple[str | None, float | None]:
    """Best-effort language from yt-dlp info JSON. Low confidence — a hint, not truth."""
    for field in ("language",):
        lang = normalize_language(info.get(field))
        if lang:
            return lang, 0.5
    return None, None


def detect_from_audio(root: Path, wav_path: Path | str) -> dict[str, Any]:
    """Run the configured ASR adapter's language-detection head, if one is available.

    Returns {"detected": <iso|null>, "confidence": <float|null>, "provider": <str|null>,
    "available": bool}. Never raises on a missing engine — that is the whole point.
    """
    tools = load_tools_config(root)
    provider = tools.get("asr", {}).get("provider")
    # Phase 1 keeps engines opt-in; the concrete adapter lands in Phase 2. Until an
    # engine is verified+installed, report unavailable so callers defer to a human.
    return {
        "detected": None,
        "confidence": None,
        "provider": provider,
        "available": False,
        "note": "no verified ASR adapter installed; set language manually via `langid set`",
    }


def set_language(
    root: Path,
    project_id: str,
    language: str,
    *,
    source: str = "manual",
    confidence: float | None = None,
    dialect: str | None = None,
    actor: str = "human",
) -> dict[str, Any]:
    """Record the confirmed source language on project state + source metadata + catalog."""
    normalized = normalize_language(language)
    if normalized is None:
        raise ConfigurationError(f"Unrecognized language code: {language!r}")
    paths = ProjectPaths(root, project_id).require()
    with project_lock(paths.lock):
        state = load_json(paths.state)
        state["source_language"] = normalized
        # If the source language is also a translation TARGET, that track skips TRANSLATION:
        # translating source->source is pointless. It still produces verbatim source captions
        # (see translate.export_worksheet), so mark it here and let the export short-circuit
        # emit the caption doc. The marker is what excludes the track from translation
        # quorums/gates downstream (translate._translatable_track_langs, state gate checks).
        tracks = state.get("language_tracks", {})
        if normalized in tracks:
            tracks[normalized]["skip_translation"] = True
            tracks[normalized]["updated_at"] = utc_now()
            if not tracks[normalized].get("notes"):
                tracks[normalized]["notes"] = "source language — no translation; verbatim captions"
        state["updated_at"] = utc_now()
        state["updated_by"] = actor
        atomic_write_json(paths.state, state)

        meta_path = paths.source_dir / "metadata.json"
        if meta_path.exists():
            meta = load_json(meta_path)
            meta["detected_language"] = normalized
            meta["language_source"] = source
            meta["language_confidence"] = confidence
            if dialect:
                meta["dialect"] = dialect
            atomic_write_json(meta_path, meta)

        append_event(
            paths.events, project_id, "SOURCE_LANGUAGE_SET", actor,
            {"language": normalized, "source": source, "confidence": confidence, "dialect": dialect},
        )

    # Reflect into the repo-global catalog (best-effort; catalog entry may not exist yet).
    from .catalog import get_entry, upsert_entry

    entry = get_entry(root, project_id)
    if entry:
        upsert_entry(root, {
            "video_id": project_id, "url": entry.get("url", ""),
            "language": normalized, "language_confidence": confidence,
            "language_source": source, "dialect": dialect,
        }, actor=actor)
    return {"project_id": project_id, "source_language": normalized, "source": source}
