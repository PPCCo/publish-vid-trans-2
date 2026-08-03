from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def read_input() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def project_root(payload: dict[str, Any]) -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("VIDTRANS_REPO_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    cwd = payload.get("cwd") or os.getcwd()
    current = Path(cwd).expanduser().resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".claude").is_dir():
            return candidate
    return current


def active_project(payload: dict[str, Any], root: Path) -> str | None:
    """Best-effort: infer the project id from a file path in the tool input."""
    tool_input = payload.get("tool_input") or {}
    value = tool_input.get("file_path") or tool_input.get("path") or ""
    try:
        rel = Path(str(value)).resolve().relative_to(root / "projects")
        return rel.parts[0] if rel.parts else None
    except (ValueError, OSError):
        return None


def append_audit(root: Path, record: dict[str, Any]) -> None:
    logs = root / ".claude" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "hook-audit.ndjson").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def structured_deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    raise SystemExit(0)


def structured_ask(reason: str) -> None:
    """Escalate the tool call to a human permission prompt instead of hard-denying it.

    Used for higher-risk-but-legitimate operations (recursive rm outside temp, sudo,
    git push, network writes, cloud CLI) where the policy wants a human in the loop but
    not an outright block. The hard-deny set stays reserved for CLI-owned state integrity.
    """
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }))
    raise SystemExit(0)
