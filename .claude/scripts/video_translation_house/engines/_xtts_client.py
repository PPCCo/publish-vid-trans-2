"""Thin, stdlib-only client that stands in for the Coqui `tts` CLI at the XTTS seam.

`tools.local.json` -> `tts.binary_overrides.xtts` points (via xtts_client.sh) at this script.
engines/tts.py `_build_command` builds the SAME argv it always did for the `tts` CLI:

    --text <t> --out_path <dst> --language_idx <lang> --model_name <m> [--speaker_wav <ref>]

This client parses that argv, forwards it as one JSON request to the persistent daemon
(xtts_worker.py) over a Unix socket, and relays the result as a normal process exit code +
stderr. So engines/tts.py's existing `returncode`/`dst.exists()` checks keep working verbatim —
tts.py and dubbing.py are unchanged.

If the daemon isn't running (cold socket, stale socket, or it idle-exited), this launches it
detached and waits for it to come up. IMPORTANT: this file imports ONLY the stdlib — never torch
/ TTS — so it is cheap to spawn per cue and safe even if accidentally run under the wrong Python.
The heavy import lives only in the daemon.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

_DEFAULT_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"


def _repo_root() -> Path:
    # This file: <root>/.claude/scripts/video_translation_house/engines/_xtts_client.py
    return Path(__file__).resolve().parents[4]


def _socket_path(root: Path) -> Path:
    env = os.environ.get("VIDTRANS_XTTS_SOCKET")
    if env:
        p = Path(env)
        return p if p.is_absolute() else (root / p)
    return root / ".claude" / "state" / "xtts-worker.sock"


def _connect(sock_path: Path, timeout: float = 5.0) -> socket.socket | None:
    if not sock_path.exists():
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
        return s
    except OSError:
        s.close()
        return None


def _launch_daemon(root: Path, sock_path: Path, model_name: str, idle_timeout: float) -> None:
    """Spawn the daemon detached, under THIS interpreter (the .venv-xtts python running us)."""
    worker = root / ".venv-xtts" / "xtts_worker.py"
    log_dir = root / ".claude" / "state"
    log_dir.mkdir(parents=True, exist_ok=True)
    logf = open(log_dir / "xtts-worker.log", "ab")  # noqa: SIM115 - handed to the detached child
    env = {**os.environ, "COQUI_TOS_AGREED": "1"}
    subprocess.Popen(  # noqa: S603 - fixed args, no shell
        [sys.executable, str(worker), "--socket", str(sock_path),
         "--model-name", model_name, "--idle-timeout", str(idle_timeout)],
        stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
        start_new_session=True, env=env,
    )


def _ensure_connection(root: Path, sock_path: Path, model_name: str,
                       startup_timeout: float, idle_timeout: float) -> socket.socket:
    s = _connect(sock_path)
    if s is not None:
        return s
    # Stale socket file with no listener -> remove so bind() succeeds, then launch.
    if sock_path.exists():
        try:
            sock_path.unlink()
        except OSError:
            pass
    _launch_daemon(root, sock_path, model_name, idle_timeout)
    deadline = time.monotonic() + startup_timeout
    delay = 0.25
    while time.monotonic() < deadline:
        time.sleep(delay)
        s = _connect(sock_path)
        if s is not None:
            return s
        delay = min(delay * 1.5, 2.0)
    raise TimeoutError(
        f"xtts daemon did not come up within {startup_timeout:.0f}s "
        f"(see {root / '.claude' / 'state' / 'xtts-worker.log'})"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="XTTS daemon client (drop-in for the `tts` CLI)")
    ap.add_argument("--text", required=True)
    ap.add_argument("--out_path", required=True)
    ap.add_argument("--language_idx", required=True)
    ap.add_argument("--model_name", default=_DEFAULT_MODEL)
    ap.add_argument("--speaker_wav")
    # Tolerate any extra flags the framework might append without failing the cue.
    args, _unknown = ap.parse_known_args(argv)

    root = _repo_root()
    sock_path = _socket_path(root)
    startup_timeout = float(os.environ.get("VIDTRANS_XTTS_STARTUP_TIMEOUT", "90"))
    idle_timeout = float(os.environ.get("VIDTRANS_XTTS_IDLE_TIMEOUT", "1800"))
    model_name = args.model_name or _DEFAULT_MODEL

    req = {
        "text": args.text, "out_path": args.out_path,
        "language_idx": args.language_idx, "model_name": model_name,
        "speaker_wav": args.speaker_wav,
    }

    # One retry: if the daemon rejects a model mismatch (stale daemon from a prior model) or the
    # connection drops mid-request, restart a fresh daemon once and retry.
    for attempt in range(2):
        try:
            s = _ensure_connection(root, sock_path, model_name, startup_timeout, idle_timeout)
        except TimeoutError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        try:
            with s:
                s.sendall((json.dumps(req) + "\n").encode("utf-8"))
                buf = b""
                s.settimeout(600.0)
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            resp = json.loads(buf.decode("utf-8")) if buf else {"ok": False, "error": "empty response"}
        except (OSError, json.JSONDecodeError) as exc:
            if attempt == 0:
                # Force a fresh daemon and retry once.
                if sock_path.exists():
                    try:
                        sock_path.unlink()
                    except OSError:
                        pass
                continue
            print(f"xtts client transport error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        if resp.get("ok"):
            return 0
        error = resp.get("error", "unknown error")
        if attempt == 0 and "restart the daemon" in error:
            # Model mismatch on a stale daemon: drop it and retry with our model.
            if sock_path.exists():
                try:
                    sock_path.unlink()
                except OSError:
                    pass
            continue
        print(f"xtts synthesis failed: {error}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
