"""Print the raw external shell command (+ vid_cli.py alternative) for a pipeline step.

Two pure, READ-ONLY verbs back this module — they *print* copy-paste-ready terminal blocks
and never execute anything or mutate state (rule 1: the CLI owns state; this is a view over
it, exactly like `catalog.enrich_entry`'s `next_command`):

    vid_cli.py cmd <video-id-or-url>   # the onboarding/ingest equivalent for a video/playlist
    vid_cli.py nextcmd <project-id>    # the raw-external equivalent of the current next step

Where a step maps to a real external tool (first cut: only INGEST -> yt-dlp) *and* a
`vid_cli.py` verb, BOTH are emitted as labelled options. Where there is no external tool (the
pure state mutations — langid set, translate import, …), the block falls back to the
`vid_cli.py` verb (a "CLI-only state op"). Human gates (rule 13) emit no runnable command —
just an explanation plus the disclose+confirm-first approval line as reference.

The whole thing is template-driven (`_STEP_TEMPLATES`, keyed like `catalog._STATE_NEXT_VERB`)
so the printer and the real executor can never drift: the raw yt-dlp argv comes from the same
`net.build_ytdlp_download_argv` the downloader uses, and the vid_cli.py verb strings + the
gate/kickoff/rights compounds are imported from `catalog`, not re-written here.

Output is bare-terminal form by default (no `!`). `--for-claude` re-adds `!` on runnable
lines, with two deliberate exceptions:
  * gate reference lines NEVER get `!` (rule 13 — they must not be run standalone); and
  * a CLI-only *non-gate* state op DOES get `!` under --for-claude (Claude may run it, rule 1),
    matching `enrich_entry`'s existing PROCEED convention.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import catalog
from .catalog import (
    _STATE_NEXT_VERB,
    _gate_reference_command,
    _kickoff_command,
    _rights_blocked_command,
)
from .net import build_ytdlp_download_argv, build_ytdlp_playlist_argv, is_playlist_url, normalize_youtube_url
from .paths import ProjectPaths, is_valid_video_id

# env preamble modes for an emitted block
_ENV_NONE = "none"
_ENV_FETCH_FLAG = "fetch_flag_only"   # raw brew/PATH yt-dlp — only needs the fetch flag
_ENV_LOCAL = "env_local"              # goes through the Python net layer — full .env.local

_ENV_PREAMBLE = {
    _ENV_NONE: [],
    _ENV_FETCH_FLAG: ["export VIDTRANS_FETCH_ENABLED=1"],
    _ENV_LOCAL: ["source .env.local"],
}


def _cli() -> str:
    return catalog._cli()


@dataclass(frozen=True)
class CommandOption:
    """One labelled, runnable block: a `cd`, an optional env preamble, and a command line."""
    label: str
    command: str
    cwd: Path | None = None
    env: str = _ENV_NONE
    runnable: bool = True   # False -> never prefixed with `!` (gate reference lines)


def _render_block(opt: CommandOption, *, for_claude: bool) -> str:
    lines: list[str] = []
    if opt.cwd is not None:
        lines.append(f"cd {opt.cwd}")
    lines.extend(_ENV_PREAMBLE.get(opt.env, []))
    prefix = "! " if (for_claude and opt.runnable) else ""
    lines.append(f"{prefix}{opt.command}")
    return "\n".join(lines)


def _render_options(options: list[CommandOption], *, for_claude: bool, header: str | None = None) -> str:
    """Join labelled blocks. A single option skips the block label header; multiple options
    each get their `label:` line so the operator can tell them apart."""
    parts: list[str] = []
    if header:
        parts.append(header)
    if len(options) == 1:
        opt = options[0]
        parts.append(f"{opt.label}:")
        parts.append(_render_block(opt, for_claude=for_claude))
    else:
        for opt in options:
            parts.append(f"{opt.label}:")
            parts.append(_render_block(opt, for_claude=for_claude))
            parts.append("")   # blank line between stacked options
        if parts and parts[-1] == "":
            parts.pop()
    return "\n".join(parts)


# --------------------------------------------------------------------------------------
# Per-state template table. Keyed exactly like catalog._STATE_NEXT_VERB (reuse those verb
# strings verbatim so the two surfaces cannot drift). First cut: only INGEST maps to a real
# external tool; every other state is a cli-only fallback.
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class TemplateCtx:
    root: Path
    project_id: str
    paths: ProjectPaths
    url: str | None
    plan: dict[str, Any]


@dataclass(frozen=True)
class StepTemplate:
    """One current_state's advance step, as raw-external + vid_cli.py options."""
    verb_template: str                                    # the vid_cli.py verb, "{id}" placeholder
    raw_argv: Callable[[TemplateCtx], list[str] | None] = lambda ctx: None
    raw_cwd: Callable[[TemplateCtx], Path] | None = None  # cwd for the raw command
    raw_env: str = _ENV_NONE                              # env preamble for the raw command
    verb_env: str = _ENV_NONE                             # env preamble for the vid_cli.py verb
    kind: str = "cli-only"                                # "external+cli" | "cli-only"


def _ingest_raw_argv(ctx: TemplateCtx) -> list[str] | None:
    url = ctx.url or "<url>"
    # binary="yt-dlp" so the printer never requires the tool installed locally (it's showing a
    # command to run, possibly elsewhere); the operator's own PATH resolves it.
    return build_ytdlp_download_argv(url, ctx.paths.source_dir, binary="yt-dlp")


_STEP_TEMPLATES: dict[str, StepTemplate] = {
    "INGEST": StepTemplate(
        verb_template=_STATE_NEXT_VERB["INGEST"],
        raw_argv=_ingest_raw_argv,
        raw_cwd=lambda ctx: ctx.paths.source_dir,
        raw_env=_ENV_FETCH_FLAG,
        verb_env=_ENV_LOCAL,
        kind="external+cli",
    ),
    **{
        state: StepTemplate(verb_template=verb, kind="cli-only")
        for state, verb in _STATE_NEXT_VERB.items()
        if state != "INGEST"
    },
}


# --------------------------------------------------------------------------------------
# nextcmd — the raw-external equivalent of a project's current next step.
# --------------------------------------------------------------------------------------
def nextcmd_for_project(root: Path, project_id: str, *, for_claude: bool = False) -> str:
    """Print the runnable block(s) for `project_id`'s current next step (see module docstring)."""
    from . import state as state_mod

    cli = _cli()
    try:
        plan = state_mod.plan(root, project_id)
    except Exception as exc:  # unreadable/deleted project — say so, emit nothing to run
        return f"# {project_id}: cannot read project state ({exc}); nothing to run."

    action = plan.get("autonomy_action")
    current = plan.get("current_state")
    paths = ProjectPaths(root, project_id)

    if action == "TERMINAL":
        return f"# {project_id} is TERMINAL at {current}; nothing to run."

    if action == "STOP_AT_GATE":
        gate = plan.get("required_gate")
        target = plan.get("recommended_target")
        ref = CommandOption(
            label="disclose+confirm-first reference (do NOT run standalone)",
            command=_gate_reference_command(cli, project_id, gate, target),
            cwd=root,
            env=_ENV_NONE,
            runnable=False,   # never `!`, even with --for-claude (rule 13)
        )
        header = (f"# {gate} is a human gate (rule 13) — decide in conversation; the line below "
                  f"is reference only, not meant to be run directly.")
        return _render_options([ref], for_claude=for_claude, header=header)

    if action == "PROCEED":
        tmpl = _STEP_TEMPLATES.get(current)
        if tmpl is None:
            return f"# no next command known for state {current}."
        ctx = TemplateCtx(root=root, project_id=project_id, paths=paths,
                          url=_project_url(root, project_id), plan=plan)
        verb_line = tmpl.verb_template.format(id=project_id)
        argv = tmpl.raw_argv(ctx)
        if argv is not None:
            raw = CommandOption(
                label="Option A (raw external)",
                command=shlex.join(argv),
                cwd=tmpl.raw_cwd(ctx) if tmpl.raw_cwd else root,
                env=tmpl.raw_env,
            )
            via = CommandOption(
                label="Option B (via vid_cli.py)",
                command=f"{cli} {verb_line}",
                cwd=root,
                env=tmpl.verb_env,
            )
            return _render_options([raw, via], for_claude=for_claude)
        # CLI-only state op — no external tool for this step.
        op = CommandOption(
            label="CLI-only state op (no external tool for this step)",
            command=f"{cli} {verb_line}",
            cwd=root,
            env=tmpl.verb_env,
        )
        return _render_options([op], for_claude=for_claude)

    # BLOCKED — mirror enrich_entry: the common case is rights not set before PACKAGE.
    if current == "PACKAGE":
        op = CommandOption(
            label="CLI-only state op (rights not set — blocks PACKAGE → READY_FOR_REVIEW)",
            command=_rights_blocked_command(cli, project_id),
            cwd=root,
        )
        return _render_options([op], for_claude=for_claude)
    return f"# {project_id} is BLOCKED at {current}; no command resolves it automatically."


# --------------------------------------------------------------------------------------
# cmd — the onboarding/ingest equivalent for a URL / bare id / playlist / catalog video_id.
# --------------------------------------------------------------------------------------
def cmd_for_video(root: Path, video_id_or_url: str, *, for_claude: bool = False) -> str:
    """Print the runnable block(s) to onboard/ingest a video or playlist (see module docstring)."""
    cli = _cli()
    raw = video_id_or_url.strip()

    # 1) An existing catalog video_id OR an on-disk project -> route to its current step.
    entry = catalog.get_entry(root, raw) if is_valid_video_id(raw) else None
    has_project_dir = is_valid_video_id(raw) and ProjectPaths(root, raw).config.exists()
    if entry is not None or has_project_dir:
        # project_id defaults to the id itself (init uses one id for both) when a project dir
        # exists but the catalog entry lacks project_id (or there's no catalog entry at all).
        project_id = (entry or {}).get("project_id") or (raw if has_project_dir else None)
        entry_url = (entry or {}).get("url")
        if not project_id:
            # Catalogued (e.g. under a playlist) but no project yet — kick one off.
            return _kickoff_options(root, cli, raw, entry_url or "<url>", for_claude=for_claude)
        from . import state as state_mod
        try:
            current = state_mod.plan(root, project_id).get("current_state")
        except Exception:
            current = None
        if current and current != "INGEST":
            header = (f"# {raw} is already past INGEST (current_state={current}); "
                      f"showing the current next step instead:")
            return header + "\n" + nextcmd_for_project(root, project_id, for_claude=for_claude)
        return _ingest_options(root, cli, project_id,
                               entry_url or _project_url(root, project_id), for_claude=for_claude)

    # 2) A playlist URL/id -> the flag-free enumeration carve-out (raw + add-playlist).
    if is_playlist_url(raw):
        url = normalize_youtube_url(raw)
        argv = build_ytdlp_playlist_argv(url, binary="yt-dlp")
        raw_opt = CommandOption(
            label="Option A (raw external)",
            command=shlex.join(argv),
            cwd=root,
            env=_ENV_NONE,   # enumeration is flag-free (rule 3)
        )
        via = CommandOption(
            label="Option B (via vid_cli.py)",
            command=f"{cli} catalog add-playlist {url}",
            cwd=root,
            env=_ENV_NONE,
        )
        return _render_options([raw_opt, via], for_claude=for_claude)

    # 3) A new single-video URL/bare id -> the kickoff (project init must run first).
    url = normalize_youtube_url(raw)
    return _kickoff_options(root, cli, _derive_video_id(raw, url), url, for_claude=for_claude)


def _ingest_options(root: Path, cli: str, project_id: str, url: str | None, *, for_claude: bool) -> str:
    """Both options for a project that already exists and is at INGEST."""
    paths = ProjectPaths(root, project_id)
    argv = build_ytdlp_download_argv(url or "<url>", paths.source_dir, binary="yt-dlp")
    raw = CommandOption(
        label="Option A (raw external)",
        command=shlex.join(argv),
        cwd=paths.source_dir,
        env=_ENV_FETCH_FLAG,
    )
    via = CommandOption(
        label="Option B (via vid_cli.py)",
        command=f"{cli} ingest run {project_id}",
        cwd=root,
        env=_ENV_LOCAL,
    )
    return _render_options([raw, via], for_claude=for_claude)


def _kickoff_options(root: Path, cli: str, vid: str, url: str, *, for_claude: bool) -> str:
    """Both options for a not-yet-started video: `project init` must precede the download, so the
    raw yt-dlp can't `cd` into a source dir that doesn't exist yet — annotate that."""
    targets = catalog._default_targets_csv(root)
    # The source dir the raw command would target, AFTER project init creates it.
    source_dir = ProjectPaths(root, vid).source_dir
    argv = build_ytdlp_download_argv(url, source_dir, binary="yt-dlp")
    raw = CommandOption(
        label=f"Option A (raw external — run `{cli} project init {vid} --url {url}` FIRST to create the dir)",
        command=shlex.join(argv),
        cwd=source_dir,
        env=_ENV_FETCH_FLAG,
    )
    via = CommandOption(
        label="Option B (via vid_cli.py)",
        command=_kickoff_command(cli, vid, url, targets),
        cwd=root,
        env=_ENV_LOCAL,
    )
    return _render_options([raw, via], for_claude=for_claude)


def _project_url(root: Path, project_id: str) -> str | None:
    """The source URL for a project — from its catalog entry (`project_id` match), falling back
    to `project.yaml`'s `source.url`. Used to fill the INGEST raw command's `<url>`."""
    for entry in catalog.list_entries(root):
        if entry.get("project_id") == project_id and entry.get("url"):
            return entry["url"]
    try:
        from .util import load_yaml
        cfg = load_yaml(ProjectPaths(root, project_id).config, {}) or {}
        return ((cfg.get("source") or {}).get("url")) or None
    except Exception:
        return None


def _derive_video_id(raw: str, url: str) -> str:
    """Best-effort `yt-<11char>` id from a bare id or a watch/youtu.be URL; `<video-id>`
    placeholder when it can't be extracted confidently (the operator supplies it at
    `project init`). Deliberately narrow — the real id is authoritatively resolved by yt-dlp
    at download time, so this only needs to help the common paste-a-link case."""
    from urllib.parse import parse_qs, urlparse

    if is_valid_video_id(raw) and "/" not in raw and ":" not in raw:
        # Already a `yt-…` catalog id? use it as-is; a bare YouTube id gets the `yt-` prefix.
        return raw if raw.startswith("yt-") else f"yt-{raw}"
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    vid = None
    if host == "youtu.be":
        vid = parsed.path.lstrip("/").split("/")[0] or None
    elif "youtube.com" in host:
        vid = (parse_qs(parsed.query).get("v") or [None])[0]
    if vid and len(vid) == 11:
        return f"yt-{vid}"
    return "<video-id>"
