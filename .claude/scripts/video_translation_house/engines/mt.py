"""MT (machine-translation) engine adapter.

Design constraints (ANALYSIS.md D, E) — identical posture to ``engines/asr.py`` and
``engines/tts.py``:
  * NEVER import a translation library (argostranslate / ctranslate2 / transformers / torch)
    into this process. Each provider runs as a *subprocess* invoking that engine's own CLI,
    so the deterministic CLI stays pure and the heavy model load happens in a child process.
  * Every provider is optional. If the engine is not installed we raise
    ``EngineUnavailableError`` — callers defer to a human (or to Claude-authored translation,
    or to the ``translate import`` path) rather than crash.

This adapter is what makes the *scripted / manual* route (``project autopilot --mt`` /
``run_pipeline.sh``) able to fill translation worksheets without spending Claude tokens:
``translate.machine_fill_worksheet`` calls :func:`translate_text` per empty cue.

Provider selection reads ``tools.default.json -> translation.mt_baseline`` (overridable in a
gitignored ``tools.local.json``), falling back to the first installed engine. HuggingFace is
policy-blocked on this network (project memory: ``hf-offline-model-staging``) — the operator
stages whatever MT model their chosen engine needs offline (the GitHub-staging recipe in
OPERATING-GUIDE.md §6); this adapter never touches the network, it only shells out to a
locally installed binary.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..errors import EngineUnavailableError, VideoTranslationHouseError
from ..util import executable, load_tools_config

# Providers we know how to drive via subprocess, in local-first preference order.
KNOWN_PROVIDERS = ("argos", "ctranslate2", "nllb", "opus-mt")

# The engine binary each provider needs on PATH. All are opt-in; none ships with the framework.
#   argos       -> argos-translate CLI (argospm installs offline .argosmodel packages)
#   ctranslate2 -> a ct2 translate wrapper CLI (operator-provided; e.g. NLLB/M2M100 in CT2)
#   nllb        -> operator NLLB CLI wrapper
#   opus-mt     -> operator Marian/OPUS-MT CLI wrapper
_PROVIDER_BINARY = {
    "argos": "argos-translate",
    "ctranslate2": "ct2-translate",
    "nllb": "nllb-translate",
    "opus-mt": "opus-mt",
}


def available_mt_providers() -> list[str]:
    """Return the subset of KNOWN_PROVIDERS whose engine binary is on PATH."""
    return [p for p in KNOWN_PROVIDERS if executable(_PROVIDER_BINARY[p])]


def _configured_provider(root: Path | None) -> str | None:
    """The MT baseline provider from tools config (``translation.mt_baseline``), if set."""
    if root is None:
        return None
    try:
        tools = load_tools_config(root)
    except Exception:  # noqa: BLE001 - config optional; fall back to availability order
        return None
    baseline = tools.get("translation", {}).get("mt_baseline")
    return str(baseline) if baseline else None


def _resolve_provider(requested: str | None, *, root: Path | None = None) -> str:
    """Pick an MT provider: an explicit request wins (if installed); else the configured
    ``translation.mt_baseline`` (if installed); else the first available engine."""
    available = available_mt_providers()
    if requested:
        normalized = requested.strip().lower()
        if normalized not in KNOWN_PROVIDERS:
            raise VideoTranslationHouseError(
                f"Unknown MT provider {requested!r}; known: {', '.join(KNOWN_PROVIDERS)}"
            )
        if normalized not in available:
            raise EngineUnavailableError(
                f"MT provider {normalized!r} requested but its binary "
                f"({_PROVIDER_BINARY[normalized]}) is not on PATH. Install it (stage the model "
                f"offline — see OPERATING-GUIDE.md §6), let Claude translate, or choose an "
                f"available provider ({', '.join(available) or 'none installed'})."
            )
        return normalized

    configured = _configured_provider(root)
    if configured and configured.lower() in available:
        return configured.lower()

    if not available:
        raise EngineUnavailableError(
            "No MT engine installed. Install argos-translate (offline .argosmodel packages) or "
            "another supported engine, translate via Claude, or fill the worksheet by hand. "
            "All MT engines are opt-in."
        )
    return available[0]


def _binary(name: str) -> str:
    """Absolute path to an engine binary (PATH + venv bin), mirroring asr._binary."""
    return executable(name) or name


def _build_command(
    provider: str, src_lang: str, tgt_lang: str, *, model: str | None,
) -> tuple[list[str], bool]:
    """Build the per-provider translate command. Returns (command, text_via_stdin).

    Kept conservative — exact flags vary by engine version; operators can wrap their engine so
    these defaults apply. All supported providers read the source text on **stdin** and write
    the translation to **stdout** (one text in, one text out), which keeps the adapter simple
    and avoids temp files per cue."""
    if provider == "argos":
        # argos-translate --from-code fa --to-code en   (reads stdin, writes stdout)
        cmd = [_binary("argos-translate"), "--from-code", src_lang, "--to-code", tgt_lang]
        return cmd, True
    if provider == "ctranslate2":
        cmd = [_binary("ct2-translate"), "--source-lang", src_lang, "--target-lang", tgt_lang]
        if model:
            cmd += ["--model", model]
        return cmd, True
    if provider == "nllb":
        cmd = [_binary("nllb-translate"), "--src", src_lang, "--tgt", tgt_lang]
        if model:
            cmd += ["--model", model]
        return cmd, True
    # opus-mt
    cmd = [_binary("opus-mt"), "--source", src_lang, "--target", tgt_lang]
    if model:
        cmd += ["--model", model]
    return cmd, True


def translate_text(
    text: str,
    src_lang: str,
    tgt_lang: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    root: Path | None = None,
    timeout: int = 600,
) -> str:
    """Translate ``text`` from ``src_lang`` to ``tgt_lang`` with the resolved MT provider.

    Raises ``EngineUnavailableError`` if no suitable engine is installed. Pure text-in /
    text-out (source on the child's stdin, translation from its stdout) — no network, no
    in-process ML import."""
    if not text.strip():
        return ""
    resolved = _resolve_provider(provider, root=root)
    command, _stdin = _build_command(resolved, src_lang, tgt_lang, model=model)
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary, path args, no shell
            command, input=text, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:  # binary vanished between the PATH check and now
        raise EngineUnavailableError(f"MT engine binary not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoTranslationHouseError(f"MT timed out after {timeout}s: {command[0]}") from exc
    if result.returncode != 0:
        raise VideoTranslationHouseError(
            f"{resolved} failed (exit {result.returncode}): {(result.stderr or '').strip()[:500]}"
        )
    out = (result.stdout or "").strip()
    if not out:
        raise VideoTranslationHouseError(
            f"{resolved} produced no translation for a non-empty source cue"
        )
    return out
