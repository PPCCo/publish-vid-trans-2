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
    "rights_status", "project_id", "playlist_id", "playlist_title",
}


def _default_targets_csv(root: Path) -> str:
    """The company default target-language set as a CSV, for a kickoff `next_command`."""
    from .util import load_company_config

    try:
        defaults = (load_company_config(root).get("defaults") or {}).get("target_languages") or []
    except Exception:
        defaults = []
    return ",".join(defaults) if defaults else "en"


def catalog_path(root: Path) -> Path:
    return root / "catalog" / "videos.json"


def playlists_path(root: Path) -> Path:
    return root / "catalog" / "playlists.json"


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


def remove_entry(root: Path, video_id: str, *, actor: str = "agent") -> dict[str, Any]:
    """Delete a catalog entry (the teardown twin of ``upsert_entry``).

    Idempotent: removing a video_id that isn't in the catalog returns
    ``{"removed": False}`` without raising, mirroring the tolerant style elsewhere.
    Logs a ``CATALOG_ENTRY_REMOVED`` event so the removal is auditable (the catalog is
    append-only history; deleting an entry does not rewrite that history).
    """
    with project_lock(_lock_path(root)):
        data = _load(root)
        videos = data["videos"]
        existing = next((v for v in videos if v.get("video_id") == video_id), None)
        if existing is None:
            return {"video_id": video_id, "removed": False}
        videos[:] = [v for v in videos if v is not existing]
        atomic_write_json(catalog_path(root), data)
        _log_catalog_event(root, video_id, "CATALOG_ENTRY_REMOVED",
                           {"url": existing.get("url"), "project_id": existing.get("project_id")},
                           actor)
        return {"video_id": video_id, "removed": True}


def _log_catalog_event(root: Path, video_id: str, event: str, details: dict[str, Any], actor: str) -> None:
    append_event(catalog_events_path(root), video_id, event, actor, details)


# --------------------------------------------------------------------------------------
# Playlists: a light index of playlist_id -> {url, title, video_ids[]} for O(1) listing
# and playlist-level provenance. The per-video catalog entry carries playlist_id/title too
# (so a single entry is self-describing); this store is the reverse index.
# --------------------------------------------------------------------------------------

def _load_playlists(root: Path) -> dict[str, Any]:
    data = load_json(playlists_path(root), {"schema_version": "1.0", "playlists": []})
    data.setdefault("playlists", [])
    return data


def list_playlists(root: Path) -> list[dict[str, Any]]:
    return _load_playlists(root)["playlists"]


def get_playlist(root: Path, playlist_id: str) -> dict[str, Any] | None:
    return next((p for p in list_playlists(root) if p.get("playlist_id") == playlist_id), None)


def upsert_playlist(
    root: Path,
    playlist_id: str,
    *,
    url: str,
    title: str | None,
    video_ids: list[str],
    actor: str = "agent",
) -> dict[str, Any]:
    """Insert or merge a playlist index entry, unioning its video_ids. Locked + atomic."""
    if not playlist_id:
        raise ConfigurationError("upsert_playlist needs a playlist_id")
    with project_lock(_lock_path(root)):
        data = _load_playlists(root)
        playlists = data["playlists"]
        existing = next((p for p in playlists if p.get("playlist_id") == playlist_id), None)
        now = utc_now()
        if existing is None:
            entry = {
                "playlist_id": playlist_id, "url": url, "title": title,
                "video_ids": list(dict.fromkeys(video_ids)),
                "added_at": now, "updated_at": now,
            }
            playlists.append(entry)
            playlists.sort(key=lambda p: p.get("playlist_id", ""))
            atomic_write_json(playlists_path(root), data)
            _log_catalog_event(root, playlist_id, "PLAYLIST_ADDED",
                               {"url": url, "video_count": len(entry["video_ids"])}, actor)
            return entry
        # Re-enumeration is authoritative for order (yt-dlp returns the live playlist
        # sequence); a video removed from the source playlist between runs is dropped
        # from the index rather than kept at a stale position. Fall back to appending any
        # previously-known id that the fresh enumeration didn't include, so a partial/failed
        # re-enumeration can't silently lose videos.
        merged_ids = list(dict.fromkeys(video_ids)) if video_ids else list(
            existing.get("video_ids", [])
        )
        for vid in existing.get("video_ids", []):
            if vid not in merged_ids:
                merged_ids.append(vid)
        existing["video_ids"] = merged_ids
        if title and not existing.get("title"):
            existing["title"] = title
        existing["updated_at"] = now
        atomic_write_json(playlists_path(root), data)
        _log_catalog_event(root, playlist_id, "PLAYLIST_UPDATED",
                           {"video_count": len(merged_ids)}, actor)
        return existing


def add_playlist(root: Path, url: str, *, actor: str = "agent") -> dict[str, Any]:
    """Enumerate a playlist (metadata only) and index every video under it.

    Enumeration is the sanctioned flag-free carve-out from VIDTRANS_FETCH_ENABLED (it reads
    titles/ids only — no media bytes; see net.fetch.ytdlp_playlist_entries and rule 3). Each
    entry is upserted into the catalog with a derived ``yt-<id>`` video_id and the playlist
    fields; merge policy preserves an already-catalogued video's status, so a video that is
    already in-progress simply *moves under* the playlist rather than being duplicated.

    Index-only: this does NOT init projects or download media. Per-video kickoff happens later
    via each entry's derived ``next_command`` (see enrich_entry).
    """
    from .net import ytdlp_playlist_entries  # lazy: keeps a no-net CLI import cheap

    entries = ytdlp_playlist_entries(url)
    playlist_id = entries[0]["playlist_id"] if entries else None
    playlist_title = entries[0]["playlist_title"] if entries else None
    if not playlist_id:
        # yt-dlp couldn't resolve a playlist id (e.g. a single-video URL) — refuse rather
        # than silently indexing under a null playlist.
        raise ConfigurationError(f"no playlist id resolved from {url!r}; is this a playlist URL?")

    added: list[str] = []
    linked_existing: list[str] = []
    video_ids: list[str] = []
    for e in entries:
        vid = f"yt-{e['id']}"
        if not is_valid_video_id(vid):
            continue
        video_ids.append(vid)
        before = get_entry(root, vid)
        upsert_entry(root, {
            "video_id": vid,
            "url": e.get("url") or url,
            "title": e.get("title"),
            "channel": e.get("channel"),
            "duration_seconds": e.get("duration_seconds"),
            "playlist_id": playlist_id,
            "playlist_title": playlist_title,
        }, actor=actor)
        (linked_existing if before is not None else added).append(vid)

    upsert_playlist(root, playlist_id, url=url, title=playlist_title,
                    video_ids=video_ids, actor=actor)
    return {
        "playlist_id": playlist_id, "playlist_title": playlist_title,
        "added": added, "linked_existing": linked_existing, "video_ids": video_ids,
    }


# --------------------------------------------------------------------------------------
# Derived, read-only fields: next_command (+ review_files at gate steps). Computed fresh on
# read from state.plan() so they are never stale and never persisted (rule 1). This is a
# convenience shortcut in the index — it does NOT relax rule 13: when Claude drives a gate
# in-conversation it still does disclose->confirm->grant; the field is a power-user hint and,
# for the outward-facing gates, is exactly the `!` command the human already runs.
# --------------------------------------------------------------------------------------

# current_state -> the literal CLI verb that performs the next NON-GATE step from it.
_STATE_NEXT_VERB = {
    "INGEST": "ingest run {id}",
    "LANGUAGE_ID": "langid set {id} --language <iso>",
    "TRANSCRIPTION": "transcript run {id} --advance",
    "SEGMENT_RESOLUTION": "segments resolve {id} --advance",
    "TRANSLATION": "translate export {id} --lang <iso>   # then fill + `translate import`",
    "CAPTION_TIMING": "captions build {id} --advance",
    "CAPTION_VALIDATION": "captions validate {id} --advance",
    "DUBBING": "dub run {id} --advance",
    "AUDIO_SYNC_ADJUST": "dub sync {id} --advance",
    "VIDEO_MUX": "package mux {id} --advance",
    "PACKAGE": "package build {id} --advance",
    "PLATFORM_PACKAGING": "distribution package {id} --advance",
    "YOUTUBE_UPLOAD": "distribution upload {id}",
    "PROMOTION_QUEUE": "promotion queue {id} --advance",
    "PROMOTION_PUBLISHED": "promotion publish {id} --advance",
}


def _cli() -> str:
    return "vid_cli.py"


# Named command builders shared by enrich_entry (the catalog next_command view) and cmdgen
# (the raw-external command printer). Kept in one place so the two surfaces never drift — the
# strings they produce are byte-identical.
def _kickoff_command(cli: str, vid: str, url: str, targets_csv: str) -> str:
    """The compound `project init … && ingest run …` for a not-yet-started video."""
    return (f"{cli} project init {vid} --url {url} --targets {targets_csv} "
            f"&& {cli} ingest run {vid}")


def _gate_reference_command(cli: str, vid: str, gate: str, target: str) -> str:
    """The disclose+confirm-first `approval grant … && project transition …` reference (rule 13)."""
    return (f"{cli} approval grant {vid} --gate {gate} --approver \"<you>\" "
            f"--scope project --artifact <sha256>  # disclose+confirm first (rule 13)"
            f" && {cli} project transition {vid} --to {target} --actor human")


def _rights_blocked_command(cli: str, vid: str) -> str:
    """The `rights set …` a human runs to clear a rights-blocked PACKAGE → READY_FOR_REVIEW."""
    return (f"{cli} rights set {vid} --status <self-authored|licensed|fair-use-claimed> "
            f"--reviewer \"<you>\"")


# autonomy_action -> a coarse, human-facing progress bucket. Mirrors autonomy_action 1:1
# (TERMINAL splits into "done" only for the real terminal states below; PAUSED/CANCELLED/
# ERROR are surfaced as their own buckets since "blocked" would understate a cancellation).
_TERMINAL_DONE = {"READY_FOR_REVIEW", "MONITORING"}
_STATUS_BUCKETS = {
    "PROCEED": "in-progress",
    "STOP_AT_GATE": "gate-pending",
    "BLOCKED": "blocked",
}


def _status_for(current: str | None, action: str | None) -> str:
    if current in {"CANCELLED", "ERROR"}:
        return current.lower()
    if action == "TERMINAL":
        return "done" if current in _TERMINAL_DONE else "stopped"
    return _STATUS_BUCKETS.get(action, "blocked")


def summarize(entries: list[dict[str, Any]]) -> dict[str, int]:
    """Tally already-enriched entries by their derived ``status`` bucket.

    Shared by the ``catalog list`` and ``catalog playlist`` CLI verbs so the two surfaces
    compute the same rollup (was inline in the playlist handler). Pure — expects each entry
    to already carry a ``status`` (from ``enrich_entry``); entries missing one count as
    ``unknown`` rather than raising.
    """
    summary: dict[str, int] = {}
    for e in entries:
        bucket = e.get("status") or "unknown"
        summary[bucket] = summary.get(bucket, 0) + 1
    return summary


def enrich_entry(root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``entry`` with a derived ``status``, ``next_command`` (+
    ``review_files`` at gate steps). Pure/read-only: reads state.plan() for entries that have
    a project; never writes. Nothing here is persisted — call this fresh on every read (rule 1:
    the CLI owns state, this is a view over it)."""
    from . import state as state_mod

    vid = entry.get("video_id")
    out = dict(entry)
    project_id = entry.get("project_id")

    # Not yet a project: the next step is to kick one off (still flag-gated media download).
    if not project_id:
        out["status"] = "not-started"
        out["current_state"] = None
        targets = _default_targets_csv(root)
        out["next_command"] = "! " + _kickoff_command(
            _cli(), vid, entry.get("url", "<url>"), targets)
        out.pop("review_files", None)
        return out

    try:
        plan = state_mod.plan(root, project_id)
    except Exception:
        # Project referenced but unreadable (e.g. deleted dir) — surface no command rather
        # than a wrong one.
        out["status"] = "unknown"
        out["current_state"] = None
        out["next_command"] = None
        out.pop("review_files", None)
        return out

    action = plan.get("autonomy_action")
    current = plan.get("current_state")
    out["status"] = _status_for(current, action)
    out["current_state"] = current

    if action == "TERMINAL":
        out["next_command"] = None
        out.pop("review_files", None)
        return out

    if action == "PROCEED":
        verb = _STATE_NEXT_VERB.get(current)
        out["next_command"] = (f"! {_cli()} " + verb.format(id=vid)) if verb else None
        out.pop("review_files", None)
        return out

    if action == "STOP_AT_GATE":
        gate = plan.get("required_gate")
        target = plan.get("recommended_target")
        out["next_command"] = "! " + _gate_reference_command(_cli(), vid, gate, target)
        rf = _gate_review_files(root, project_id, current, target)
        if rf:
            out["review_files"] = rf
        else:
            out.pop("review_files", None)
        return out

    # BLOCKED: the common cause is rights not yet set before PACKAGE->READY_FOR_REVIEW.
    if current == "PACKAGE":
        out["next_command"] = "! " + _rights_blocked_command(_cli(), vid)
    else:
        out["next_command"] = None
    out.pop("review_files", None)
    return out


def project_progress(root: Path, project_id: str) -> dict[str, Any]:
    """Derive ``{status, autonomy_action, current_state, next_command}`` for an existing
    project, reusing the same buckets + command builders as ``enrich_entry``.

    Used by ``project list`` to enrich each project row (``enrich_entry`` is keyed on a
    catalog entry with a url; this is the project-only view). Read-only / never persisted
    (rule 1). Best-effort: on a ``state.plan()`` error the status is ``unknown`` and the
    other fields are null — mirrors ``enrich_entry``'s tolerance for an unreadable project.
    """
    from . import state as state_mod

    try:
        plan = state_mod.plan(root, project_id)
    except Exception:
        return {"status": "unknown", "autonomy_action": None,
                "current_state": None, "next_command": None}

    action = plan.get("autonomy_action")
    current = plan.get("current_state")
    out = {
        "status": _status_for(current, action),
        "autonomy_action": action,
        "current_state": current,
        "next_command": None,
    }
    if action == "PROCEED":
        verb = _STATE_NEXT_VERB.get(current)
        out["next_command"] = (f"! {_cli()} " + verb.format(id=project_id)) if verb else None
    elif action == "STOP_AT_GATE":
        gate = plan.get("required_gate")
        target = plan.get("recommended_target")
        out["next_command"] = "! " + _gate_reference_command(_cli(), project_id, gate, target)
    elif action == "BLOCKED" and current == "PACKAGE":
        out["next_command"] = "! " + _rights_blocked_command(_cli(), project_id)
    return out


def _gate_review_files(root: Path, project_id: str, current: str, target: str | None) -> list[str]:
    """Relative artifact paths a human reviews at this gate: the active gate-report doc(s)
    plus the per-language artifacts the gate binds. Best-effort; empty list if none resolve."""
    from . import state as state_mod
    from .paths import ProjectPaths

    edge = f"{current}->{target}" if target else None
    reports = (state_mod.workflow_config(root).get("gate_reports", {}) or {}).get(edge, []) if edge else []
    paths = ProjectPaths(root, project_id)
    files: list[str] = []
    for rtype in reports:
        # Gate report docs are keyed by report type: reviews/<rtype>-gate-latest.json.
        candidate = paths.gate_report(rtype)
        if candidate.is_file():
            files.append(candidate.relative_to(paths.directory).as_posix())
    return sorted(set(files))
