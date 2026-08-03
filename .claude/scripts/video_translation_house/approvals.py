from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ApprovalError
from .events import append_event
from .paths import ProjectPaths
from .util import (
    atomic_write_json,
    load_json,
    parse_time,
    project_lock,
    random_token,
    utc_now,
)


def _approvals_dir(paths: ProjectPaths) -> Path:
    d = paths.directory / "approvals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_approvals(root: Path, project_id: str) -> list[dict[str, Any]]:
    paths = ProjectPaths(root, project_id).require()
    d = paths.directory / "approvals"
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("apr-*.json")):
        try:
            out.append(load_json(f))
        except Exception:  # noqa: BLE001
            continue
    return out


def approval_is_current(record: dict[str, Any]) -> bool:
    if record.get("decision") != "APPROVED" or record.get("invalidated_at"):
        return False
    expires = record.get("expires_at")
    return not expires or parse_time(expires) > parse_time(utc_now())


def has_valid_approval(root: Path, project_id: str, gate: str, language: str | None = None) -> bool:
    for record in list_approvals(root, project_id):
        if record.get("gate") != gate or not approval_is_current(record):
            continue
        if language is not None and record.get("language") != language:
            continue
        return True
    return False


def grant_approval(
    root: Path,
    project_id: str,
    gate: str,
    approver: str,
    artifact_hashes: list[str],
    scope: str,
    *,
    language: str | None = None,
    approver_role: str | None = None,
    decision: str = "APPROVED",
    notes: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    paths = ProjectPaths(root, project_id).require()
    if not artifact_hashes:
        raise ApprovalError("An approval must bind at least one artifact hash")
    # Every hash must be a currently-registered artifact.
    from .artifacts import get_artifact_by_hash

    for h in artifact_hashes:
        if not get_artifact_by_hash(root, project_id, h):
            raise ApprovalError(f"Cannot approve unregistered artifact hash: {h}")

    record = {
        "approval_id": f"apr-{gate}-{utc_now().replace(':', '').replace('-', '').lower()}-{random_token(6)}",
        "project_id": project_id,
        "gate": gate,
        "language": language,
        "approver": approver,
        "approver_role": approver_role,
        "decision": decision,
        "artifact_hashes": artifact_hashes,
        "scope": scope,
        "exclusions": [],
        "notes": notes,
        "created_at": utc_now(),
        "expires_at": expires_at,
        "invalidated_at": None,
        "invalidation_reason": None,
    }
    from .validation import require_valid

    require_valid(root, record, "approval.schema.json")
    with project_lock(paths.lock):
        path = _approvals_dir(paths) / f"{record['approval_id']}.json"
        atomic_write_json(path, record)
        append_event(
            paths.events, project_id, "APPROVAL_RECORDED", approver,
            {"gate": gate, "language": language, "decision": decision,
             "artifact_hashes": artifact_hashes, "approval_id": record["approval_id"]},
        )
    return record


def invalidate_approvals_for_hashes(
    root: Path, project_id: str, hashes: list[str], actor: str, reason: str
) -> list[str]:
    paths = ProjectPaths(root, project_id).require()
    invalidated: list[str] = []
    with project_lock(paths.lock):
        d = paths.directory / "approvals"
        if not d.exists():
            return []
        for f in sorted(d.glob("apr-*.json")):
            record = load_json(f)
            if record.get("invalidated_at"):
                continue
            if any(h in record.get("artifact_hashes", []) for h in hashes):
                record["invalidated_at"] = utc_now()
                record["invalidation_reason"] = reason
                atomic_write_json(f, record)
                invalidated.append(record["approval_id"])
        if invalidated:
            append_event(
                paths.events, project_id, "APPROVALS_INVALIDATED", actor,
                {"approval_ids": invalidated, "reason": reason},
            )
    return invalidated
