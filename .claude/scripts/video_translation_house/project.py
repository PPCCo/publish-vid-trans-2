from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .errors import ConfigurationError, ProjectNotFoundError
from .events import append_event
from .paths import PROJECT_DIRS, ProjectPaths, ensure_within, is_valid_video_id
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


# Subpaths under transcript/ preserved by a keep-transcript reset: the source ASR transcript
# doc(s) (source.<lang>.json) and the engine's raw output. Everything else in transcript/
# (english-gloss*, qa-report.json) is downstream review work and is wiped.
_TRANSCRIPT_KEEP_GLOBS = ("source.*.json", "engine")


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
    selection: dict[str, Any] | None = None,
    join_clips: bool = True,
    snap_edges: bool = True,
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

    # A selection restricts processing to specific source windows (resolved at
    # SEGMENT_RESOLUTION). Absent/null selection = whole video. `snap_edges` is a property of
    # the selection object; fold the CLI flag in without clobbering an explicit value.
    if selection is not None:
        if not isinstance(selection, dict):
            raise ConfigurationError("selection must be a JSON object or null")
        selection = dict(selection)
        selection.setdefault("snap_edges", snap_edges)

    project_cfg = {
        "schema_version": "1.0",
        "project_id": project_id,
        "source": {"url": url, "channel": channel, "title": title},
        "target_languages": target_languages,
        "audio_languages": dub_langs,
        "dubbing": {"voice_clone": False},
        "glossary_id": None,
        "join_clips": bool(join_clips),
        "selection": selection,
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


def add_languages(
    root: Path,
    project_id: str,
    *,
    target_languages: list[str],
    audio_languages: list[str] | None = None,
    actor: str = "human",
) -> dict[str, Any]:
    """Add translation/dub tracks to an existing project WITHOUT touching existing work.

    The sanctioned way to widen an in-progress project's language set (rule 1: the CLI owns
    state — this mutates ``state.json`` + the project config through the same atomic+validated
    path as ``init_project``, never by hand). Existing language tracks, their approvals, and
    all registered artifacts are left untouched — new tracks are appended at the ``TRANSLATION``
    stage exactly as ``init_project`` seeds them, and the translation stage picks them up on the
    next ``translate export`` (``translate`` reads tracks from live state).

    Idempotent: languages already present are ignored; adding only already-present languages is
    a no-op (no state change, no event). A newly-added language that equals the confirmed
    ``source_language`` is marked ``skip_translation`` (rule 7), mirroring ``langid.set_language``.
    Events are append-only: appends a ``LANGUAGES_ADDED`` event.
    """
    from .langid import normalize_language

    paths = ProjectPaths(root, project_id).require()

    requested: list[str] = []
    for raw in target_languages:
        norm = normalize_language(raw)
        if norm is None:
            raise ConfigurationError(f"Unrecognized language code: {raw!r}")
        if norm not in requested:
            requested.append(norm)
    if not requested:
        raise ConfigurationError("At least one target language is required")

    # --audio must be a subset of the languages being added (dubbing a language not being
    # added makes no sense here); default = dub every newly-added language.
    requested_audio: list[str] | None = None
    if audio_languages is not None:
        requested_audio = []
        for raw in audio_languages:
            norm = normalize_language(raw)
            if norm is None:
                raise ConfigurationError(f"Unrecognized audio language code: {raw!r}")
            requested_audio.append(norm)

    with project_lock(paths.lock):
        state = load_json(paths.state)
        existing = list(state.get("target_languages", []))
        new_langs = [lang for lang in requested if lang not in existing]
        if not new_langs:
            return {
                "project_id": project_id,
                "added": [],
                "dub_enabled": [],
                "target_languages": existing,
            }

        bad_audio = [lang for lang in (requested_audio or []) if lang not in new_langs]
        if bad_audio:
            raise ConfigurationError(
                "--audio must be a subset of the languages being added; "
                f"not being added: {', '.join(bad_audio)}"
            )
        dub_langs = requested_audio if requested_audio is not None else list(new_langs)

        source_language = state.get("source_language")
        tracks = state.setdefault("language_tracks", {})
        for lang in new_langs:
            track = {
                "stage": "TRANSLATION",
                "status": "pending",
                "dub_enabled": lang in dub_langs,
                "updated_at": utc_now(),
            }
            # Source language is never translated (rule 7): it skips TRANSLATION and gets
            # verbatim captions instead. Mirror langid.set_language's marker so the track is
            # excluded from translation quorums/gates downstream.
            if source_language and lang == source_language:
                track["skip_translation"] = True
                track["notes"] = "source language — no translation; verbatim captions"
            tracks[lang] = track

        state["target_languages"] = existing + new_langs
        state["updated_at"] = utc_now()
        state["updated_by"] = actor
        require_valid(root, state, "state.schema.json")
        atomic_write_json(paths.state, state)

        # Keep the project config consistent with init_project's contract (it carries both
        # target_languages and audio_languages). State is authoritative, but a stale config
        # would make `project status` misreport the project's languages.
        cfg = load_yaml(paths.config)
        cfg["target_languages"] = list(cfg.get("target_languages", [])) + new_langs
        cfg_audio = list(cfg.get("audio_languages", []))
        cfg["audio_languages"] = cfg_audio + [lang for lang in dub_langs if lang not in cfg_audio]
        errors = validate_data(root, cfg, "project.schema.json")
        if errors:
            raise ConfigurationError("project.yaml invalid after add: " + "; ".join(errors))
        atomic_write_yaml(paths.config, cfg)

        append_event(
            paths.events, project_id, "LANGUAGES_ADDED", actor,
            {"added": new_langs, "dub_enabled": [lang for lang in dub_langs if lang in new_langs]},
        )

    return {
        "project_id": project_id,
        "added": new_langs,
        "dub_enabled": [lang for lang in dub_langs if lang in new_langs],
        "target_languages": existing + new_langs,
    }


def _clear_dir_contents(directory: Path, *, keep_globs: tuple[str, ...] = ()) -> None:
    """Delete everything under ``directory`` except entries matching ``keep_globs``,
    leaving the (empty) directory itself in place so the PROJECT_DIRS skeleton stays intact."""
    if not directory.is_dir():
        directory.mkdir(parents=True, exist_ok=True)
        return
    keep: set[Path] = set()
    for pattern in keep_globs:
        keep.update(directory.glob(pattern))
    for child in directory.iterdir():
        if child in keep:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def reset_project(
    root: Path,
    project_id: str,
    *,
    keep_source: bool = True,
    keep_transcript: bool = True,
    to_state: str | None = None,
    actor: str = "agent",
) -> dict[str, Any]:
    """Rewind a project to an earlier pipeline state, wiping downstream work.

    The sanctioned teardown-and-restart path (rule 1: the CLI owns state — this rewrites
    ``state.json`` through the same atomic+validated path as ``init_project``/``transition``,
    never by hand). By default it preserves the expensive-to-regenerate ``source/`` media and
    the source ASR transcript, and resumes at ``TRANSCRIPTION`` so the transcript QA gate can be
    re-run. Everything downstream (gloss, QA reports, reviews, approvals, segments, captions,
    audio, video, packages, chapters, distribution, rights) is cleared.

    Events are append-only: this appends a ``PROJECT_RESET`` event; it does not erase history.
    """
    from . import artifacts as artifacts_mod
    from . import state as state_mod
    from .transcript import transcript_filename

    paths = ProjectPaths(root, project_id).require()
    prior = load_json(paths.state)
    source_language = prior.get("source_language")

    # keep_source is a prerequisite for keep_transcript (the transcript describes the source);
    # dropping the source implies dropping the transcript.
    if not keep_source:
        keep_transcript = False

    if to_state is None:
        if keep_transcript and source_language:
            to_state = "TRANSCRIPTION"
        elif keep_source:
            to_state = "LANGUAGE_ID"
        else:
            to_state = _initial_state()
    states = state_mod.workflow_config(root).get("states", [])
    if to_state not in states:
        raise ConfigurationError(f"reset target is not a valid state: {to_state!r}")

    keep_language = source_language if (keep_source or keep_transcript) else None
    removed: list[str] = []
    with project_lock(paths.lock):
        for sub in PROJECT_DIRS:
            directory = paths.directory / sub
            if sub == "source" and keep_source:
                continue
            if sub == "transcript" and keep_transcript:
                _clear_dir_contents(directory, keep_globs=_TRANSCRIPT_KEEP_GLOBS)
                removed.append(f"{sub}/* (kept source transcript + engine)")
                continue
            if directory.is_dir() and any(directory.iterdir()):
                removed.append(f"{sub}/*")
            _clear_dir_contents(directory)

        # Reset the artifact manifest to the seeded empty shape; preserved artifacts are
        # re-registered below (outside the lock) so hashes + active_artifacts are honest.
        atomic_write_json(
            paths.artifact_manifest,
            {"schema_version": "1.0", "project_id": project_id, "artifacts": []},
        )

        tracks = {
            lang: {
                "stage": "TRANSLATION",
                "status": "pending",
                "dub_enabled": bool(track.get("dub_enabled", False)),
                "updated_at": utc_now(),
            }
            for lang, track in prior.get("language_tracks", {}).items()
        }
        state = {
            "schema_version": "1.0",
            "project_id": project_id,
            "current_state": to_state,
            "previous_state": None,
            "created_at": prior.get("created_at", utc_now()),
            "created_by": prior.get("created_by", actor),
            "updated_at": utc_now(),
            "updated_by": actor,
            "source_language": keep_language,
            "target_languages": prior.get("target_languages", []),
            "language_tracks": tracks,
            "active_artifacts": {},
            "blocked_reasons": [],
        }
        require_valid(root, state, "state.schema.json")
        atomic_write_json(paths.state, state)
        append_event(paths.events, project_id, "PROJECT_RESET", actor, {
            "to_state": to_state,
            "kept_source": keep_source,
            "kept_transcript": keep_transcript,
            "removed": removed,
        })

    # Re-register preserved artifacts through the standard producer path so their hashes and
    # active_artifacts entries are correct (register_artifact takes the lock itself, so this
    # runs after the reset block above — no nested lock).
    reregistered: list[str] = []
    if keep_source:
        for name, atype in (("source-video", "source-video"), ("source-audio", "source-audio")):
            for candidate in _preserved_source_files(paths, name):
                artifacts_mod.register_artifact(
                    root, project_id, candidate, atype, "INGEST", actor, active=True)
                reregistered.append(atype)
                break
    if keep_transcript and source_language:
        tpath = paths.transcript_dir / transcript_filename(source_language)
        if tpath.exists():
            artifacts_mod.register_artifact(
                root, project_id, tpath, "transcript", "TRANSCRIPTION", actor,
                language=source_language, active=True)
            reregistered.append(f"transcript@{source_language}")

    return {
        "project_id": project_id,
        "current_state": to_state,
        "kept_source": keep_source,
        "kept_transcript": keep_transcript,
        "reregistered_artifacts": reregistered,
        "removed_paths": removed,
    }


def _preserved_source_files(paths: ProjectPaths, kind: str) -> list[Path]:
    """Locate the source media file(s) for re-registration after a keep-source reset."""
    src = paths.source_dir
    if not src.is_dir():
        return []
    if kind == "source-audio":
        return [p for p in [src / "audio.wav"] if p.exists()]
    # source-video: the downloaded container (mp4/mkv/webm), excluding audio.wav
    return sorted(p for p in src.iterdir()
                  if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"})


def delete_project(
    root: Path,
    project_id: str,
    *,
    purge_catalog: bool = True,
    actor: str = "agent",
) -> dict[str, Any]:
    """Permanently remove a project directory and (by default) its catalog entry.

    Destructive and hard to reverse. The catalog removal is logged as a ``CATALOG_ENTRY_REMOVED``
    event (the project's own events file is deleted with the directory)."""
    from . import catalog as catalog_mod

    paths = ProjectPaths(root, project_id).require()
    projects_root = (root / "projects").resolve()
    directory = paths.directory.resolve()
    # Defensive: never rmtree outside root/projects/<id>.
    ensure_within(directory, projects_root)
    if directory == projects_root:
        raise ConfigurationError("refusing to delete the projects root")

    shutil.rmtree(directory)
    catalog_removed = False
    if purge_catalog:
        catalog_removed = catalog_mod.remove_entry(root, project_id, actor=actor).get("removed", False)
    return {"project_id": project_id, "deleted": True, "catalog_removed": catalog_removed}


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
