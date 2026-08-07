from __future__ import annotations

import importlib.util
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from .util import executable, load_json, load_tools_config
from .validation import validate_framework


def _version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or result.stderr or "").strip().splitlines()
    return text[0] if text else None


def run_doctor(root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str, required: bool = False) -> None:
        checks.append({"name": name, "status": status, "detail": detail, "required": required})

    # Python + pure-Python deps (required — the CLI itself).
    py_ok = sys.version_info >= (3, 11)
    add("python", "pass" if py_ok else "fail", platform.python_version(), True)
    for module, required in [("yaml", True), ("jsonschema", True), ("mcp", False)]:
        found = importlib.util.find_spec(module) is not None
        add(f"python:{module}",
            "pass" if found else ("fail" if required else "optional-missing"),
            "installed" if found else "not installed", required)

    # Media tooling (ffmpeg required for extraction/mux; yt-dlp required for ingest).
    for tool, required in [("ffmpeg", True), ("ffprobe", True), ("yt-dlp", True), ("rubberband", False)]:
        path = executable(tool)
        if not path:
            version = None
        else:
            version = _version([tool, "-version" if tool.startswith("ff") else "--version"])
        add(f"tool:{tool}",
            "pass" if path else ("fail" if required else "optional-missing"),
            version or ("installed" if path else "not installed"), required)

    # Engine adapters — ALL optional; the pipeline validates without them, degrades gracefully.
    # (Local-first defaults on this Apple-Silicon machine; vendor APIs are opt-in.)
    engine_modules = [
        ("mlx_whisper", "ASR (Apple MLX, default)"),
        ("faster_whisper", "ASR (CTranslate2, CPU on Mac)"),
        ("whisperx", "word-level alignment / diarization"),
        ("piper", "TTS (fa/ur baseline)"),
        ("kokoro", "TTS (English)"),
        ("TTS", "Coqui XTTS (zh/fr/es/pt/ru, non-commercial)"),
    ]
    for module, desc in engine_modules:
        found = importlib.util.find_spec(module) is not None
        add(f"engine:{module}", "pass" if found else "optional-missing",
            f"{desc} — {'installed' if found else 'not installed (opt-in)'}", False)

    # Framework self-check.
    fw = validate_framework(root)
    add("framework-validate", "pass" if fw["valid"] else "fail",
        f"{sum(1 for c in fw['checks'] if c['status'] == 'pass')}/{len(fw['checks'])} checks", True)

    # Settings / external-write posture.
    settings_path = root / ".claude" / "settings.json"
    try:
        settings = load_json(settings_path)
        writes = settings.get("env", {}).get("VIDTRANS_EXTERNAL_WRITES")
        add("external-writes-default", "pass" if writes == "disabled" else "warn", str(writes), True)
    except Exception as exc:  # noqa: BLE001
        add("settings", "fail", str(exc), True)

    # Configured engine providers (informational).
    try:
        tools = load_tools_config(root)
        for cap in ("asr", "translation", "tts"):
            prov = tools.get(cap, {}).get("provider")
            add(f"config:{cap}-provider", "pass", str(prov), False)
        baseline = tools.get("translation", {}).get("mt_baseline")
        add("config:mt-baseline", "pass", str(baseline),
            False)
    except Exception as exc:  # noqa: BLE001
        add("tools-config", "warn", str(exc), False)

    # MT engine binaries (drive the scripted/manual translation-fill route; all opt-in).
    try:
        from .engines import mt as mt_mod
        avail = mt_mod.available_mt_providers()
        add("engine:mt", "pass" if avail else "optional-missing",
            (", ".join(avail) if avail else
             "no MT engine installed (opt-in — needed only for --mt scripted translation)"),
            False)
    except Exception as exc:  # noqa: BLE001
        add("engine:mt", "warn", str(exc), False)

    required_fail = any(c["status"] == "fail" and c["required"] for c in checks)
    return {"status": "fail" if required_fail else "pass", "checks": checks}
