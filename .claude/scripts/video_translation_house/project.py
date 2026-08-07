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


# The only target languages that get a human-filled worksheet + a per-language translation_qa
# human gate are the SOURCE language and English. Every other translatable target is
# AI-auto-translated with deterministic QA only (marked ``auto_translate`` on its track, which
# state.transition_blockers consults to skip the human gate). Keeping this in one place lets
# init_project and add_languages mark tracks identically.
_HUMAN_REVIEW_LANG = "en"


def _is_auto_translate(lang: str, source_language: str | None) -> bool:
    """True when ``lang`` should be AI-auto-translated (no human worksheet/gate).

    A track is human-reviewed iff it is the source language (which actually skips translation
    entirely, rule 7) or English; anything else is auto-translated.
    """
    if source_language and lang == source_language:
        return False  # source track skips translation; not "auto-translated"
    return lang != _HUMAN_REVIEW_LANG


# Subpaths under transcript/ preserved by a keep-transcript reset: the source ASR transcript
# doc(s) (source.<lang>.json) and the engine's raw output. Everything else in transcript/
# (english-gloss*, qa-report.json) is downstream review work and is wiped.
_TRANSCRIPT_KEEP_GLOBS = ("source.*.json", "engine")


# Per-language still-image files must be a readable image (by suffix). A talking-head speech can
# be replaced by one fixed frame per language, with the dub over it (TASK 1).
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _parse_lang_map(raw: str, *, kind: str) -> dict[str, str]:
    """Parse a ``lang=value`` comma-map CLI arg (e.g. ``en=/p/a.jpg,ur=/p/b.png``).

    The single-shot per-language-value convention (mirrors how a single value would be passed
    but for many languages at once). Whitespace-tolerant; empty string => empty map. Raises
    ConfigurationError on a malformed entry (missing ``=`` or empty lang/value).
    """
    from .langid import normalize_language

    out: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ConfigurationError(f"malformed {kind} entry {chunk!r} — expected lang=value")
        lang_raw, _, value = chunk.partition("=")
        lang_raw, value = lang_raw.strip(), value.strip()
        if not lang_raw or not value:
            raise ConfigurationError(f"malformed {kind} entry {chunk!r} — empty lang or value")
        norm = normalize_language(lang_raw)
        if norm is None:
            raise ConfigurationError(f"unrecognized language code in {kind}: {lang_raw!r}")
        out[norm] = value
    return out


# Public alias for the CLI layer (parses --images / --speeds lang=value maps).
parse_lang_map = _parse_lang_map


def _validate_image_path(raw_path: str) -> str:
    """Validate a still-image path (exists, readable-image suffix) and return it absolute."""
    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = p.resolve()
    if not p.is_file():
        raise ConfigurationError(f"still image not found: {raw_path}")
    if p.suffix.lower() not in _IMAGE_SUFFIXES:
        raise ConfigurationError(
            f"still image {raw_path} must be one of {sorted(_IMAGE_SUFFIXES)} (got {p.suffix!r})")
    return str(p)


def _validate_speed_factor(value: Any) -> float:
    """Validate a playback-speed factor: a positive float in a sane band (0.25–4.0)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ConfigurationError(f"speed factor must be a number, got {value!r}") from None
    if not (0.25 <= f <= 4.0):
        raise ConfigurationError(f"speed factor must be in 0.25–4.0, got {f}")
    return f


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
    dub_voice_gender: str = "male",
    voice_clone_requested: bool = False,
    voice_clone: bool | None = None,
    mux_mode: str = "soft-subs",
    glossary_id: str | None = None,
    images: dict[str, str] | None = None,
    playback_speed: dict[str, float] | None = None,
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

    # Rights/clone posture (rule-5 authorized override, see company.default.json "rights").
    # Voice cloning defaults ON company-wide; the caller can opt out per project (voice_clone
    # False) restoring the neutral-voice default. Resolution order: explicit arg -> company
    # default_voice_clone -> False.
    rights_policy = company.get("rights", {}) or {}
    if voice_clone is None:
        resolved_clone = bool(rights_policy.get("default_voice_clone", False))
    else:
        resolved_clone = bool(voice_clone)

    if mux_mode not in ("soft-subs", "burned-in", "no-subs"):
        raise ConfigurationError(
            f"mux_mode must be one of soft-subs/burned-in/no-subs, got {mux_mode!r}"
        )

    # Per-language still image (TASK 1) + playback speed (TASK 3). Both are per-language maps
    # keyed by target language; a language absent from either map keeps the default (source
    # video / 1.0x). Validate paths/factors here so a bad value fails at init, not at mux.
    resolved_images: dict[str, str] = {}
    for lang, path in (images or {}).items():
        if lang not in target_languages:
            raise ConfigurationError(f"image language {lang!r} is not a target language")
        resolved_images[lang] = _validate_image_path(path)
    resolved_speeds: dict[str, float] = {}
    for lang, factor in (playback_speed or {}).items():
        if lang not in target_languages:
            raise ConfigurationError(f"speed language {lang!r} is not a target language")
        f = _validate_speed_factor(factor)
        if abs(f - 1.0) >= 1e-6:  # 1.0x is the default; no need to store it
            resolved_speeds[lang] = f

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
        # Male is the hard default dub voice gender for every language (TASK 2); the human
        # confirms/overrides it at /new-video. `voice_clone` is the actual clone switch; it
        # defaults ON via company policy (rule-5 authorized override, opt out with --no-clone).
        # Cloning ALSO requires recorded voice_clone_consent in the RIGHTS record (rule 5) — when
        # clone is on and company auto_consent_at_init is set, that consent is recorded below
        # through the sanctioned `rights set` path (never hand-written). `voice_clone_requested`
        # remains the advisory onboarding intent flag; it's forced True whenever clone is on.
        "dubbing": {
            "voice_clone": resolved_clone,
            "voice_gender": (dub_voice_gender if dub_voice_gender in ("male", "female") else "male"),
            "voice_clone_requested": bool(voice_clone_requested) or resolved_clone,
        },
        "glossary_id": glossary_id,
        "mux_mode": mux_mode,
        # Per-language still-image display (TASK 1) and deliberate uniform playback speed (TASK 3);
        # empty maps = every language keeps the source video at 1.0x. Adjustable post-init via
        # `project set-image` / `project set-speed`.
        "images": resolved_images,
        "playback_speed": resolved_speeds,
        "join_clips": bool(join_clips),
        "selection": selection,
        "created_at": utc_now(),
    }
    errors = validate_data(root, project_cfg, "project.schema.json")
    if errors:
        raise ConfigurationError("project.yaml invalid: " + "; ".join(errors))

    # source_language is unknown at init (set later at LANGUAGE_ID); a track is human-reviewed
    # iff it's English, everything else is AI-auto-translated (rule: worksheet+gate only for
    # source+en). If the source language turns out to also be a target, langid.set_language
    # marks it skip_translation and its (harmless) auto_translate flag is ignored downstream.
    tracks = {
        lang: {
            "stage": "TRANSLATION",
            "status": "pending",
            "dub_enabled": lang in dub_langs,
            "updated_at": utc_now(),
            **({"auto_translate": True} if _is_auto_translate(lang, None) else {}),
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

    # Auto-record the rights posture at init (rule-5 authorized override — see company.default.json
    # "rights"). ONLY through the sanctioned rights writer (rule 1: the CLI owns the rights record;
    # never hand-write rights/record.json). We record consent when cloning is on AND the company
    # opts into init-time consent; the rights_status is set from company policy the same way.
    # set_rights takes its own lock + appends RIGHTS_SET, so it runs after the init lock releases.
    # A later human `rights set` at the rights-check gate still fully overrides this (set_rights
    # overwrites), so this is a pre-population, not a lock-in.
    rights_record = None
    auto_consent = bool(rights_policy.get("auto_consent_at_init", False))
    if resolved_clone and auto_consent:
        from . import rights as rights_mod

        rights_record = rights_mod.set_rights(
            root, project_id,
            status=rights_policy.get("default_rights_status", "self-authored"),
            reviewer=rights_policy.get("default_reviewer", "company-standing-authorization"),
            voice_clone_consent=True,
            notes="auto-recorded at init under company standing authorization (rule-5 override)",
        )

    return {
        "project_id": project_id,
        "state": state,
        "config": project_cfg,
        "rights": rights_record,
    }


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
            elif _is_auto_translate(lang, source_language):
                # Non-source, non-English target: AI-auto-translated, deterministic QA only,
                # no per-language human translation_qa gate (worksheet+gate stay source+en).
                track["auto_translate"] = True
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


def sync_scope(
    root: Path,
    project_id: str,
    *,
    actor: str = "human",
) -> dict[str, Any]:
    """Reconcile the two-axis review markers (``skip_translation`` / ``auto_translate``) on the
    tracks of a project created BEFORE the two-axis feature existed.

    Recomputes both markers for every track from the confirmed ``source_language`` and the fixed
    ``en`` human-review rule (the single ``_is_auto_translate`` helper, so init/add-languages/
    sync-scope agree): the source track becomes ``skip_translation`` (rule 7), ``en`` and the
    source stay human-reviewed (no ``auto_translate``), every other translatable target becomes
    ``auto_translate``. It is a true recompute — a marker that no longer applies is *removed*, not
    just left in place.

    It NEVER touches ``dub_enabled``, stages, statuses, artifacts, or approvals — a recorded
    translation_qa approval on ``en`` stays valid (rule 6). Idempotent: when the markers already
    match, it is a no-op (no state change, no event). Requires a confirmed ``source_language``.
    """
    paths = ProjectPaths(root, project_id).require()

    with project_lock(paths.lock):
        state = load_json(paths.state)
        source_language = state.get("source_language")
        if not source_language:
            raise ConfigurationError(
                "source_language is not set yet — run `langid set` before `sync-scope`"
            )
        tracks = state.get("language_tracks", {})

        marked_auto: list[str] = []
        marked_skip: list[str] = []
        cleared: list[str] = []
        changed = False
        for lang, track in tracks.items():
            want_skip = lang == source_language
            want_auto = _is_auto_translate(lang, source_language)

            had_skip = bool(track.get("skip_translation"))
            had_auto = bool(track.get("auto_translate"))
            track_changed = False
            if want_skip and not had_skip:
                track["skip_translation"] = True
                marked_skip.append(lang)
                track_changed = True
            elif not want_skip and had_skip:
                track.pop("skip_translation", None)
                cleared.append(lang)
                track_changed = True
            if want_auto and not had_auto:
                track["auto_translate"] = True
                marked_auto.append(lang)
                track_changed = True
            elif not want_auto and had_auto:
                track.pop("auto_translate", None)
                cleared.append(lang)
                track_changed = True
            if track_changed:
                track["updated_at"] = utc_now()
                changed = True

        if not changed:
            return {
                "project_id": project_id,
                "marked_auto": [],
                "marked_skip": [],
                "cleared": [],
            }

        state["updated_at"] = utc_now()
        state["updated_by"] = actor
        require_valid(root, state, "state.schema.json")
        atomic_write_json(paths.state, state)
        append_event(
            paths.events, project_id, "SCOPE_SYNCED", actor,
            {"marked_auto": marked_auto, "marked_skip": marked_skip, "cleared": cleared},
        )

    return {
        "project_id": project_id,
        "marked_auto": marked_auto,
        "marked_skip": marked_skip,
        "cleared": cleared,
    }


def enable_dub(
    root: Path,
    project_id: str,
    *,
    target_languages: list[str],
    actor: str = "human",
    disable: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Turn dubbing on (or, with ``disable=True``, off) for language tracks that already exist.

    The sanctioned "add es dubbing for <id>" path (rule 1: mutates state + config through the
    atomic+validated CLI path, never by hand). Flips ``dub_enabled`` on existing tracks, adds/
    removes them from the project config ``audio_languages``, and logs ``DUB_ENABLED`` /
    ``DUB_DISABLED``. It does NOT create tracks or fetch anything — dubbing reuses the
    already-produced ``captions/captions.<lang>.json`` (a non-clone dub has no source-video
    dependency). If a requested language has no track yet, that's an error directing the caller to
    ``add-languages`` first (which creates the track).

    Idempotent: languages already in the requested state are ignored; a request that changes
    nothing is a no-op (no state change, no event).

    ``disable=True`` refuses to strand a produced dub: if a track has an active ``dub-wav@<lang>``
    artifact, disabling raises unless ``force=True`` (rule 6).
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

    with project_lock(paths.lock):
        state = load_json(paths.state)
        tracks = state.get("language_tracks", {})

        missing = [lang for lang in requested if lang not in tracks]
        if missing:
            raise ConfigurationError(
                "no track for language(s): " + ", ".join(missing)
                + " — add them first with `project add-languages`"
            )

        target_enabled = not disable
        changing = [
            lang for lang in requested
            if bool(tracks[lang].get("dub_enabled")) != target_enabled
        ]
        if not changing:
            return {
                "project_id": project_id,
                "enabled": [] if not disable else None,
                "disabled": [] if disable else None,
                "audio_languages": list(load_yaml(paths.config).get("audio_languages", [])),
            }

        if disable and not force:
            active = state.get("active_artifacts", {})
            stranded = [lang for lang in changing if active.get(f"dub-wav@{lang}")]
            if stranded:
                raise ConfigurationError(
                    "refusing to disable dubbing for language(s) with a produced dub: "
                    + ", ".join(stranded)
                    + " — a dub-wav artifact exists; re-run with --force to override (rule 6)"
                )

        for lang in changing:
            tracks[lang]["dub_enabled"] = target_enabled
            tracks[lang]["updated_at"] = utc_now()
        state["updated_at"] = utc_now()
        state["updated_by"] = actor
        require_valid(root, state, "state.schema.json")
        atomic_write_json(paths.state, state)

        cfg = load_yaml(paths.config)
        cfg_audio = list(cfg.get("audio_languages", []))
        if disable:
            cfg["audio_languages"] = [lang for lang in cfg_audio if lang not in changing]
        else:
            cfg["audio_languages"] = cfg_audio + [lang for lang in changing if lang not in cfg_audio]
        errors = validate_data(root, cfg, "project.schema.json")
        if errors:
            raise ConfigurationError("project.yaml invalid after enable-dub: " + "; ".join(errors))
        atomic_write_yaml(paths.config, cfg)

        event = "DUB_DISABLED" if disable else "DUB_ENABLED"
        key = "disabled" if disable else "enabled"
        append_event(paths.events, project_id, event, actor, {key: changing})

    return {
        "project_id": project_id,
        "enabled": None if disable else changing,
        "disabled": changing if disable else None,
        "audio_languages": list(cfg["audio_languages"]),
    }


def set_image(
    root: Path,
    project_id: str,
    *,
    language: str,
    path: str | None = None,
    clear: bool = False,
    actor: str = "human",
) -> dict[str, Any]:
    """Set / change / clear the per-language still image on an existing project (TASK 1).

    Some languages show one fixed image for the whole runtime with the dub over it instead of
    the source video (different image per language; some keep the source video). This writes the
    ``images`` map in ``project.yaml`` (rule 1: through the atomic+validated CLI path, never by
    hand) and appends an ``IMAGE_SET`` event. ``clear=True`` (or ``path`` None) removes the
    language's image, reverting it to the source-video picture. The image applies to the
    dub-enabled mux path; a caption-only track has no dub to lay over an image.
    """
    from .langid import normalize_language

    paths = ProjectPaths(root, project_id).require()
    norm = normalize_language(language)
    if norm is None:
        raise ConfigurationError(f"Unrecognized language code: {language!r}")

    with project_lock(paths.lock):
        cfg = load_yaml(paths.config)
        if norm not in cfg.get("target_languages", []):
            raise ConfigurationError(f"{norm!r} is not a target language of {project_id}")
        images = dict(cfg.get("images") or {})
        if clear or path is None:
            removed = images.pop(norm, None)
            action = "cleared"
            value = removed
        else:
            value = _validate_image_path(path)
            images[norm] = value
            action = "set"
        cfg["images"] = images
        errors = validate_data(root, cfg, "project.schema.json")
        if errors:
            raise ConfigurationError("project.yaml invalid after set-image: " + "; ".join(errors))
        atomic_write_yaml(paths.config, cfg)
        append_event(paths.events, project_id, "IMAGE_SET", actor,
                     {"language": norm, "action": action, "path": value})

    return {"project_id": project_id, "language": norm, "action": action,
            "image": value if action == "set" else None, "images": images}


def set_speed(
    root: Path,
    project_id: str,
    *,
    language: str,
    factor: float,
    actor: str = "human",
) -> dict[str, Any]:
    """Set the per-language deliberate playback speed on an existing project (TASK 3).

    A uniform whole-video speed change applied at mux (audio AND video scaled by the same factor
    — NOT the per-cue rubber-banding rule 5 forbids). Writes the ``playback_speed`` map in
    ``project.yaml`` (rule 1) and appends a ``SPEED_SET`` event. ``factor`` 1.0 resets the
    language to default speed (removes it from the map).
    """
    from .langid import normalize_language

    paths = ProjectPaths(root, project_id).require()
    norm = normalize_language(language)
    if norm is None:
        raise ConfigurationError(f"Unrecognized language code: {language!r}")
    f = _validate_speed_factor(factor)

    with project_lock(paths.lock):
        cfg = load_yaml(paths.config)
        if norm not in cfg.get("target_languages", []):
            raise ConfigurationError(f"{norm!r} is not a target language of {project_id}")
        speeds = dict(cfg.get("playback_speed") or {})
        if abs(f - 1.0) < 1e-6:
            speeds.pop(norm, None)
            stored: float | None = None
        else:
            speeds[norm] = f
            stored = f
        cfg["playback_speed"] = speeds
        errors = validate_data(root, cfg, "project.schema.json")
        if errors:
            raise ConfigurationError("project.yaml invalid after set-speed: " + "; ".join(errors))
        atomic_write_yaml(paths.config, cfg)
        append_event(paths.events, project_id, "SPEED_SET", actor,
                     {"language": norm, "factor": stored if stored is not None else 1.0})

    return {"project_id": project_id, "language": norm,
            "factor": stored if stored is not None else 1.0, "playback_speed": speeds}


def redub_track(
    root: Path,
    project_id: str,
    *,
    target_languages: list[str],
    actor: str = "agent",
) -> dict[str, Any]:
    """Scoped rewind so ONE (or a few) dub track(s) can be re-rendered, preserving the rest.

    The narrow alternative to ``reset_project`` when a single language's dub needs to be
    re-run after ``AUDIO_QA_GATE`` (e.g. a freeze/trim-plan fix) but the other languages'
    dubs, captions, artifacts, and approvals are correct and must NOT be discarded. Unlike
    ``reset_project`` (which wipes ALL downstream work and every track), this:

      * rewinds only the top ``current_state`` back to ``AUDIO_SYNC_ADJUST`` (a dub-allowed
        state) when the project has advanced past it, so ``dub run`` is permitted again;
      * resets ONLY the named track(s) to the dubbing stage (``AUDIO_SYNC_ADJUST`` /
        ``in_progress``), leaving every other track's stage/status untouched;
      * leaves ALL artifacts and approvals in place. The re-``dub run`` registers a fresh
        ``dub-wav@<lang>`` that supersedes the old one; rule 6 auto-invalidates the stale
        ``audio_qa`` approval bound to the superseded hash — no approval is hand-cleared here.

    It is the CLI-owned, atomic+validated rewind (rule 1); the caller runs ``dub run`` +
    ``dub qa`` afterward and re-surfaces the per-language ``audio_qa`` gate (rule 13). Only
    ``dub_enabled`` tracks are eligible (a captions-only track has no dub to re-render).
    Idempotent-ish: re-running when already at/behind ``AUDIO_SYNC_ADJUST`` with the named
    track already reset just re-stamps and logs.
    """
    from . import state as state_mod
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

    states = state_mod.workflow_config(root).get("states", [])
    rewind_to = "AUDIO_SYNC_ADJUST"
    if rewind_to not in states:  # defensive; the workflow always defines it
        raise ConfigurationError(f"redub rewind target is not a valid state: {rewind_to!r}")

    with project_lock(paths.lock):
        state = load_json(paths.state)
        tracks = state.get("language_tracks", {})

        missing = [lang for lang in requested if lang not in tracks]
        if missing:
            raise ConfigurationError(
                "no track for language(s): " + ", ".join(missing)
                + " — add them first with `project add-languages`"
            )
        not_dubbed = [lang for lang in requested if not bool(tracks[lang].get("dub_enabled"))]
        if not_dubbed:
            raise ConfigurationError(
                "refusing to redub captions-only language(s): " + ", ".join(not_dubbed)
                + " — enable dubbing first with `project enable-dub`"
            )

        current = state.get("current_state")
        cur_idx = states.index(current) if current in states else -1
        rewind_idx = states.index(rewind_to)
        rewound_from: str | None = None
        # Only pull the top state *backward* to a dub-allowed state; never push it forward
        # (that is the pipeline's job through the normal gated transitions).
        if cur_idx > rewind_idx:
            rewound_from = current
            state["previous_state"] = current
            state["current_state"] = rewind_to

        for lang in requested:
            tracks[lang]["stage"] = "AUDIO_SYNC_ADJUST"
            tracks[lang]["status"] = "in_progress"
            tracks[lang]["updated_at"] = utc_now()

        state["updated_at"] = utc_now()
        state["updated_by"] = actor
        require_valid(root, state, "state.schema.json")
        atomic_write_json(paths.state, state)

        append_event(paths.events, project_id, "TRACK_REDUB_REQUESTED", actor, {
            "languages": requested,
            "rewound_from": rewound_from,
            "current_state": rewind_to,
        })

    return {
        "project_id": project_id,
        "redub_languages": requested,
        "current_state": rewind_to,
        "rewound_from": rewound_from,
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
