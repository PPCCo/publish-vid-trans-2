from __future__ import annotations

from pathlib import Path
from typing import Any

from .events import append_event
from .paths import ProjectPaths
from .util import atomic_write_json, load_json, project_lock, utc_now
from .validation import require_valid

DISTRIBUTABLE = {"self-authored", "licensed", "fair-use-claimed"}


def check_rights(root: Path, project_id: str) -> dict[str, Any]:
    paths = ProjectPaths(root, project_id).require()
    if not paths.rights_record.exists():
        return {"project_id": project_id, "rights_status": "unreviewed", "distributable": False,
                "voice_clone_consent": False, "note": "no rights record; run rights set"}
    record = load_json(paths.rights_record)
    status = record.get("rights_status", "unreviewed")
    return {
        "project_id": project_id,
        "rights_status": status,
        "distributable": status in DISTRIBUTABLE,
        "voice_clone_consent": bool(record.get("voice_clone_consent", False)),
        "reviewer": record.get("reviewer"),
    }


def set_rights(
    root: Path,
    project_id: str,
    *,
    status: str,
    reviewer: str,
    voice_clone_consent: bool = False,
    consent_evidence: str | None = None,
    evidence_paths: list[str] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    paths = ProjectPaths(root, project_id).require()
    record = {
        "schema_version": "1.0",
        "project_id": project_id,
        "rights_status": status,
        "voice_clone_consent": bool(voice_clone_consent),
        "consent_evidence": consent_evidence,
        "evidence_paths": evidence_paths or [],
        "reviewer": reviewer,
        "notes": notes,
        "updated_at": utc_now(),
        "updated_by": reviewer,
    }
    require_valid(root, record, "rights-record.schema.json")
    with project_lock(paths.lock):
        paths.rights_record.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(paths.rights_record, record)
        append_event(
            paths.events, project_id, "RIGHTS_SET", reviewer,
            {"rights_status": status, "voice_clone_consent": bool(voice_clone_consent)},
        )
    return record
