from __future__ import annotations

from pathlib import Path
from typing import Any

from .events import append_event
from .paths import ProjectPaths
from .util import (
    atomic_write_json,
    load_json,
    project_lock,
    relative_to,
    sha256_path,
    slugify,
    utc_now,
)
from .validation import require_valid


def _manifest_default(project_id: str) -> dict[str, Any]:
    return {"schema_version": "1.0", "project_id": project_id, "artifacts": []}


def list_artifacts(root: Path, project_id: str) -> list[dict[str, Any]]:
    paths = ProjectPaths(root, project_id).require()
    return load_json(paths.artifact_manifest, _manifest_default(project_id)).get("artifacts", [])


def get_artifact_by_hash(root: Path, project_id: str, digest: str) -> dict[str, Any] | None:
    return next((a for a in list_artifacts(root, project_id) if a.get("sha256") == digest), None)


def _active_key(artifact_type: str, language: str | None) -> str:
    return f"{artifact_type}@{language}" if language else artifact_type


def resolve_hash(root: Path, project_id: str, path_or_hash: str) -> str:
    if path_or_hash.startswith("sha256:"):
        if not get_artifact_by_hash(root, project_id, path_or_hash):
            raise ValueError(f"Unregistered artifact hash: {path_or_hash}")
        return path_or_hash
    paths = ProjectPaths(root, project_id).require()
    path = paths.resolve_inside(path_or_hash)
    digest, _ = sha256_path(path)
    if not get_artifact_by_hash(root, project_id, digest):
        raise ValueError(f"Artifact is not registered: {path_or_hash} ({digest})")
    return digest


def register_artifact(
    root: Path,
    project_id: str,
    path: str | Path,
    artifact_type: str,
    stage: str,
    actor: str,
    *,
    language: str | None = None,
    rights_status: str = "unreviewed",
    validation_status: str = "not-run",
    source_artifact_ids: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
    active: bool = True,
) -> dict[str, Any]:
    paths = ProjectPaths(root, project_id).require()
    absolute = paths.resolve_inside(path)
    if not absolute.exists():
        raise FileNotFoundError(absolute)
    digest, size = sha256_path(absolute)
    rel = relative_to(absolute, paths.directory)
    with project_lock(paths.lock):
        manifest = load_json(paths.artifact_manifest, _manifest_default(project_id))
        for existing in manifest["artifacts"]:
            if existing["sha256"] == digest and existing["path"] == rel and existing["type"] == artifact_type:
                return existing
        versions = [a["version"] for a in manifest["artifacts"]
                    if a["type"] == artifact_type and a.get("language") == language]
        artifact = {
            "artifact_id": f"art-{slugify(artifact_type, 20)}-{digest.split(':')[1][:12]}",
            "project_id": project_id,
            "type": artifact_type,
            "language": language,
            "stage": stage,
            "version": max(versions, default=0) + 1,
            "path": rel,
            "sha256": digest,
            "size_bytes": size,
            "created_at": utc_now(),
            "created_by": actor,
            "source_artifact_ids": source_artifact_ids or [],
            "provenance": provenance or {},
            "rights_status": rights_status,
            "validation_status": validation_status,
            "supersedes": None,
            "frozen": False,
        }
        key = _active_key(artifact_type, language)
        previous_hash = None
        state = load_json(paths.state)
        if active:
            previous_hash = state.get("active_artifacts", {}).get(key)
            if previous_hash and previous_hash != digest:
                prev = next((x for x in manifest["artifacts"] if x["sha256"] == previous_hash), None)
                if prev:
                    artifact["supersedes"] = prev["artifact_id"]
            state.setdefault("active_artifacts", {})[key] = digest
            state["updated_at"] = utc_now()
            state["updated_by"] = actor
            atomic_write_json(paths.state, state)

        require_valid(root, artifact, "artifact.schema.json")
        manifest["artifacts"].append(artifact)
        atomic_write_json(paths.artifact_manifest, manifest)
        append_event(
            paths.events, project_id, "ARTIFACT_REGISTERED", actor,
            {"artifact_id": artifact["artifact_id"], "type": artifact_type,
             "language": language, "sha256": digest, "stage": stage},
        )

    # Superseding an active artifact invalidates approvals bound to the old bytes.
    if active and previous_hash and previous_hash != digest:
        from .approvals import invalidate_approvals_for_hashes

        invalidate_approvals_for_hashes(root, project_id, [previous_hash], actor, f"Superseded by {digest}")
    return artifact


def freeze_artifacts(root: Path, project_id: str, hashes: list[str], actor: str) -> list[str]:
    paths = ProjectPaths(root, project_id).require()
    frozen: list[str] = []
    with project_lock(paths.lock):
        manifest = load_json(paths.artifact_manifest, _manifest_default(project_id))
        for artifact in manifest["artifacts"]:
            if artifact["sha256"] in hashes and not artifact["frozen"]:
                artifact["frozen"] = True
                frozen.append(artifact["artifact_id"])
        if frozen:
            atomic_write_json(paths.artifact_manifest, manifest)
            append_event(paths.events, project_id, "ARTIFACTS_FROZEN", actor, {"artifact_ids": frozen})
    return frozen
