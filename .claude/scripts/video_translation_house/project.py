from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ConfigurationError, ProjectNotFoundError
from .events import append_event
from .paths import PROJECT_DIRS, ProjectPaths, is_valid_video_id
from .util import (
    atomic_write_json,
    atomic_write_yaml,
    load_company_config,
    load_json,
    load_yaml,
    project_lock,
    utc_now,
)
from .validation import require_valid, validate_data


def _initial_state() -> str:
    return "INGEST"


def init_project(
    root: Path,
    project_id: str,
    *,
    url: str,
    target_languages: list[str],
    audio_languages: list[str] | None = None,
    channel: str | None = None,
    title: str | None = None,
    actor: str = "human",
) -> dict[str, Any]:
    if not is_valid_video_id(project_id):
        raise ConfigurationError(f"Invalid project/video id: {project_id!r}")
    paths = ProjectPaths(root, project_id)
    if paths.config.exists():
        raise ConfigurationError(f"Project already exists: {project_id}")
    if not target_languages:
        raise ConfigurationError("At least one target language is required")

    company = load_company_config(root)
    default_audio = audio_languages if audio_languages is not None else company.get(
        "defaults", {}
    ).get("audio_languages", ["en"])
    dub_langs = [lang for lang in default_audio if lang in target_languages]

    for sub in PROJECT_DIRS:
        (paths.directory / sub).mkdir(parents=True, exist_ok=True)

    project_cfg = {
        "schema_version": "1.0",
        "project_id": project_id,
        "source": {"url": url, "channel": channel, "title": title},
        "target_languages": target_languages,
        "audio_languages": dub_langs,
        "dubbing": {"voice_clone": False},
        "glossary_id": None,
        "created_at": utc_now(),
    }
    errors = validate_data(root, project_cfg, "project.schema.json")
    if errors:
        raise ConfigurationError("project.yaml invalid: " + "; ".join(errors))

    tracks = {
        lang: {
            "stage": "TRANSLATION",
            "status": "pending",
            "dub_enabled": lang in dub_langs,
            "updated_at": utc_now(),
        }
        for lang in target_languages
    }
    state = {
        "schema_version": "1.0",
        "project_id": project_id,
        "current_state": _initial_state(),
        "previous_state": None,
        "created_at": utc_now(),
        "created_by": actor,
        "updated_at": utc_now(),
        "updated_by": actor,
        "source_language": None,
        "target_languages": target_languages,
        "language_tracks": tracks,
        "active_artifacts": {},
        "blocked_reasons": [],
    }
    require_valid(root, state, "state.schema.json")

    with project_lock(paths.lock):
        atomic_write_yaml(paths.config, project_cfg)
        atomic_write_json(paths.state, state)
        atomic_write_json(
            paths.artifact_manifest,
            {"schema_version": "1.0", "project_id": project_id, "artifacts": []},
        )
        append_event(
            paths.events, project_id, "PROJECT_CREATED", actor,
            {"url": url, "target_languages": target_languages, "audio_languages": dub_langs},
        )
    return {"project_id": project_id, "state": state, "config": project_cfg}


def list_projects(root: Path) -> list[dict[str, Any]]:
    projects_dir = root / "projects"
    out = []
    if not projects_dir.is_dir():
        return out
    for child in sorted(projects_dir.iterdir()):
        if not child.is_dir():
            continue
        paths = ProjectPaths(root, child.name)
        if not paths.state.exists():
            continue
        try:
            state = load_json(paths.state)
            out.append({
                "project_id": child.name,
                "current_state": state.get("current_state"),
                "target_languages": state.get("target_languages", []),
                "source_language": state.get("source_language"),
            })
        except Exception:  # noqa: BLE001
            out.append({"project_id": child.name, "current_state": "UNKNOWN"})
    return out


def project_status(root: Path, project_id: str) -> dict[str, Any]:
    from .approvals import list_approvals
    from .artifacts import list_artifacts
    from .events import read_events

    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    config = load_yaml(paths.config)
    return {
        "project_id": project_id,
        "config": config,
        "state": state,
        "artifacts": list_artifacts(root, project_id),
        "approvals": list_approvals(root, project_id),
        "recent_events": read_events(paths.events, tail=15),
    }


def validate_project(root: Path, project_id: str) -> dict[str, Any]:
    paths = ProjectPaths(root, project_id)
    if not paths.config.is_file() or not paths.state.is_file():
        raise ProjectNotFoundError(f"Project not found: {project_id}")
    errors: list[str] = []
    state = load_json(paths.state)
    errors += [f"state: {e}" for e in validate_data(root, state, "state.schema.json")]
    config = load_yaml(paths.config)
    errors += [f"project: {e}" for e in validate_data(root, config, "project.schema.json")]
    if paths.artifact_manifest.exists():
        manifest = load_json(paths.artifact_manifest)
        for artifact in manifest.get("artifacts", []):
            errors += [f"artifact {artifact.get('artifact_id')}: {e}"
                       for e in validate_data(root, artifact, "artifact.schema.json")]
    if paths.rights_record.exists():
        errors += [f"rights: {e}"
                   for e in validate_data(root, load_json(paths.rights_record), "rights-record.schema.json")]
    return {"project_id": project_id, "valid": not errors, "errors": errors}
