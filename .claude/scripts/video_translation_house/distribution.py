"""Phase-6 distribution & promotion (READY_FOR_REVIEW -> ... -> MONITORING).

The deterministic (no-LLM) orchestration side of publishing. It prepares packets, drives the
sanctioned uploader in net/publish.py, and records manifests — but never grants an approval
and never publishes on its own initiative:

  * run_platform_packaging  (PLATFORM_PACKAGING)  builds distribution/platform-package.json:
    per target language/channel, templated title/description (chapter timecodes appended),
    tags, resolved channel, and the EXACT final-video artifact hash it binds to. Renders a
    per-project how-to guide per target. Writes the `platform-package` gate report. Advances
    to RELEASE_AUTHORIZATION (a HUMAN-ONLY gate).
  * run_youtube_upload      (YOUTUBE_UPLOAD)       requires the release_authorization approval
    already granted for this state's edge; idempotent on (channel, video hash); calls
    net.publish.youtube_upload (dry_run default). Records distribution/upload-manifest.json.
  * run_promotion_queue     (PROMOTION_QUEUE)      drafts one DISTINCT post per automatable
    platform + checklist entries for manual platforms; writes distribution/promotion-manifest
    .json + the `promotion` gate report. Advances to PROMOTION_REVIEW (HUMAN-ONLY gate).
  * run_promotion_publish   (PROMOTION_PUBLISHED)  fires automatable posts via net.publish.*
    (dry_run default), marks manual platforms as checklist items, records outcomes, advances
    to MONITORING (terminal).

Rights are re-checked at the RELEASE_AUTHORIZATION->YOUTUBE_UPLOAD edge (a `rights` report in
gate_reports); packaging refuses to bind a non-distributable release into an upload target.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import artifacts as artifacts_mod
from . import chapters as chapters_mod
from . import state as state_mod
from .approvals import has_valid_approval
from .errors import ConfigurationError, DistributionError
from .events import append_event
from .net import publish as publish_mod
from .packaging import _active_artifact, _source_provenance
from .paths import ProjectPaths
from .rights import check_rights
from .translate import (
    SCHEMA_VERSION,
    _active_track_langs,
    _rel,
    _write_gate_report,
)
from .util import atomic_write_json, atomic_write_text, load_json, load_yaml, project_lock, utc_now
from .validation import require_valid

# States each step may run from (mirrors packaging._STATES_ALLOWING_*).
_STATES_ALLOWING_PACKAGING = {"READY_FOR_REVIEW", "PLATFORM_PACKAGING"}
_STATE_UPLOAD = "YOUTUBE_UPLOAD"
_STATES_ALLOWING_PROMO_QUEUE = {"YOUTUBE_UPLOAD", "PROMOTION_QUEUE"}
_STATE_PROMO_PUBLISH = "PROMOTION_REVIEW"  # publish runs on the PROMOTION_REVIEW->PUBLISHED edge


# --- config loaders ----------------------------------------------------------

def _config_dir(root: Path) -> Path:
    return root / ".claude" / "config"


def load_channels_config(root: Path) -> dict[str, Any]:
    path = _config_dir(root) / "channels.config.json"
    if not path.is_file():
        raise ConfigurationError("missing .claude/config/channels.config.json")
    return load_json(path)


def load_promotion_config(root: Path) -> dict[str, Any]:
    path = _config_dir(root) / "promotion.config.json"
    if not path.is_file():
        raise ConfigurationError("missing .claude/config/promotion.config.json")
    return load_json(path)


def _project_cfg(paths: ProjectPaths) -> dict[str, Any]:
    return load_yaml(paths.config) or {}


def _resolve_channel(
    channels_cfg: dict[str, Any], language: str, project_override: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Pick the YouTube channel for a language: per-project override wins, else the
    default:true channel for that language, else the first channel for that language.

    Returns a channel dict (config entry, possibly with channelId overridden) or None."""
    channels = [c for c in channels_cfg.get("channels", []) if c.get("platform") == "youtube"]
    lang_channels = [c for c in channels if c.get("language") == language]
    chosen: dict[str, Any] | None = None
    if lang_channels:
        chosen = next((c for c in lang_channels if c.get("default")), lang_channels[0])
    if project_override and project_override.get("channelId"):
        base = dict(chosen) if chosen else {"id": "project-override", "platform": "youtube",
                                            "language": language, "credentials_ref": "YT_DEFAULT",
                                            "privacy_default": "private", "category_id": "25",
                                            "playlist_id": None}
        base = dict(base)
        base["channelId"] = project_override["channelId"]
        return base
    return chosen


# --- 1. platform packaging ---------------------------------------------------

def _final_video_artifact(root: Path, project_id: str, language: str) -> dict[str, Any] | None:
    """The deliverable video hash a release binds to: the active dubbed-video@lang if the
    track was dubbed, else None (caption-only tracks have no standalone deliverable video)."""
    return _active_artifact(root, project_id, "dubbed-video", language)


def _render_title(template_ctx: dict[str, Any], language: str) -> str:
    base = template_ctx.get("title") or template_ctx.get("project_id", "")
    # Distinguish language editions without inventing copy the human hasn't approved.
    return f"{base} [{language}]" if base else language


def _render_description(template_ctx: dict[str, Any], chapters_block: str) -> str:
    parts: list[str] = []
    summary = template_ctx.get("summary")
    if summary:
        parts.append(summary)
    src_url = template_ctx.get("source_url")
    if src_url:
        parts.append(f"Source: {src_url}")
    if chapters_block:
        parts.append("Chapters:\n" + chapters_block)
    parts.append("Translated & captioned by publish-vid-trans.")
    return "\n\n".join(parts)


def _guide_template(root: Path, name: str) -> str | None:
    path = root / "docs" / "guides" / name
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


def _render_project_guide(
    root: Path, paths: ProjectPaths, target: dict[str, Any],
) -> str | None:
    """Render a per-project how-to guide for one target from the generic template, filling
    video-specific fields. Returns the project-relative path, or None if no template."""
    template = _guide_template(root, "youtube-upload.md")
    if template is None:
        return None
    filled = template
    replacements = {
        "{{TITLE}}": target.get("title", ""),
        "{{DESCRIPTION}}": target.get("description", ""),
        "{{CHANNEL_ID}}": target.get("channel_id") or "(resolve at upload)",
        "{{PRIVACY}}": target.get("privacy", "private"),
        "{{LANGUAGE}}": target.get("language", ""),
        "{{VIDEO_PATH}}": target.get("video_path") or "",
        "{{VIDEO_SHA256}}": target.get("video_sha256", ""),
    }
    for token, value in replacements.items():
        filled = filled.replace(token, str(value))
    guides_dir = paths.distribution_dir / "guides"
    guide_path = guides_dir / f"youtube-{target['language']}.md"
    with project_lock(paths.lock):
        guides_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(guide_path, filled if filled.endswith("\n") else filled + "\n")
    return _rel(guide_path, paths)


def run_platform_packaging(
    root: Path,
    project_id: str,
    *,
    actor: str = "agent",
    advance: bool = False,
) -> dict[str, Any]:
    """Build distribution/platform-package.json for every dub-enabled language track.

    Each target binds to the exact active dubbed-video hash, appends chapter timecodes to a
    templated description, resolves a channel, and renders a per-project guide. Writes the
    `platform-package` gate report (PASS iff every target has a bound video hash + resolvable
    channel). Advances PLATFORM_PACKAGING -> RELEASE_AUTHORIZATION only if `advance` (subject
    to the human release_authorization gate, which will block the transition)."""
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    if state["current_state"] not in _STATES_ALLOWING_PACKAGING:
        raise ConfigurationError(
            f"platform packaging expects the project at READY_FOR_REVIEW/PLATFORM_PACKAGING, "
            f"is at {state['current_state']}"
        )

    rights = check_rights(root, project_id)
    distributable = bool(rights.get("distributable"))
    rights_status = str(rights.get("rights_status", "unreviewed"))

    cfg = _project_cfg(paths)
    prov = _source_provenance(paths)
    channels_cfg = load_channels_config(root)
    project_dist = (cfg.get("distribution") or {}).get("youtube") or {}
    template_ctx = {
        "project_id": project_id,
        "title": (cfg.get("source") or {}).get("title") or prov.get("title"),
        "source_url": (cfg.get("source") or {}).get("url") or prov.get("url"),
        "summary": (cfg.get("distribution") or {}).get("summary"),
    }
    default_tags = (cfg.get("distribution") or {}).get("tags") or []

    # Only dub-enabled tracks have a standalone deliverable video to upload.
    tracks = state.get("language_tracks", {})
    langs = [lang for lang in _active_track_langs(state)
             if tracks.get(lang, {}).get("dub_enabled")]

    targets: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for lang in langs:
        video_artifact = _final_video_artifact(root, project_id, lang)
        if not video_artifact:
            findings.append({"severity": "blocker", "category": "package-no-video",
                             "summary": f"[{lang}] no active dubbed-video artifact to bind.",
                             "language": lang})
            continue
        chapters_doc = chapters_mod.load_chapters(root, project_id, lang)
        chapters_block = (chapters_mod.render_youtube_description_timecodes(chapters_doc)
                          if chapters_doc else "")
        channel = _resolve_channel(channels_cfg, lang, project_dist)
        if not channel:
            findings.append({"severity": "major", "category": "package-no-channel",
                             "summary": f"[{lang}] no channel resolved for language; upload "
                                        f"target cannot be authorized.", "language": lang})
        title = _render_title(template_ctx, lang)
        description = _render_description(template_ctx, chapters_block)
        caption_paths = []
        for fmt in ("srt", "vtt"):
            cap = paths.captions_dir / f"captions.{lang}.{fmt}"
            if cap.is_file():
                caption_paths.append(_rel(cap, paths))
        target = {
            "language": lang,
            "platform": "youtube",
            "channel_id": (channel or {}).get("channelId"),
            "channel_config_id": (channel or {}).get("id"),
            "title": title,
            "description": description,
            "tags": list(default_tags),
            "category_id": (channel or {}).get("category_id"),
            "privacy": (channel or {}).get("privacy_default", "private"),
            "playlist_id": (channel or {}).get("playlist_id"),
            "video_path": video_artifact["path"],
            "video_sha256": video_artifact["sha256"],
            "caption_paths": caption_paths,
            "thumbnail_path": None,
            "chapters_timecodes": chapters_block or None,
        }
        target["guide_path"] = _render_project_guide(root, paths, target)
        targets.append(target)

    if not targets:
        findings.append({"severity": "blocker", "category": "package-empty",
                         "summary": "no dub-enabled tracks with a deliverable video to package."})
    if not distributable:
        findings.append({"severity": "blocker", "category": "rights-not-distributable",
                         "summary": f"rights_status {rights_status!r} is not distributable; "
                                    f"a human must clear rights before release."})

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "created_at": utc_now(),
        "created_by": actor,
        "rights_status": rights_status,
        "distributable": distributable,
        "targets": targets,
    }
    require_valid(root, manifest, "platform-package-manifest.schema.json")
    with project_lock(paths.lock):
        paths.platform_package_manifest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(paths.platform_package_manifest, manifest)
    artifact = artifacts_mod.register_artifact(
        root, project_id, paths.platform_package_manifest, "platform-package",
        "PLATFORM_PACKAGING", actor,
    )

    decision = "FAIL" if any(f["severity"] == "blocker" for f in findings) else (
        "CONDITIONAL_PASS" if any(f["severity"] == "major" for f in findings) else "PASS")
    gate_path = _write_gate_report(
        root, project_id, "platform-package", decision, findings,
        {"targets": len(targets), "languages": langs, "distributable": distributable}, actor,
    )
    append_event(paths.events, project_id, "PLATFORM_PACKAGED", actor, {
        "targets": len(targets), "decision": decision,
        "manifest_sha256": artifact["sha256"],
    })

    advanced = state["current_state"]
    if advance and state["current_state"] == "PLATFORM_PACKAGING":
        # Human gate blocks this; surfaces as a blocker rather than transitioning.
        blockers = state_mod.transition_blockers(root, project_id, "PLATFORM_PACKAGING",
                                                  "RELEASE_AUTHORIZATION")
        if not blockers:
            state_mod.transition(root, project_id, "RELEASE_AUTHORIZATION", actor,
                                 reason="platform package prepared")
            advanced = "RELEASE_AUTHORIZATION"

    return {"manifest": _rel(paths.platform_package_manifest, paths), "artifact": artifact,
            "targets": targets, "decision": decision, "report": _rel(gate_path, paths),
            "distributable": distributable, "rights_status": rights_status,
            "advanced_to": advanced}


def load_platform_package(root: Path, project_id: str) -> dict[str, Any]:
    paths = ProjectPaths(root, project_id).require()
    if not paths.platform_package_manifest.is_file():
        raise DistributionError("no platform package; run `distribute package` first")
    return load_json(paths.platform_package_manifest)


# --- 2. youtube upload -------------------------------------------------------

def _load_upload_manifest(paths: ProjectPaths, project_id: str) -> dict[str, Any]:
    if paths.upload_manifest.is_file():
        return load_json(paths.upload_manifest)
    return {"schema_version": SCHEMA_VERSION, "project_id": project_id, "uploads": []}


def run_youtube_upload(
    root: Path,
    project_id: str,
    language: str,
    *,
    dry_run: bool = True,
    actor: str = "agent",
    advance: bool = False,
) -> dict[str, Any]:
    """Upload one language's deliverable video to YouTube (dry_run default = no network).

    Requires the project at YOUTUBE_UPLOAD (i.e. the human release_authorization gate has
    already been passed to reach this state). Idempotent: a completed upload for this exact
    (channel, video hash) is skipped. Records the attempt/outcome in the upload manifest."""
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    if state["current_state"] != _STATE_UPLOAD:
        raise ConfigurationError(
            f"youtube upload expects the project at {_STATE_UPLOAD} (past the "
            f"release_authorization gate), is at {state['current_state']}"
        )

    package = load_platform_package(root, project_id)
    target = next((t for t in package["targets"] if t["language"] == language), None)
    if target is None:
        raise DistributionError(f"no packaged target for language {language!r}; run `distribute package`")

    # Rights re-check (belt-and-braces; the RELEASE_AUTHORIZATION->YOUTUBE_UPLOAD edge also
    # carries a `rights` gate report, but this state was already entered so guard here too).
    rights = check_rights(root, project_id)
    if not rights.get("distributable"):
        raise DistributionError(
            f"rights_status {rights.get('rights_status')!r} is not distributable; refusing upload"
        )

    # Bind to the CURRENT active video hash; refuse if the package's hash was superseded.
    live_artifact = _final_video_artifact(root, project_id, language)
    if not live_artifact or live_artifact["sha256"] != target["video_sha256"]:
        raise DistributionError(
            f"[{language}] packaged video hash {target['video_sha256']} no longer matches the "
            f"active deliverable; re-run `distribute package` after any edit"
        )

    manifest = _load_upload_manifest(paths, project_id)
    channel_id = target.get("channel_id")
    for rec in manifest["uploads"]:
        if (rec["platform"] == "youtube" and rec["video_sha256"] == target["video_sha256"]
                and rec.get("channel_id") == channel_id and rec.get("status") == "uploaded"):
            return {"skipped": True, "reason": "already uploaded", "record": rec,
                    "advanced_to": state["current_state"]}

    video_path = paths.directory / target["video_path"]
    caption_files = [(language, paths.directory / cp) for cp in target.get("caption_paths", [])
                     if cp.endswith(".srt")]
    channel_cfg_id = target.get("channel_config_id")
    credentials_ref = "YT_DEFAULT"
    for ch in load_channels_config(root).get("channels", []):
        if ch.get("id") == channel_cfg_id:
            credentials_ref = ch.get("credentials_ref", "YT_DEFAULT")
            break

    result = publish_mod.youtube_upload(
        video_path=video_path,
        title=target["title"],
        description=target["description"],
        tags=target.get("tags"),
        category_id=target.get("category_id") or "25",
        privacy=target.get("privacy", "private"),
        channel_id=channel_id,
        credentials_ref=credentials_ref,
        caption_files=caption_files,
        playlist_id=target.get("playlist_id"),
        dry_run=dry_run,
    )

    record = {
        "platform": "youtube",
        "language": language,
        "channel_id": channel_id,
        "video_sha256": target["video_sha256"],
        "dry_run": bool(dry_run),
        "status": "prepared" if dry_run else result.get("status", "uploaded"),
        "video_id": result.get("video_id"),
        "url": result.get("url"),
        "privacy": target.get("privacy"),
        "request_preview": result.get("request_preview"),
        "error": None,
        "created_at": utc_now(),
        "created_by": actor,
    }
    manifest["uploads"].append(record)
    require_valid(root, manifest, "upload-record.schema.json")
    with project_lock(paths.lock):
        paths.upload_manifest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(paths.upload_manifest, manifest)
    append_event(paths.events, project_id, "YOUTUBE_UPLOAD_RECORDED", actor, {
        "language": language, "dry_run": bool(dry_run), "status": record["status"],
        "video_id": record["video_id"],
    })

    advanced = state["current_state"]
    if advance and not dry_run and state["current_state"] == _STATE_UPLOAD:
        state_mod.transition(root, project_id, "PROMOTION_QUEUE", actor,
                             reason="youtube upload complete")
        advanced = "PROMOTION_QUEUE"

    return {"record": record, "dry_run": bool(dry_run), "video_url": record["url"],
            "advanced_to": advanced}


# --- 3. promotion queue ------------------------------------------------------

def _render_message(template: str, ctx: dict[str, Any]) -> str:
    out = template
    for key in ("title", "url", "summary", "hashtags"):
        out = out.replace("{" + key + "}", str(ctx.get(key, "")))
    return out.strip()


def run_promotion_queue(
    root: Path,
    project_id: str,
    *,
    actor: str = "agent",
    advance: bool = False,
) -> dict[str, Any]:
    """Draft one DISTINCT promotional post per platform + checklist entries for manual ones.

    Automatable, enabled platforms get an API-ready message from their template; manual (or
    disabled) platforms get a `checklist` entry pointing at their how-to guide. Distinct text
    per platform is an anti-spam guardrail (schema-described). Writes the promotion manifest +
    the `promotion` gate report; advances PROMOTION_QUEUE -> PROMOTION_REVIEW if `advance`."""
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    if state["current_state"] not in _STATES_ALLOWING_PROMO_QUEUE:
        raise ConfigurationError(
            f"promotion queue expects the project at/through YOUTUBE_UPLOAD, "
            f"is at {state['current_state']}"
        )

    promo_cfg = load_promotion_config(root)
    cfg = _project_cfg(paths)
    prov = _source_provenance(paths)

    # Canonical URL: the first recorded YouTube upload URL, if any.
    video_url = None
    if paths.upload_manifest.is_file():
        for rec in load_json(paths.upload_manifest).get("uploads", []):
            if rec.get("url"):
                video_url = rec["url"]
                break

    defaults = promo_cfg.get("defaults", {})
    ctx = {
        "title": (cfg.get("source") or {}).get("title") or prov.get("title") or project_id,
        "url": video_url or "(add watch URL)",
        "summary": (cfg.get("distribution") or {}).get("summary") or "",
        "hashtags": defaults.get("hashtags", ""),
    }

    posts: list[dict[str, Any]] = []
    seen_messages: dict[str, str] = {}
    for platform, pcfg in promo_cfg.get("platforms", {}).items():
        automatable = bool(pcfg.get("automatable"))
        enabled = bool(pcfg.get("enabled"))
        template = pcfg.get("message_template", "{title}\n{url}")
        message = _render_message(template, ctx)
        status = "queued" if (automatable and enabled) else "checklist"
        # Guardrail: if two platforms would AUTO-POST byte-identical text, tag the later one so
        # the human differentiates before publish (no silent cross-posting). Only applies to
        # queued posts — checklist items are hand-adapted per their guide, so a shared default
        # template between manual platforms is expected and not a finding.
        duplicate_of = None
        if status == "queued":
            duplicate_of = next((p for p, m in seen_messages.items() if m == message), None)
            seen_messages[platform] = message
        post = {
            "platform": platform,
            "automatable": automatable,
            "language": None,
            "message": message,
            "target": pcfg.get("credentials_ref"),
            "status": status,
            "dry_run": None,
            "post_id": None,
            "url": None,
            "request_preview": None,
            "error": (f"duplicate message text with {duplicate_of}; differentiate before publish"
                      if duplicate_of else None),
            "posted_at": None,
        }
        if not automatable and pcfg.get("guide"):
            post["target"] = f"docs/guides/{pcfg['guide']}"
        posts.append(post)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "created_at": utc_now(),
        "created_by": actor,
        "video_url": video_url,
        "posts": posts,
    }
    require_valid(root, manifest, "promotion-manifest.schema.json")
    with project_lock(paths.lock):
        paths.promotion_manifest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(paths.promotion_manifest, manifest)
    artifact = artifacts_mod.register_artifact(
        root, project_id, paths.promotion_manifest, "promotion", "PROMOTION_QUEUE", actor,
    )

    findings: list[dict[str, Any]] = []
    dups = [p["platform"] for p in posts if p.get("error")]
    for platform in dups:
        findings.append({"severity": "major", "category": "promo-duplicate-text",
                         "summary": f"{platform}: post text duplicates another platform."})
    queued = [p for p in posts if p["status"] == "queued"]
    if not queued:
        findings.append({"severity": "note", "category": "promo-manual-only",
                         "summary": "no automatable platform enabled; all posts are checklist items."})
    decision = "CONDITIONAL_PASS" if dups else "PASS"
    gate_path = _write_gate_report(
        root, project_id, "promotion", decision, findings,
        {"posts": len(posts), "queued": len(queued), "checklist": len(posts) - len(queued)}, actor,
    )
    append_event(paths.events, project_id, "PROMOTION_QUEUED", actor, {
        "posts": len(posts), "queued": len(queued), "decision": decision,
        "manifest_sha256": artifact["sha256"],
    })

    advanced = state["current_state"]
    if advance and state["current_state"] == "PROMOTION_QUEUE":
        blockers = state_mod.transition_blockers(root, project_id, "PROMOTION_QUEUE",
                                                  "PROMOTION_REVIEW")
        if not blockers:
            state_mod.transition(root, project_id, "PROMOTION_REVIEW", actor,
                                 reason="promotion posts queued")
            advanced = "PROMOTION_REVIEW"

    return {"manifest": _rel(paths.promotion_manifest, paths), "artifact": artifact,
            "posts": posts, "decision": decision, "report": _rel(gate_path, paths),
            "advanced_to": advanced}


def load_promotion_manifest(root: Path, project_id: str) -> dict[str, Any]:
    paths = ProjectPaths(root, project_id).require()
    if not paths.promotion_manifest.is_file():
        raise DistributionError("no promotion manifest; run `distribute promote queue` first")
    return load_json(paths.promotion_manifest)


# --- 4. promotion publish ----------------------------------------------------

def run_promotion_publish(
    root: Path,
    project_id: str,
    *,
    dry_run: bool = True,
    actor: str = "agent",
    advance: bool = False,
) -> dict[str, Any]:
    """Fire queued automatable posts (dry_run default) and finalize manual checklist items.

    Requires the promotion_review human approval to have been granted (the
    PROMOTION_REVIEW->PROMOTION_PUBLISHED edge is gated). Automatable posts route through
    net.publish.post_promotion; manual posts stay `checklist`. Records each outcome in the
    promotion manifest and advances PROMOTION_REVIEW -> PROMOTION_PUBLISHED -> MONITORING."""
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    current = state["current_state"]
    if current not in {"PROMOTION_REVIEW", "PROMOTION_PUBLISHED"}:
        raise ConfigurationError(
            f"promotion publish expects the project at PROMOTION_REVIEW/PROMOTION_PUBLISHED, "
            f"is at {current}"
        )
    # The human gate lives on PROMOTION_REVIEW->PROMOTION_PUBLISHED; require it before firing.
    if not has_valid_approval(root, project_id, "promotion_review", None):
        raise DistributionError(
            "promotion_review approval required before publishing posts (human-only gate)"
        )

    manifest = load_promotion_manifest(root, project_id)
    promo_cfg = load_promotion_config(root)
    results: list[dict[str, Any]] = []
    for post in manifest["posts"]:
        platform = post["platform"]
        pcfg = promo_cfg.get("platforms", {}).get(platform, {})
        if not (post["automatable"] and pcfg.get("enabled")):
            post["status"] = "checklist"  # manual: leave for the human + guide
            results.append({"platform": platform, "status": "checklist"})
            continue
        if post.get("error"):  # duplicate-text guardrail: skip rather than spam
            post["status"] = "skipped"
            results.append({"platform": platform, "status": "skipped", "reason": post["error"]})
            continue
        credentials_ref = pcfg.get("credentials_ref", platform.upper())
        try:
            res = publish_mod.post_promotion(platform, message=post["message"],
                                             credentials_ref=credentials_ref, dry_run=dry_run)
            post["dry_run"] = bool(dry_run)
            post["request_preview"] = res.get("request_preview")
            post["post_id"] = res.get("post_id")
            post["url"] = res.get("url")
            post["status"] = "posted" if not dry_run else "queued"
            post["posted_at"] = utc_now() if not dry_run else None
            results.append({"platform": platform, "status": post["status"],
                            "dry_run": bool(dry_run)})
        except Exception as exc:  # noqa: BLE001 - record failure, don't abort the batch
            post["status"] = "failed"
            post["error"] = str(exc)[:400]
            results.append({"platform": platform, "status": "failed", "error": post["error"]})

    manifest["created_by"] = manifest.get("created_by") or actor
    require_valid(root, manifest, "promotion-manifest.schema.json")
    with project_lock(paths.lock):
        atomic_write_json(paths.promotion_manifest, manifest)
    artifacts_mod.register_artifact(
        root, project_id, paths.promotion_manifest, "promotion", "PROMOTION_PUBLISHED", actor,
    )
    append_event(paths.events, project_id, "PROMOTION_PUBLISHED", actor, {
        "dry_run": bool(dry_run), "results": results,
    })

    advanced = current
    if advance:
        # PROMOTION_REVIEW -> PROMOTION_PUBLISHED (gated), then -> MONITORING (terminal).
        if current == "PROMOTION_REVIEW":
            blockers = state_mod.transition_blockers(root, project_id, "PROMOTION_REVIEW",
                                                      "PROMOTION_PUBLISHED")
            if not blockers:
                state_mod.transition(root, project_id, "PROMOTION_PUBLISHED", actor,
                                     reason="promotion posts published")
                advanced = "PROMOTION_PUBLISHED"
        if advanced == "PROMOTION_PUBLISHED" and not dry_run:
            state_mod.transition(root, project_id, "MONITORING", actor,
                                 reason="promotion complete")
            advanced = "MONITORING"

    return {"manifest": _rel(paths.promotion_manifest, paths), "results": results,
            "dry_run": bool(dry_run), "advanced_to": advanced}
