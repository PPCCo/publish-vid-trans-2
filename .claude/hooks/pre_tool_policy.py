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

# Canonical HuggingFace cache location on this machine. HuggingFace is policy-blocked on this
# network (see project memory + OPERATING-GUIDE §6), so mlx-whisper / any HF operation must ALWAYS
# read from this persistent, pre-staged offline cache — never attempt a network hit. This is the
# single source of truth for the path: `.env.local` and `.claude/settings.json` (the env applied to
# every tool call) both point here, and the hook re-asserts it into any HF/whisper Bash command's
# effective environment so it holds even when `.env.local` was never sourced.
HF_CACHE_DIR = os.environ.get("VIDTRANS_HF_CACHE") or "/Users/qaiser.abbas/Dev/my-repos/pub/.cache/huggingface"

# All staging/model caches on this machine live under one root so there's a single place to
# point at, back up, or clean: `/Users/qaiser.abbas/Dev/my-repos/pub/.cache` (HF_CACHE_DIR above
# already resolves inside it, at `.cache/huggingface`). The MMS-TTS Urdu/Persian checkpoints are
# staged via the GitHub-repo recipe in OPERATING-GUIDE §6 (weights pushed to a private GitHub
# repo, downloaded here, then HF-cache blobs symlinked back at them) — same pattern as the
# whisper stage dir. Deleting either dir breaks the HF cache symlinks that resolve
# `facebook/mms-tts-urd-script_arabic` / `facebook/mms-tts-fas` offline, so `mms_tts.sh` (the
# `tts.binary_overrides.mms` seam) and any direct HF/transformers invocation must always resolve
# against these paths, never re-fetch from the network.
CACHE_ROOT = os.environ.get("VIDTRANS_CACHE_ROOT") or "/Users/qaiser.abbas/Dev/my-repos/pub/.cache"
MMS_STAGE_DIRS = {
    "urd": os.environ.get("VIDTRANS_MMS_URD_STAGE")
    or "/Users/qaiser.abbas/Dev/my-repos/pub/mms-tts-urd-stage",
    "fas": os.environ.get("VIDTRANS_MMS_FAS_STAGE")
    or "/Users/qaiser.abbas/Dev/my-repos/pub/mms-tts-fas-stage",
}

# Command-position tokens (or path basenames) whose invocation is an HF-backed model operation. When
# any of these run, the HF cache env below is asserted so the model is loaded from HF_CACHE_DIR
# offline, not fetched over the (blocked) network. mms_tts.sh already sets HF_HOME/HF_HUB_OFFLINE
# itself (belt-and-suspenders — see the top-of-file comment), but it's listed here too so a direct
# invocation (bypassing `dub run`) still gets the env asserted and the cache path audited.
_HF_OPERATION_NAMES = {
    "mlx_whisper", "whisper", "faster-whisper", "faster_whisper", "huggingface-cli", "hf",
    "mms_tts.sh", "mms_tts.py",
}

# The offline HF environment every HF/whisper operation must run under. HF_HUB_OFFLINE forbids any
# network hit; HF_HOME/HF_HUB_CACHE/TRANSFORMERS_CACHE all point at the staged cache so every HF
# client library (transformers, huggingface_hub, mlx-whisper) resolves models locally.
def _hf_offline_env() -> dict[str, str]:
    hub = str(Path(HF_CACHE_DIR).expanduser() / "hub")
    return {
        "HF_HOME": HF_CACHE_DIR,
        "HF_HUB_CACHE": hub,
        "TRANSFORMERS_CACHE": hub,
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_PROGRESS_BARS": "1",
    }


# Bare Python interpreter names that must NEVER be invoked directly. The project's CLI and
# helpers depend on the project venv (jsonschema/yaml/piper/etc.), and a bare `python3`
# resolves to the system interpreter that is missing those deps (see the venv PATH note in
# project memory). Company policy: always run `.venv/bin/python3` (an explicit venv path, an
# activated-venv absolute path, or an interpreter resolved via a variable like $PY) — never a
# bare `python`/`python3`. Enforced as a HARD deny, not a prompt.
_BARE_PYTHON_NAMES = {"python", "python3"}

# Shell separators after which a fresh command word begins. Used to find command-position
# tokens (the actual programs being run) so we can distinguish "run yt-dlp" / "publish" as a
# command from those same strings appearing as arguments, quoted literals, or grep patterns.
_CMD_SEPARATORS = {"&&", "||", ";", "|", "(", ")", "{", "}"}
# Package-manager verbs that legitimately take an egress tool's NAME as an argument (e.g.
# `pip install yt-dlp`). Installing a tool is not the same as running it, so a command whose
# leading program is one of these is not treated as egress on account of its arguments.
_PKG_MANAGERS = {"pip", "pip3", "pipx", "uv", "poetry", "conda", "mamba", "brew", "port"}


def _command_position_tokens(command: str) -> set[str] | None:
    """Return the set of tokens that appear in *command position* — the first token, and the
    first token after each shell separator (&&, ||, ;, |, subshell parens/braces). Basenames
    are included too, so `/usr/local/bin/yt-dlp` contributes `yt-dlp`.

    Returns None if the command can't be tokenized (unbalanced quotes) — callers should then
    fall back to their conservative default. This lets a sensitive verb be matched only when
    it's actually being invoked, not when it shows up inside a quoted string or a grep pattern
    (the real-world false positive: `grep -iE "...|publish|..."`)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    words: set[str] = set()
    expect_command = True
    for token in tokens:
        if token in _CMD_SEPARATORS:
            expect_command = True
            continue
        if expect_command:
            words.add(token)
            words.add(token.rsplit("/", 1)[-1])
            # env-var prefixes (FOO=bar cmd) and `env`/`sudo`/`nohup` wrappers keep the next
            # token in command position too.
            if "=" in token and not token.startswith("-"):
                continue
            if token.rsplit("/", 1)[-1] in {"env", "sudo", "nohup", "time", "nice", "xargs", "command"}:
                continue
            expect_command = False
    return words

def _invokes_bare_python(command: str) -> bool:
    """True when the command runs a *bare* Python interpreter (`python` / `python3`) in
    command position — i.e. resolved off PATH rather than the project venv.

    A token counts as bare only when it has no path component: `python3` matches, but
    `.venv/bin/python3`, `/usr/bin/python3`, `./python3`, and `$PY` (a variable) do NOT — those
    name an explicit interpreter and are allowed. We inspect command-position tokens (the first
    token, and the first token after each shell separator, honoring env-var prefixes and
    env/sudo/... wrappers) so a bare interpreter is caught in `foo && python3 x`, `env python3 x`,
    and `A=1 python3 x`, while `python3` appearing as an argument, a quoted literal, or a grep
    pattern is ignored. If the command can't be tokenized (unbalanced quotes), fall back to a
    conservative regex for a standalone bare name.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Bare `python`/`python3` as a standalone word, but not when preceded by a path
        # separator (`.venv/bin/python3`) — the negative lookbehind excludes path forms.
        return bool(re.search(r"(?<![\w./-])python3?(?![\w./-])", command))
    expect_command = True
    for token in tokens:
        if token in _CMD_SEPARATORS:
            expect_command = True
            continue
        if not expect_command:
            continue
        # env-var prefixes (FOO=bar cmd) and env/sudo/... wrappers keep command position on
        # the NEXT token — don't decide on the wrapper itself.
        if "=" in token and not token.startswith("-"):
            continue
        if token.rsplit("/", 1)[-1] in {"env", "sudo", "nohup", "time", "nice", "xargs", "command"}:
            continue
        # This token is the actual program being run. It is a bare interpreter only if it has
        # no path component (no "/") and matches a banned name.
        if "/" not in token and token in _BARE_PYTHON_NAMES:
            return True
        expect_command = False
    return False


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
        cmd_words = _command_position_tokens(command)
        # HF/whisper operations must always resolve models from the staged offline cache
        # (HuggingFace is network-blocked here). Assert the canonical HF cache env — into this
        # hook's own process (so any in-hook checks are correct) and into the audit record — for
        # any command that runs an HF-backed model tool, or the project CLI's transcript verbs
        # (which shell out to mlx-whisper). `.claude/settings.json` applies the same vars to every
        # tool call's environment; this keeps the reference authoritative even if `.env.local` was
        # never sourced. Non-blocking: it never denies or prompts — it just guarantees the path.
        runs_hf = bool(cmd_words and (cmd_words & _HF_OPERATION_NAMES)) or bool(
            re.search(r"\btranscript\s+(run|import|english-|qa)\b", command)
        ) or bool(re.search(r"\bdub\s+run\b", command))
        if runs_hf:
            hf_env = _hf_offline_env()
            os.environ.update(hf_env)
            record["hf_cache_asserted"] = HF_CACHE_DIR
            record["mms_stage_dirs"] = MMS_STAGE_DIRS
        # HARD deny: never invoke a bare `python`/`python3` — it resolves to the system
        # interpreter (missing the venv's deps). Always use `.venv/bin/python3` (or another
        # explicit interpreter path). This is a non-negotiable company rule, not a prompt.
        if _invokes_bare_python(command):
            reason = (
                "bare `python3`/`python` is forbidden — always run `.venv/bin/python3` "
                "(the project venv has jsonschema/yaml/piper etc.; the system python does not)"
            )
            record.update({"decision": "deny", "reason": reason})
            append_audit(root, record)
            structured_deny(reason)
        # Ingest/vendor egress is HARD-denied unless the sanctioned fetch flag is set — this
        # is the network-egress non-negotiable, not a case-by-case call. We deny only when
        # yt-dlp is *invoked* (a command-position token), not when it merely appears as an
        # argument — e.g. `pip install yt-dlp` installs the tool without any egress. If the
        # command can't be tokenized (None), fall back to the substring check to stay safe.
        if os.environ.get("VIDTRANS_FETCH_ENABLED", "0").strip().lower() not in {"1", "true", "on", "yes"}:
            invokes_ytdlp = (
                "yt-dlp" in cmd_words if cmd_words is not None
                else bool(re.search(r"\byt-dlp\b", command, re.IGNORECASE))
            )
            leading = None
            if cmd_words is not None:
                # a package-manager invocation (pip install yt-dlp) is an install, not egress
                try:
                    first = shlex.split(command)[0].rsplit("/", 1)[-1]
                    leading = first
                except (ValueError, IndexError):
                    leading = None
            if invokes_ytdlp and leading not in _PKG_MANAGERS:
                reason = "yt-dlp egress is disabled; set VIDTRANS_FETCH_ENABLED=1 to permit ingest"
                record.update({"decision": "deny", "reason": reason})
                append_audit(root, record)
                structured_deny(reason)
        # External publication verbs are HARD-denied unless explicitly enabled — the
        # pipeline stops at READY_FOR_REVIEW by policy.
        if os.environ.get("VIDTRANS_EXTERNAL_WRITES", "disabled").lower() != "enabled":
            # Specific API-write endpoints / URLs / flags — these are distinctive enough to
            # match anywhere in the command without false-positiving on ordinary text.
            endpoint_hit = re.search(
                r"\b(videos\.insert|captions\.insert|thumbnails\.set|playlistItems\.insert)\b"
                r"|--upload\b|\bupload-video\b"
                # Phase 6 platform write endpoints: X/Twitter v2 tweets + v1.1 media upload,
                # Telegram Bot API sendMessage/sendVideo, Discord webhook posts.
                r"|api\.(twitter|x)\.com/\d[\w./]*|upload\.twitter\.com"
                r"|api\.telegram\.org/bot|/webhooks?/\d+/",
                command,
                re.IGNORECASE,
            )
            # The bare verbs `publish`/`purchase` are matched ONLY in command position — as an
            # actual program being invoked — never as a quoted literal, a grep pattern
            # (`grep -iE "...|publish|..."`), or embedded in a path slug like
            # "publish-vid-trans". If the command can't be tokenized, fall back to the old
            # standalone-word check so we don't silently stop enforcing.
            if cmd_words is not None:
                verb_hit = bool(cmd_words & {"publish", "purchase"})
            else:
                verb_hit = bool(
                    re.search(r"(?<![\w./-])(publish|purchase)(?![\w./-])", command, re.IGNORECASE)
                )
            if endpoint_hit or verb_hit:
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
