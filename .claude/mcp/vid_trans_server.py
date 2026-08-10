#!/usr/bin/env python3
"""Read-only / dry-run MCP surface for publish-vid-trans.

Mirrors publish-book's posture: every real mutation goes through vid_cli.py, never
through MCP. This server exposes only query/validate tools. The pre_tool_policy hook
additionally blocks any write-capable MCP tool name as defense in depth.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# The high-level decorator server moved between SDK majors: `MCPServer` (mcp >= 2.0) replaced
# `FastMCP` (mcp 1.x). Both expose the same `@server.tool()` / `server.run()` surface we use, so
# accept whichever the installed SDK provides.
try:
    from mcp.server import MCPServer as _McpServer  # mcp >= 2.0
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP as _McpServer  # mcp 1.x
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Install MCP support: python -m pip install 'mcp>=2.0'") from exc

from video_translation_house.artifacts import list_artifacts
from video_translation_house.catalog import get_entry, list_entries
from video_translation_house.doctor import run_doctor
from video_translation_house.project import list_projects, project_status, validate_project
from video_translation_house.rights import check_rights
from video_translation_house.state import next_actions, plan
from video_translation_house.util import repo_root
from video_translation_house.validation import validate_framework

mcp = _McpServer("vid-trans-house")


def root() -> Path:
    return repo_root()


@mcp.tool()
def framework_doctor() -> dict[str, Any]:
    """Check environment, media tools, and engine adapters. Makes no changes."""
    return run_doctor(root())


@mcp.tool()
def framework_validate() -> dict[str, Any]:
    """Validate the state machine and JSON schemas. Read-only."""
    return validate_framework(root())


@mcp.tool()
def projects_list() -> list[dict[str, Any]]:
    """List translation projects and their current states."""
    return list_projects(root())


@mcp.tool()
def project_get_status(project_id: str) -> dict[str, Any]:
    """Return project config, state, language tracks, artifacts, approvals, recent events."""
    return project_status(root(), project_id)


@mcp.tool()
def project_get_validate(project_id: str) -> dict[str, Any]:
    """Validate a project's state/config/artifacts against schemas. Read-only."""
    return validate_project(root(), project_id)


@mcp.tool()
def project_next_actions(project_id: str) -> dict[str, Any]:
    """Enumerate candidate transitions with permitted/blocked status. Read-only."""
    return next_actions(root(), project_id)


@mcp.tool()
def project_plan(project_id: str) -> dict[str, Any]:
    """Deterministic next-step recommendation (autonomy_action). Read-only; changes no state."""
    return plan(root(), project_id)


@mcp.tool()
def project_artifacts(project_id: str) -> list[dict[str, Any]]:
    """List a project's registered artifacts with hashes and provenance. Read-only."""
    return list_artifacts(root(), project_id)


@mcp.tool()
def project_rights(project_id: str) -> dict[str, Any]:
    """Report the project's rights status and distributability. Read-only."""
    return check_rights(root(), project_id)


@mcp.tool()
def catalog_list() -> list[dict[str, Any]]:
    """List all cataloged source videos with metadata and rights status. Read-only."""
    return list_entries(root())


@mcp.tool()
def catalog_show(video_id: str) -> dict[str, Any] | None:
    """Return a single catalog entry by video id. Read-only."""
    return get_entry(root(), video_id)


@mcp.tool()
def transcript_show(project_id: str, language: str | None = None) -> dict[str, Any]:
    """Return the canonical source-language transcript (cues + timing). Read-only."""
    from video_translation_house.transcript import load_transcript

    return load_transcript(root(), project_id, language)


@mcp.tool()
def transcript_qa_preview(project_id: str, language: str | None = None) -> dict[str, Any]:
    """Compute the deterministic transcript-QA analysis (decision/findings/metrics) in
    memory WITHOUT writing any report or mutating state. Dry-run only."""
    from video_translation_house.transcript import load_transcript
    from video_translation_house.transcript_qa import analyze_transcript

    return analyze_transcript(load_transcript(root(), project_id, language))


@mcp.tool()
def captions_show(project_id: str, language: str) -> dict[str, Any]:
    """Return the canonical translated captions.<lang>.json (cues + timing). Read-only."""
    from video_translation_house.translate import load_captions

    return load_captions(root(), project_id, language)


@mcp.tool()
def glossary_check_preview(project_id: str, language: str) -> dict[str, Any]:
    """Run the deterministic glossary hard-check for one language's captions in memory,
    WITHOUT writing any gate report. Returns findings + hit-rate metrics. Dry-run only."""
    from video_translation_house.glossary import check_captions, load_glossary
    from video_translation_house.paths import ProjectPaths
    from video_translation_house.translate import _project_glossary_id, load_captions

    paths = ProjectPaths(root(), project_id).require()
    gid = _project_glossary_id(paths)
    if not gid:
        return {"glossary_id": None, "findings": [], "metrics": {"note": "no glossary configured"}}
    doc = load_captions(root(), project_id, language)
    findings, metrics = check_captions(load_glossary(root(), gid), doc)
    return {"glossary_id": gid, "language": language, "findings": findings, "metrics": metrics}


@mcp.tool()
def caption_validate_preview(project_id: str, language: str) -> dict[str, Any]:
    """Compute the deterministic caption readability analysis (decision/findings/metrics)
    for one language in memory WITHOUT writing the gate report. Dry-run only."""
    from video_translation_house.captions import validate_captions
    from video_translation_house.translate import load_captions

    return validate_captions(load_captions(root(), project_id, language))


@mcp.tool()
def sync_report_show(project_id: str, language: str | None = None) -> dict[str, Any]:
    """Return the dub sync report (per-cue drift, stretch, cumulative offset). Read-only.
    With `language`, return just that track's block."""
    from video_translation_house.dubbing import load_sync_report

    report = load_sync_report(root(), project_id)
    if language is not None:
        return report.get("languages", {}).get(language, {})
    return report


@mcp.tool()
def audio_qa_preview(project_id: str) -> dict[str, Any]:
    """Compute the deterministic audio-sync analysis per dub-enabled language in memory
    WITHOUT writing the gate report or mutating state. Dry-run only."""
    from video_translation_house.dubbing import _audio_quality_bars, analyze_sync, load_sync_report

    bars = _audio_quality_bars(root())
    report = load_sync_report(root(), project_id)
    return {
        lang: analyze_sync(block, bars)
        for lang, block in report.get("languages", {}).items()
    }


@mcp.tool()
def package_manifest_show(project_id: str) -> dict[str, Any]:
    """Return the deliverable package manifest (packages/package-manifest.json). Read-only."""
    from video_translation_house.packaging import load_package_manifest

    return load_package_manifest(root(), project_id)


@mcp.tool()
def final_qa_preview(project_id: str) -> dict[str, Any]:
    """Probe each dub-enabled track's dubbed video and compute the deterministic mux analysis
    (stream presence + A/V alignment) in memory WITHOUT writing the gate report or mutating
    state. Dry-run only."""
    from video_translation_house.packaging import _probe_dubbed_block, analyze_mux
    from video_translation_house.paths import ProjectPaths
    from video_translation_house.state import _active_track_langs
    from video_translation_house.util import load_json as _load_json

    paths = ProjectPaths(root(), project_id).require()
    state = _load_json(paths.state)
    tracks = state.get("language_tracks", {})
    out: dict[str, Any] = {}
    for lang in _active_track_langs(state):
        if not tracks.get(lang, {}).get("dub_enabled"):
            continue
        block = _probe_dubbed_block(paths, lang)
        out[lang] = {"probe": block, "analysis": analyze_mux(block) if block["present"] else None}
    return out


@mcp.tool()
def chapters_show(project_id: str, language: str) -> dict[str, Any] | None:
    """Return a language's canonical chapters doc (chapters/chapters.<lang>.json), or null if
    none has been imported. Read-only."""
    from video_translation_house.chapters import load_chapters

    return load_chapters(root(), project_id, language)


@mcp.tool()
def chapters_youtube_timecodes(project_id: str, language: str) -> dict[str, Any]:
    """Render the YouTube description timecode block from a language's chapters, WITHOUT
    writing anything. Returns {} block text; empty string if no chapters. Read-only."""
    from video_translation_house.chapters import (
        load_chapters,
        render_youtube_description_timecodes,
    )

    doc = load_chapters(root(), project_id, language)
    return {"language": language, "timecodes": render_youtube_description_timecodes(doc) if doc else ""}


@mcp.tool()
def platform_package_show(project_id: str) -> dict[str, Any]:
    """Return the platform package manifest (distribution/platform-package.json). Read-only."""
    from video_translation_house.distribution import load_platform_package

    return load_platform_package(root(), project_id)


@mcp.tool()
def upload_manifest_show(project_id: str) -> dict[str, Any]:
    """Return the upload manifest (distribution/upload-manifest.json), or an empty shell if no
    upload has been recorded. Read-only."""
    from video_translation_house.distribution import _load_upload_manifest
    from video_translation_house.paths import ProjectPaths

    paths = ProjectPaths(root(), project_id).require()
    return _load_upload_manifest(paths, project_id)


@mcp.tool()
def promotion_manifest_show(project_id: str) -> dict[str, Any]:
    """Return the promotion manifest (distribution/promotion-manifest.json). Read-only."""
    from video_translation_house.distribution import load_promotion_manifest

    return load_promotion_manifest(root(), project_id)


@mcp.tool()
def budget_status() -> dict[str, Any]:
    """Vendor (billed) TTS spend ceiling, running total, and remaining headroom. Read-only.

    The ceiling is human-set (company config); this tool only reports it — it can never
    raise it or record spend.
    """
    from video_translation_house.budget import status as budget_status_fn

    return budget_status_fn(root())


if __name__ == "__main__":
    mcp.run()
