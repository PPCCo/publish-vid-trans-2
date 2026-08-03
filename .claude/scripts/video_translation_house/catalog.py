"""The repo-global discovery index: catalog/videos.json.

The catalog is an index of every downloaded source video, NOT a source of truth for
transcript/translation content. Writes are atomic + locked and NEVER silently overwrite
an existing entry: a re-ingest merges fields and appends a diff to catalog/events.ndjson
so history is auditable (plan §4.1 step 5).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .events import append_event
from .paths import is_valid_video_id
from .util import atomic_write_json, load_json, project_lock, utc_now
from .validation import require_valid

CATALOG_FIELDS = {
    "video_id", "url", "title", "channel", "channel_id", "duration_seconds",
    "upload_date", "language", "language_confidence", "language_source", "dialect",
    "has_youtube_captions", "downloaded_at", "added_at", "updated_at", "local_paths",
    "rights_status", "project_id",
}


def catalog_path(root: Path) -> Path:
    return root / "catalog" / "videos.json"


def catalog_events_path(root: Path) -> Path:
    return root / "catalog" / "events.ndjson"


def _lock_path(root: Path) -> Path:
    return root / "catalog" / ".lock"


def _load(root: Path) -> dict[str, Any]:
    data = load_json(catalog_path(root), {"schema_version": "1.0", "videos": []})
    data.setdefault("videos", [])
    return data


def list_entries(root: Path) -> list[dict[str, Any]]:
    return _load(root)["videos"]


def get_entry(root: Path, video_id: str) -> dict[str, Any] | None:
    return next((v for v in list_entries(root) if v.get("video_id") == video_id), None)


def _diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            changes[key] = {"from": old.get(key), "to": new.get(key)}
    return changes


def upsert_entry(root: Path, entry: dict[str, Any], *, actor: str = "agent") -> dict[str, Any]:
    """Insert or merge a catalog entry. Never overwrites silently; logs a diff event.

    Merge policy: new non-null values win; existing values are preserved when the
    incoming field is null/absent, so a metadata-light re-ingest can't erase data.
    """
    video_id = entry.get("video_id")
    if not video_id or not is_valid_video_id(video_id):
        raise ConfigurationError(f"catalog entry needs a valid video_id, got {video_id!r}")
    unknown = set(entry) - CATALOG_FIELDS
    if unknown:
        raise ConfigurationError(f"unknown catalog fields: {', '.join(sorted(unknown))}")

    with project_lock(_lock_path(root)):
        data = _load(root)
        videos = data["videos"]
        existing = next((v for v in videos if v.get("video_id") == video_id), None)
        now = utc_now()

        if existing is None:
            merged = {"added_at": now, "rights_status": "unreviewed", **entry}
            merged.setdefault("added_at", now)
            require_valid(root, merged, "catalog-entry.schema.json")
            videos.append(merged)
            videos.sort(key=lambda v: v.get("video_id", ""))
            atomic_write_json(catalog_path(root), data)
            _log_catalog_event(root, video_id, "CATALOG_ENTRY_ADDED",
                               {"url": merged.get("url")}, actor)
            return merged

        merged = dict(existing)
        for key, value in entry.items():
            if value is not None and value != []:
                merged[key] = value
        merged["updated_at"] = now
        changes = _diff(existing, merged)
        if not changes:
            return existing
        require_valid(root, merged, "catalog-entry.schema.json")
        videos[:] = [merged if v is existing else v for v in videos]
        atomic_write_json(catalog_path(root), data)
        _log_catalog_event(root, video_id, "CATALOG_ENTRY_MERGED", {"changes": changes}, actor)
        return merged


def _log_catalog_event(root: Path, video_id: str, event: str, details: dict[str, Any], actor: str) -> None:
    append_event(catalog_events_path(root), video_id, event, actor, details)
