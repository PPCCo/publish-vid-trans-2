from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigurationError, SecurityError

# ISO 639-1 language codes this framework knows about, plus the CJK regional
# variant used by TTS engines. Kept here so validators and the CLI share one list.
KNOWN_LANGUAGES = {
    "en", "ar", "fa", "ur", "zh", "zh-cn", "fr", "es", "pt", "ru",
    "de", "it", "tr", "hi", "ja", "ko", "nl", "pl", "cs", "hu",
}
# Languages whose captions must be measured in characters, not words, and wrapped
# without whitespace (§11 of the plan). Extend as needed.
CJK_LANGUAGES = {"zh", "zh-cn", "ja", "ko"}
# Right-to-left scripts needing bidi shaping in burned-in captions.
RTL_LANGUAGES = {"ar", "fa", "ur"}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def repo_root(start: Path | str | None = None) -> Path:
    configured = os.environ.get("VIDTRANS_REPO_ROOT")
    if configured:
        root = Path(configured).expanduser().resolve()
        if (root / ".claude").is_dir():
            return root
        raise ConfigurationError(f"VIDTRANS_REPO_ROOT has no .claude directory: {root}")
    current = Path(start or os.getcwd()).expanduser().resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".claude").is_dir() and (candidate / ".claude" / "CLAUDE.md").is_file():
            return candidate
    raise ConfigurationError("Could not locate repository root containing .claude/CLAUDE.md")


def ensure_within(path: Path | str, root: Path | str) -> Path:
    resolved = Path(path).expanduser().resolve()
    root_resolved = Path(root).expanduser().resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise SecurityError(f"Path escapes allowed root: {resolved}") from exc
    return resolved


def relative_to(path: Path | str, root: Path | str) -> str:
    return ensure_within(path, root).relative_to(Path(root).resolve()).as_posix()


def load_json(path: Path | str, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        if default is not None:
            return default
        raise FileNotFoundError(p)
    with p.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_yaml(path: Path | str, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        if default is not None:
            return default
        raise FileNotFoundError(p)
    with p.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return {} if data is None else data


def atomic_write_text(path: Path | str, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def atomic_write_json(path: Path | str, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


def atomic_write_yaml(path: Path | str, data: Any) -> None:
    atomic_write_text(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def sha256_path(path: Path | str) -> tuple[str, int]:
    p = Path(path)
    if p.is_file():
        return sha256_file(p), p.stat().st_size
    if not p.is_dir():
        raise FileNotFoundError(p)
    digest = hashlib.sha256()
    total = 0
    for child in sorted(x for x in p.rglob("*") if x.is_file()):
        rel = child.relative_to(p).as_posix().encode("utf-8")
        file_hash = bytes.fromhex(sha256_file(child).split(":", 1)[1])
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        digest.update(file_hash)
        total += child.stat().st_size
    return f"sha256:{digest.hexdigest()}", total


def hash_json(data: Any) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def slugify(value: str, max_length: int = 60) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    value = re.sub(r"-{2,}", "-", value)
    return value[:max_length].rstrip("-") or "item"


def random_token(length: int = 16) -> str:
    token = secrets.token_hex((length + 1) // 2)
    return token[:length]


def parse_csv(values: str | list[str] | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    output: list[str] = []
    for value in values:
        output.extend(part.strip() for part in value.split(",") if part.strip())
    return list(dict.fromkeys(output))


def executable(name: str) -> str | None:
    """Resolve a console-script / binary by name.

    Checks PATH first (`shutil.which`), then falls back to the directory holding the current
    interpreter (`sys.executable`) — i.e. the active venv's `bin/` (or `Scripts/` on Windows).
    Tools pip-installed into the project venv (kokoro, faster-whisper, …) land there, and the
    CLI is documented to run as `.venv/bin/python3 …` WITHOUT activating the venv, so that bin
    dir is not on PATH. Without this fallback, doctor would report a pip-installed engine as
    "not installed". Absolute paths / names containing a separator are passed straight through
    to `shutil.which`.

    NOTE: yt-dlp is deliberately NOT resolved through this venv-bindir fallback — see
    net/fetch.py._ytdlp() and doctor.py, which call `shutil.which("yt-dlp")` directly so a
    system/PATH install (e.g. Homebrew) is always preferred over a pip-installed copy in the
    project venv (the venv copy's isolated `certifi` bundle can fail behind a TLS-inspecting
    proxy even with SSL_CERT_FILE set)."""
    found = shutil.which(name)
    if found:
        return found
    if os.sep in name or (os.altsep and os.altsep in name):
        return None
    # Candidate bin dirs: the dir literally holding the interpreter (do NOT resolve() — the
    # venv's python3 is a symlink to the base interpreter, and following it would jump us out
    # of the venv), and sys.prefix's bin/Scripts (points at the venv root under a venv).
    bindirs = [Path(sys.executable).parent, Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")]
    seen: set[Path] = set()
    for bindir in bindirs:
        if bindir in seen:
            continue
        seen.add(bindir)
        for candidate in (bindir / name, bindir / f"{name}.exe"):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


@contextlib.contextmanager
def project_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_company_config(root: Path) -> dict[str, Any]:
    default = load_json(root / ".claude" / "config" / "company.default.json")
    local_path = root / ".claude" / "config" / "company.local.json"
    if local_path.exists():
        return deep_merge(default, load_json(local_path))
    return default


def load_tools_config(root: Path) -> dict[str, Any]:
    default = load_json(root / ".claude" / "config" / "tools.default.json")
    local_path = root / ".claude" / "config" / "tools.local.json"
    if local_path.exists():
        return deep_merge(default, load_json(local_path))
    return default
