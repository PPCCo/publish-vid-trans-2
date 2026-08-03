"""TTS (text-to-speech) engine adapter.

Design constraints (ANALYSIS.md D, E) — identical posture to ``engines/asr.py``:
  * NEVER import torch / mlx / any TTS library into this process. Each provider runs as a
    *subprocess* invoking that engine's own CLI, so the deterministic CLI stays pure.
  * Every provider is optional. If the engine is not installed we raise
    ``EngineUnavailableError`` — callers defer to a human or a pre-rendered import, they do
    not crash.

Provider selection is *language-aware*: a per-language default map lives in
``tools.default.json`` (en->kokoro, fa/ar/ur->piper, zh/…->xtts). This adapter only builds
the command line, runs it, and reports the WAV the child produced. Voice cloning is a pure
mechanism here (``clone_ref``); the CONSENT policy that decides whether cloning is permitted
lives in ``dubbing.py`` and the rights record — never bypass it by calling this directly.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import EngineUnavailableError, VideoTranslationHouseError
from ..util import executable, load_tools_config

# Providers we know how to drive via subprocess. Local-first order; vendor engines are
# recognized names but still binary-gated (opt-in).
KNOWN_PROVIDERS = ("kokoro", "piper", "xtts", "chatterbox", "elevenlabs", "azure", "google")

# The engine binary each provider needs on PATH.
_PROVIDER_BINARY = {
    "kokoro": "kokoro",
    "piper": "piper",
    "xtts": "tts",            # Coqui XTTS ships the `tts` CLI
    "chatterbox": "chatterbox",
    "elevenlabs": "elevenlabs",
    "azure": "spx",           # Azure Speech CLI
    "google": "gcloud",
}


@dataclass
class TTSResult:
    provider: str
    model: str | None
    wav_path: Path
    sample_rate: int | None
    duration_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "provider": self.provider,
            "wav_path": str(self.wav_path),
        }
        if self.model is not None:
            d["model"] = self.model
        if self.sample_rate is not None:
            d["sample_rate"] = self.sample_rate
        if self.duration_ms is not None:
            d["duration_ms"] = self.duration_ms
        return d


def available_tts_providers() -> list[str]:
    """Return the subset of KNOWN_PROVIDERS whose engine binary is on PATH."""
    return [p for p in KNOWN_PROVIDERS if executable(_PROVIDER_BINARY[p])]


def _configured_provider(root: Path | None, language: str) -> str | None:
    """The per-language (or default) provider from tools config, if configured."""
    if root is None:
        return None
    try:
        tools = load_tools_config(root)
    except Exception:  # noqa: BLE001 - config optional; fall back to availability order
        return None
    tts = tools.get("tts", {})
    per_lang = tts.get("per_language", {})
    return per_lang.get(language) or tts.get("default_provider")


def _resolve_provider(requested: str | None, language: str, *, root: Path | None = None) -> str:
    """Pick a TTS provider: an explicit request wins (if installed); else the configured
    per-language/default provider (if installed); else the first available engine."""
    available = available_tts_providers()
    if requested:
        normalized = requested.strip().lower()
        if normalized not in KNOWN_PROVIDERS:
            raise VideoTranslationHouseError(
                f"Unknown TTS provider {requested!r}; known: {', '.join(KNOWN_PROVIDERS)}"
            )
        if normalized not in available:
            raise EngineUnavailableError(
                f"TTS provider {normalized!r} requested but its binary "
                f"({_PROVIDER_BINARY[normalized]}) is not on PATH. Install it, import a "
                f"pre-rendered dub, or choose an available provider "
                f"({', '.join(available) or 'none installed'})."
            )
        return normalized

    configured = _configured_provider(root, language)
    if configured and configured.lower() in available:
        return configured.lower()

    if not available:
        raise EngineUnavailableError(
            f"No TTS engine installed for {language!r}. Install kokoro/piper/xtts, or dub "
            "out-of-band and use `dub import`. All TTS engines are opt-in."
        )
    return available[0]


def _build_command(
    provider: str, text: str, dst: Path, *,
    model: str | None, voice: str | None, language: str, clone_ref: Path | None,
) -> list[str]:
    """Build one cue's synthesis command. Kept deliberately conservative — the exact flags
    vary by engine version; operators can wrap their engine so these defaults apply."""
    binary = _PROVIDER_BINARY[provider]
    if provider == "piper":
        cmd = [binary, "--output_file", str(dst)]
        if model:
            cmd += ["--model", model]
        return cmd  # piper reads text from stdin
    if provider == "kokoro":
        cmd = [binary, "--text", text, "--output", str(dst), "--lang", language]
        if voice:
            cmd += ["--voice", voice]
        return cmd
    if provider == "xtts":  # Coqui `tts`
        cmd = [binary, "--text", text, "--out_path", str(dst), "--language_idx", language]
        if model:
            cmd += ["--model_name", model]
        if clone_ref is not None:
            cmd += ["--speaker_wav", str(clone_ref)]
        return cmd
    # Vendor engines: generic best-effort shape (operator-wrapped).
    cmd = [binary, "--text", text, "--output", str(dst), "--language", language]
    if voice:
        cmd += ["--voice", voice]
    return cmd


def synthesize_cue(
    text: str,
    dst_wav: Path | str,
    *,
    provider: str | None = None,
    language: str,
    model: str | None = None,
    voice: str | None = None,
    clone_ref: Path | str | None = None,
    root: Path | None = None,
    timeout: int = 600,
) -> TTSResult:
    """Synthesize one caption cue to ``dst_wav`` with the resolved provider.

    Raises ``EngineUnavailableError`` if no suitable engine is installed. ``clone_ref`` is a
    reference speaker WAV; passing it is a pure mechanism — the caller MUST have verified
    ``voice_clone_consent`` before supplying it.
    """
    dst = Path(dst_wav)
    dst.parent.mkdir(parents=True, exist_ok=True)
    resolved = _resolve_provider(provider, language, root=root)
    ref = Path(clone_ref) if clone_ref is not None else None

    command = _build_command(
        resolved, text, dst, model=model, voice=voice, language=language, clone_ref=ref,
    )
    stdin_text = text if resolved == "piper" else None
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary, path args, no shell
            command, input=stdin_text, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise EngineUnavailableError(f"TTS engine binary not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoTranslationHouseError(f"TTS timed out after {timeout}s: {command[0]}") from exc
    if result.returncode != 0:
        raise VideoTranslationHouseError(
            f"{resolved} failed (exit {result.returncode}): {(result.stderr or '').strip()[:500]}"
        )
    if not dst.exists():
        raise VideoTranslationHouseError(f"{resolved} reported success but produced no WAV at {dst}")

    # Duration/sample-rate are measured by the caller via media.probe_summary to avoid a
    # media import cycle here; leave them None (the adapter's job ends at "a WAV exists").
    return TTSResult(provider=resolved, model=model, wav_path=dst,
                     sample_rate=None, duration_ms=None)
