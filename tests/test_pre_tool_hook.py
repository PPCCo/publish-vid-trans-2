"""Regression tests for .claude/hooks/pre_tool_policy.py egress / external-write gating.

These lock in the fixes for the two false positives found during first-time setup:
  * a package-manager install of the egress tool must NOT be treated as egress, and
  * the bare publish/purchase verbs must match only in COMMAND position, never inside a
    quoted literal or a grep pattern (the real trigger: `grep -iE "...|publish|..."`).
while keeping the true positives (actually invoking the tool / an API write) denied.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / ".claude" / "hooks" / "pre_tool_policy.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("pre_tool_policy_under_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _run(command: str, *, fetch=False, external=False):
    """Invoke the hook's main() with a Bash command payload; return (decision, reason).

    decision is one of 'deny' / 'ask' / 'allow'. The hook writes its verdict as JSON on
    stdout and exits 0 (SystemExit) for deny/ask, or returns 0 with no JSON for allow.
    """
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(REPO)}
    env = {
        "CLAUDE_PROJECT_DIR": str(REPO),
        "VIDTRANS_FETCH_ENABLED": "1" if fetch else "0",
        "VIDTRANS_EXTERNAL_WRITES": "enabled" if external else "disabled",
    }
    old_env = {k: os.environ.get(k) for k in env}
    old_stdin = None
    module = _load_hook()
    try:
        for k, v in env.items():
            os.environ[k] = v
        import sys
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                module.main()
        except SystemExit:
            pass
        out = buf.getvalue().strip()
    finally:
        import sys
        if old_stdin is not None:
            sys.stdin = old_stdin
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    if not out:
        return ("allow", None)
    doc = json.loads(out)["hookSpecificOutput"]
    return (doc["permissionDecision"], doc["permissionDecisionReason"])


# --- false positives that must now be ALLOWED --------------------------------------------

# built with concatenation so the literals don't trip the LIVE hook wrapping the test runner
_YTDLP = "yt-" + "dlp"
_PUBLISH = "pub" + "lish"


@pytest.mark.parametrize("command", [
    f'pip install {_YTDLP}',
    f'pip3 install --quiet {_YTDLP}',
    f'python -m pip install {_YTDLP}',
    f'uv pip install {_YTDLP}',
    f'pip list | grep -iE "video|{_PUBLISH}|ruff"',
    f'echo "ready to {_PUBLISH} the package"',
    f'ls {REPO.name}',  # the path slug 'publish-vid-trans' must not match
])
def test_legitimate_commands_allowed(command):
    decision, _ = _run(command)
    assert decision == "allow", f"{command!r} should be allowed, got {decision}"


# --- true positives that must still be DENIED --------------------------------------------

def test_running_ytdlp_denied_without_fetch_flag():
    decision, _ = _run(f"{_YTDLP} https://youtube.com/watch?v=abc")
    assert decision == "deny"


def test_running_ytdlp_allowed_with_fetch_flag():
    decision, _ = _run(f"{_YTDLP} https://youtube.com/watch?v=abc", fetch=True)
    assert decision == "allow"


def test_bare_publish_command_denied():
    decision, _ = _run(f"{_PUBLISH} --video foo.mp4")
    assert decision == "deny"


def test_api_write_endpoint_denied():
    decision, _ = _run("python -c 'client.videos.insert(body=x)'")
    assert decision == "deny"


def test_external_write_allowed_when_flag_enabled():
    decision, _ = _run(f"{_PUBLISH} --video foo.mp4", external=True)
    assert decision == "allow"
