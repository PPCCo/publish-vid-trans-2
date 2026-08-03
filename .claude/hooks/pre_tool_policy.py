#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for value in (str(HERE), str(HERE.parent / "scripts")):
    if value not in sys.path:
        sys.path.insert(0, value)

from hooklib import (
    active_project,
    append_audit,
    project_root,
    read_input,
    structured_ask,
    structured_deny,
)

# The HARD-DENY set is deliberately narrow: only the CLI-owned state integrity the
# company policy names as non-negotiable (state.json/manifest.json/approvals/events under
# projects/, plus the shared security config). Everything else that is risky-but-legitimate
# escalates to a human permission prompt ("ask") rather than an outright block.
PROTECTED_PARTS = {".git", ".env", "secrets", "credentials"}
PROTECTED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}

# CLI-owned state files/dirs inside a project. Direct agent writes here corrupt the
# single source of truth (the CLI must own every mutation) → HARD deny, no prompt.
_CLI_STATE_NAMES = {"state.json", "manifest.json"}
_CLI_STATE_DIRS = {"approvals", "events"}

_RM_RECURSIVE = re.compile(r"\brm\s+-[^\n]*r[^\n]*f\b|\brm\s+-rf\b", re.IGNORECASE)

# Directories a recursive rm may target without a prompt: OS temp roots only. A recursive
# deletion is auto-allowed iff EVERY path argument resolves under one of these; anything
# touching the repo, $HOME, or a system path escalates to a human prompt instead.
_TEMP_ROOTS = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")

# Risky-but-legitimate Bash operations. These no longer hard-deny; they escalate to a
# human permission prompt so the user can approve case by case.
ASK_PATTERNS = [
    (r"\bsudo\b", "privilege escalation — approve if you intend to run this as root"),
    (r"\bgit\s+push\b", "remote Git write — approve to push to the remote"),
    (r"\bgit\s+reset\s+--hard\b", "destructive Git reset — approve to discard working changes"),
    (r"\b(curl|wget)\b[^\n]*(--data|-d\s|--request\s+(POST|PUT|PATCH|DELETE)|-X\s*(POST|PUT|PATCH|DELETE))",
     "network write — approve to send this request"),
    (r"\b(aws|gcloud|az|terraform|kubectl)\b[^\n]*(apply|delete|destroy|create|deploy|publish|upload|put-object)",
     "cloud infrastructure / upload write — approve to run this cloud operation"),
    (r"\b(youtube|yt)\b[^\n]*(upload|insert|publish)",
     "platform upload — approve only if this is an authorized publication"),
]


def _file_path(tool_input: dict, root: Path) -> Path | None:
    value = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    if not value:
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _is_claude_memory(path: Path) -> bool:
    """Claude Code's per-project memory dir lives outside the repo (~/.claude/projects/
    <slug>/memory/) and is a sanctioned write target, not an escape."""
    parts = path.parts
    return ".claude" in parts and "projects" in parts and "memory" in parts


def _is_claude_plans(path: Path) -> bool:
    """Claude Code's plan-mode plan files live in ~/.claude/plans/*.md — outside the
    repo but a sanctioned write target the plan workflow owns, not an escape. Same
    reasoning as the memory dir above."""
    parts = path.parts
    return ".claude" in parts and "plans" in parts and path.suffix.lower() == ".md"


def _rm_targets_temp_only(command: str) -> bool:
    """True when a recursive `rm` deletes ONLY paths under an OS temp root.

    Tokens are split on shell whitespace (good enough for the audit — an attacker
    smuggling paths via subshells still fails the 'every arg is temp' test because the
    literal tokens won't all be under a temp root). Any flag token (starting with '-') is
    skipped; every remaining token must resolve under a temp root, and there must be at
    least one. $TMPDIR is honored via its real path.
    """
    tmpdir = os.environ.get("TMPDIR", "")
    temp_roots = list(_TEMP_ROOTS)
    if tmpdir:
        resolved = str(Path(tmpdir).resolve())
        temp_roots.append(resolved.rstrip("/") + "/")
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    # Only consider the `rm ...` segment; if the command chains (&&, ;, |) other verbs,
    # be conservative and refuse the fast-path (let the normal ban apply).
    if any(sep in tokens for sep in ("&&", "||", ";", "|")):
        return False
    if not tokens or tokens[0] != "rm":
        return False
    targets = [t for t in tokens[1:] if not t.startswith("-")]
    if not targets:
        return False
    for target in targets:
        candidate = target if target.startswith("/") else str((Path.cwd() / target).resolve())
        candidate = candidate.rstrip("/") + "/"
        if not any(candidate.startswith(root) for root in temp_roots):
            return False
    return True


def _cli_owned_state(relative: Path) -> bool:
    """True when the path is CLI-owned project state (state.json/manifest.json, or anything
    under a project's approvals/ or events/ dir). These are the single source of truth the
    CLI must own exclusively — direct agent writes corrupt it, so they hard-deny."""
    parts = relative.parts
    if not parts or parts[0] != "projects" or len(parts) < 3:
        return False
    # projects/<id>/<...>
    tail = parts[2:]
    if tail and tail[-1] in _CLI_STATE_NAMES:
        return True
    return any(seg in _CLI_STATE_DIRS for seg in tail)


def _protected(path: Path, root: Path) -> tuple[str, str] | None:
    """Return (decision, reason) where decision is 'deny' (hard block, CLI-state integrity
    or shared security config) or 'ask' (escalate to a human prompt). None → allowed.

    Per the chosen policy posture, only state-integrity stays hard-deny; secrets, .git,
    key files, and writes outside the repo now escalate to a prompt instead of blocking."""
    if _is_claude_memory(path) or _is_claude_plans(path):
        return None
    try:
        relative = path.relative_to(root)
    except ValueError:
        return ("ask", "write outside the repository — approve if this path is intended")
    # HARD deny: the CLI-owned source of truth and the shared security config.
    if _cli_owned_state(relative):
        return ("deny", "CLI-owned project state must be written through vid_cli.py, never by hand")
    if relative.as_posix() in {".claude/settings.json", ".mcp.json"}:
        return ("deny", "shared security configuration requires direct human editing")
    # ASK: sensitive-but-legitimate targets — a human confirms case by case.
    lowered = [part.lower() for part in relative.parts]
    if ".git" in lowered:
        return ("ask", "edit inside .git — approve if you intend to modify Git internals")
    if any(part.startswith(".env") for part in lowered):
        return ("ask", "environment/secret file — approve to write it")
    if any(part in PROTECTED_PARTS for part in lowered):
        return ("ask", "secret or credential directory — approve to write here")
    if path.suffix.lower() in PROTECTED_SUFFIXES:
        return ("ask", "private key / certificate file — approve to write it")
    return None


def main() -> int:
    from video_translation_house.util import utc_now

    payload = read_input()
    root = project_root(payload)
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    project_id = active_project(payload, root)
    record = {
        "time": utc_now(),
        "event": "PRE_TOOL_USE",
        "session_id": payload.get("session_id"),
        "tool": tool,
        "project_id": project_id,
    }

    if tool in {"Write", "Edit", "NotebookEdit"}:
        path = _file_path(tool_input, root)
        if path:
            verdict = _protected(path, root)
            if verdict:
                decision, reason = verdict
                record.update({"decision": decision, "reason": reason, "path": str(path)})
                append_audit(root, record)
                if decision == "deny":
                    structured_deny(reason)
                structured_ask(reason)
            record["path"] = str(path)

    if tool == "Bash":
        command = str(tool_input.get("command") or "")
        record["command"] = command[:1000]
        # Ingest/vendor egress is HARD-denied unless the sanctioned fetch flag is set — this
        # is the network-egress non-negotiable, not a case-by-case call.
        if os.environ.get("VIDTRANS_FETCH_ENABLED", "0").strip().lower() not in {"1", "true", "on", "yes"}:
            if re.search(r"\byt-dlp\b", command, re.IGNORECASE):
                reason = "yt-dlp egress is disabled; set VIDTRANS_FETCH_ENABLED=1 to permit ingest"
                record.update({"decision": "deny", "reason": reason})
                append_audit(root, record)
                structured_deny(reason)
        # External publication verbs are HARD-denied unless explicitly enabled — the
        # pipeline stops at READY_FOR_REVIEW by policy.
        if os.environ.get("VIDTRANS_EXTERNAL_WRITES", "disabled").lower() != "enabled":
            # Scoped to publish/marketplace API verbs. Deliberately avoids bare
            # "activate"/"install" (venv/pip false positives) AND requires publish/purchase
            # to stand alone as command words — NOT embedded in path slugs like
            # "publish-vid-trans" where "\b" would otherwise match around the hyphen.
            if re.search(
                r"\b(videos\.insert|captions\.insert|thumbnails\.set|playlistItems\.insert)\b"
                r"|(?<![\w./-])(publish|purchase)(?![\w./-])"
                r"|--upload\b|\bupload-video\b"
                # Phase 6 platform write endpoints: X/Twitter v2 tweets + v1.1 media upload,
                # Telegram Bot API sendMessage/sendVideo, Discord webhook posts.
                r"|api\.(twitter|x)\.com/\d[\w./]*|upload\.twitter\.com"
                r"|api\.telegram\.org/bot|/webhooks?/\d+/",
                command,
                re.IGNORECASE,
            ):
                reason = "external writes are disabled; produce a package or dry-run instead"
                record.update({"decision": "deny", "reason": reason})
                append_audit(root, record)
                structured_deny(reason)
        # Recursive rm is auto-allowed only when every target is under an OS temp root
        # (tempdir cleanup). Anywhere else, escalate to a human prompt rather than block.
        if _RM_RECURSIVE.search(command) and not _rm_targets_temp_only(command):
            reason = "recursive forced deletion outside OS temp dirs — approve if the target is intended"
            record.update({"decision": "ask", "reason": reason})
            append_audit(root, record)
            structured_ask(reason)
        # Other risky-but-legitimate operations now prompt instead of hard-denying.
        for pattern, reason in ASK_PATTERNS:
            if re.search(pattern, command, flags=re.IGNORECASE):
                record.update({"decision": "ask", "reason": reason})
                append_audit(root, record)
                structured_ask(reason)

    if tool.startswith("mcp__") and re.search(
        r"(?:create|update|delete|publish|submit|upload|purchase|send|post)", tool, re.IGNORECASE
    ):
        reason = "write-capable MCP tool — approve if this call is intended"
        record.update({"decision": "ask", "reason": reason})
        append_audit(root, record)
        structured_ask(reason)

    if project_id:
        record["decision"] = "no-objection"
        append_audit(root, record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
