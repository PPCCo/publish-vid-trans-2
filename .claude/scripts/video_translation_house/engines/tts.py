"""TTS (text-to-speech) engine adapter.

Design constraints (ANALYSIS.md D, E) — identical posture to ``engines/asr.py``:
  * NEVER import torch / mlx / any TTS library into this process. Each provider runs as a
    *subprocess* invoking that engine's own CLI, so the deterministic CLI stays pure.
  * Every provider is optional. If the engine is not installed we raise
    ``EngineUnavailableError`` — callers defer to a human or a pre-rendered import, they do
    not crash.

Provider selection is *language-aware*: a per-language engine map lives in
``tools.default.json`` (``tts.per_language``). This adapter only builds the command line, runs
it, and reports the WAV the child produced. Voice cloning is a pure mechanism here
(``clone_ref``); the CONSENT policy that decides whether cloning is permitted lives in
``dubbing.py`` and the rights record — never bypass it by calling this directly.

SOURCE OF TRUTH for clone-vs-piper is NOT this file. Which languages are dubbed by XTTS
voice-clone vs the piper gender registry is decided in ``dubbing.run_dub`` from company config
``dubbing.clone_languages`` (default ["en","zh"]). By the time a clone/registry language reaches
``synthesize_cue`` its ``provider`` is already resolved, so ``tts.per_language`` here is only the
*fallback engine* for a language ``dub run`` left unresolved (``provider=None``) — see
``_resolve_provider``/``_configured_provider``. Do not treat ``tts.per_language`` as the clone
policy; keep the two in sync (both list en/zh as xtts) but change the policy in company config.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import budget as budget_mod
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

# XTTS-v2's model manifest speaks a couple of regional-variant codes that differ from this
# framework's canonical KNOWN_LANGUAGES codes (util.py). Remap scoped to the xtts provider only
# — the pipeline's own `zh` stays canonical everywhere else (captions, state, other providers).
_XTTS_LANGUAGE_MAP = {"zh": "zh-cn"}

# The `tts` CLI's own hardcoded default model is a single-speaker English model, not XTTS —
# without an explicit --model_name it ignores any staged offline cache and tries to fetch its
# default from the (policy-blocked) huggingface.co. `run_dub`'s clone path never sets `model`
# (it skips the gender/model registry lookup entirely when cloning), so this is the only place
# XTTS-v2 gets named; a company/operator --model override still wins (passed through untouched).
_XTTS_DEFAULT_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"


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


def available_tts_providers(root: Path | None = None) -> list[str]:
    """Return the subset of KNOWN_PROVIDERS whose engine binary is on PATH (or, for a provider
    with a configured `tools.local.json` binary override, resolvable via that override)."""
    return [
        p for p in KNOWN_PROVIDERS
        if executable(_PROVIDER_BINARY[p]) or _binary_override(root, p)
    ]


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
    available = available_tts_providers(root)
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


def _binary_override(root: Path | None, provider: str) -> str | None:
    """An operator-pinned absolute binary path for this provider, if configured.

    Needed for engines installed into a venv *other* than the CLI's own (e.g. XTTS/coqui-tts
    lives in an isolated `.venv-xtts` to avoid disturbing the framework's pinned Python version
    — `executable()`'s PATH + sys.executable-bindir fallback can never see into a sibling venv).
    Configured at `tools.local.json` → `tts.binary_overrides.<provider>` (gitignored, machine-
    specific paths never committed)."""
    if root is None:
        return None
    try:
        tools = load_tools_config(root)
    except Exception:  # noqa: BLE001 - config optional
        return None
    overrides = tools.get("tts", {}).get("binary_overrides", {})
    path = overrides.get(provider)
    return path if path and Path(path).is_file() else None


def _build_command(
    provider: str, text: str, dst: Path, *,
    model: str | None, voice: str | None, language: str, clone_ref: Path | None,
    root: Path | None = None,
) -> list[str]:
    """Build one cue's synthesis command. Kept deliberately conservative — the exact flags
    vary by engine version; operators can wrap their engine so these defaults apply."""
    # Resolve to an absolute path (PATH + venv bin) so the subprocess finds a venv-installed
    # engine even when the venv isn't activated — the CLI runs as `.venv/bin/python3 …`.
    # Falls back to the bare name so the caller's FileNotFoundError path still reports it.
    binary = (
        _binary_override(root, provider)
        or executable(_PROVIDER_BINARY[provider])
        or _PROVIDER_BINARY[provider]
    )
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
        xtts_language = _XTTS_LANGUAGE_MAP.get(language, language)
        cmd = [binary, "--text", text, "--out_path", str(dst), "--language_idx", xtts_language]
        cmd += ["--model_name", model or _XTTS_DEFAULT_MODEL]
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
    project_id: str | None = None,
    timeout: int = 600,
) -> TTSResult:
    """Synthesize one caption cue to ``dst_wav`` with the resolved provider.

    Raises ``EngineUnavailableError`` if no suitable engine is installed. Raises
    ``BudgetExceededError`` if the resolved provider is a billed vendor engine and the
    call would exceed the human-set ``budget.vendor_spend_ceiling_usd`` (requires
    ``root`` — without it, vendor calls cannot be spend-checked and are refused).
    ``clone_ref`` is a reference speaker WAV; passing it is a pure mechanism — the
    caller MUST have verified ``voice_clone_consent`` before supplying it.
    """
    dst = Path(dst_wav)
    dst.parent.mkdir(parents=True, exist_ok=True)
    resolved = _resolve_provider(provider, language, root=root)
    if budget_mod.is_vendor_provider(resolved):
        if root is None:
            raise EngineUnavailableError(
                f"vendor TTS provider {resolved!r} requires a repo root to check the spend "
                "ceiling; pass root= or use a local engine."
            )
        budget_mod.check_and_record(root, resolved, project_id=project_id, language=language)
    ref = Path(clone_ref) if clone_ref is not None else None

    command = _build_command(
        resolved, text, dst, model=model, voice=voice, language=language, clone_ref=ref,
        root=root,
    )
    stdin_text = text if resolved == "piper" else None
    env = None
    if resolved == "xtts":
        # XTTS-v2's model download is gated behind an interactive Coqui Public Model License
        # (CPML) prompt on first use; this process is never interactive. COQUI_TOS_AGREED=1
        # auto-accepts it — set only for this subprocess, only for xtts, and only after a human
        # has explicitly consented to CPML's non-commercial terms (see CLAUDE.md rule 5 /
        # OPERATING-GUIDE.md; this is a recorded licensing decision, not a silent bypass).
        env = {**os.environ, "COQUI_TOS_AGREED": "1"}
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary, path args, no shell
            command, input=stdin_text, capture_output=True, text=True,
            timeout=timeout, check=False, env=env,
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
