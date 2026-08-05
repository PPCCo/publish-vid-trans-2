"""ASR (speech-to-text) engine adapter.

Design constraints (ANALYSIS.md D, E):
  * NEVER import torch / mlx / whisper into this process. Each provider runs as a
    *subprocess* invoking that engine's own CLI, so the deterministic CLI stays pure.
  * Every provider is optional. If the engine is not installed we raise
    ``EngineUnavailableError`` — callers defer to a human, they do not crash.
  * Segment-level timing is the guaranteed contract. Word-level timing is emitted only
    when the engine natively produces it; downstream must treat it as optional (B3).

Default provider is ``mlx-whisper`` (Apple-Silicon, Metal). ``faster-whisper`` is the
CPU fallback. Both write a JSON transcript we normalize into a provider-neutral shape.

The heavy work (model load, inference) happens in the child process; here we only build
the command line, run it, and parse the JSON it leaves behind.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import EngineUnavailableError, VideoTranslationHouseError
from ..util import executable

# Providers we know how to drive via subprocess, in default-preference order.
KNOWN_PROVIDERS = ("mlx-whisper", "faster-whisper")

# The engine binaries each provider needs on PATH.
_PROVIDER_BINARY = {
    "mlx-whisper": "mlx_whisper",
    "faster-whisper": "faster-whisper",
}


@dataclass
class TranscriptCue:
    """One timed segment of source-language speech (the downstream contract unit)."""

    id: int
    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None
    no_speech_prob: float | None = None
    words: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
        }
        if self.confidence is not None:
            d["confidence"] = self.confidence
        if self.no_speech_prob is not None:
            d["no_speech_prob"] = self.no_speech_prob
        if self.words:
            d["words"] = self.words
        return d


@dataclass
class ASRResult:
    provider: str
    model: str | None
    language: str | None
    cues: list[TranscriptCue]
    has_word_timing: bool
    duration_seconds: float | None = None
    word_alignment: str | None = None


def available_asr_providers() -> list[str]:
    """Return the subset of KNOWN_PROVIDERS whose engine binary is on PATH."""
    return [p for p in KNOWN_PROVIDERS if executable(_PROVIDER_BINARY[p])]


def _resolve_provider(requested: str | None) -> str:
    """Pick a provider: honor an explicit request if installed, else first available."""
    available = available_asr_providers()
    if requested:
        normalized = requested.strip().lower()
        if normalized not in KNOWN_PROVIDERS:
            raise VideoTranslationHouseError(
                f"Unknown ASR provider {requested!r}; known: {', '.join(KNOWN_PROVIDERS)}"
            )
        if normalized not in available:
            raise EngineUnavailableError(
                f"ASR provider {normalized!r} requested but its binary "
                f"({_PROVIDER_BINARY[normalized]}) is not on PATH. Install it or choose "
                f"an available provider ({', '.join(available) or 'none installed'})."
            )
        return normalized
    if not available:
        raise EngineUnavailableError(
            "No ASR engine installed. Install mlx-whisper (Apple Silicon) or "
            "faster-whisper, or transcribe manually. All ASR engines are opt-in."
        )
    return available[0]


def _seconds_to_ms(value: Any) -> int:
    try:
        return max(0, round(float(value) * 1000))
    except (TypeError, ValueError):
        return 0


def _normalize_whisper_json(data: dict[str, Any]) -> tuple[list[TranscriptCue], bool]:
    """Both mlx-whisper and faster-whisper emit Whisper-shaped JSON: a top-level
    ``segments`` list with start/end (seconds), text, and optionally per-word timing."""
    cues: list[TranscriptCue] = []
    has_words = False
    for idx, seg in enumerate(data.get("segments", []) or []):
        words_out: list[dict[str, Any]] = []
        for w in seg.get("words", []) or []:
            token = w.get("word") if "word" in w else w.get("text")
            if token is None:
                continue
            prob = w.get("probability")
            words_out.append({
                "word": str(token),
                "start_ms": _seconds_to_ms(w.get("start")),
                "end_ms": _seconds_to_ms(w.get("end")),
                "confidence": prob if prob is not None else w.get("confidence"),
            })
        if words_out:
            has_words = True
        cues.append(TranscriptCue(
            id=idx,
            start_ms=_seconds_to_ms(seg.get("start")),
            end_ms=_seconds_to_ms(seg.get("end")),
            text=str(seg.get("text", "")).strip(),
            confidence=seg.get("avg_logprob"),
            no_speech_prob=seg.get("no_speech_prob"),
            words=words_out,
        ))
    return cues, has_words


def _binary(name: str) -> str:
    """Absolute path to an engine binary, resolved via util.executable (PATH + venv bin).

    The subprocess is spawned WITHOUT the venv on PATH (the CLI runs as
    `.venv/bin/python3 …` unactivated), so passing the bare name would make
    `subprocess.run` fail to find a venv-installed engine even though the availability
    check found it. Resolving to an absolute path here keeps the two in lockstep. Falls
    back to the bare name (so the caller's FileNotFoundError → EngineUnavailableError path
    still produces a clear message) if resolution fails."""
    return executable(name) or name


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:  # binary vanished between the PATH check and now
        raise EngineUnavailableError(f"ASR engine binary not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoTranslationHouseError(f"ASR timed out after {timeout}s: {command[0]}") from exc


def _transcribe_mlx(
    audio: Path, out_dir: Path, *, model: str | None, language: str | None,
    word_timestamps: bool, timeout: int,
    condition_on_previous_text: bool = True,
    hallucination_silence_threshold: float | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    command = [_binary("mlx_whisper"), str(audio), "--output-dir", str(out_dir), "--output-format", "json"]
    if model:
        command += ["--model", model]
    if language:
        command += ["--language", language]
    if word_timestamps:
        command += ["--word-timestamps", "True"]
    # Decode-control knobs (mlx-whisper CLI uses hyphenated flag names). These are the levers
    # against repetition-hallucination loops on hard audio (recitation/music): disabling
    # condition-on-previous-text stops the decoder from parroting its own prior output, and the
    # hallucination-silence-threshold skips likely hallucinations across long silences.
    if not condition_on_previous_text:
        command += ["--condition-on-previous-text", "False"]
    if hallucination_silence_threshold is not None:
        command += ["--hallucination-silence-threshold", str(hallucination_silence_threshold)]
    if temperature is not None:
        command += ["--temperature", str(temperature)]
    result = _run(command, timeout=timeout)
    if result.returncode != 0:
        raise VideoTranslationHouseError(
            f"mlx_whisper failed (exit {result.returncode}): {(result.stderr or '').strip()[:500]}"
        )
    produced = sorted(out_dir.glob(f"{audio.stem}*.json"))
    if not produced:
        raise VideoTranslationHouseError("mlx_whisper produced no JSON output")
    return json.loads(produced[-1].read_text(encoding="utf-8"))


def _transcribe_faster(
    audio: Path, out_dir: Path, *, model: str | None, language: str | None,
    word_timestamps: bool, timeout: int,
    condition_on_previous_text: bool = True,
    hallucination_silence_threshold: float | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    out_file = out_dir / f"{audio.stem}.json"
    command = [_binary("faster-whisper"), str(audio), "--output_format", "json", "--output_dir", str(out_dir)]
    if model:
        command += ["--model", model]
    if language:
        command += ["--language", language]
    if word_timestamps:
        command += ["--word_timestamps", "True"]
    # Decode-control knobs (faster-whisper CLI uses underscored flag names — mirror of the mlx
    # path). Same purpose: suppress repetition-hallucination loops on difficult audio.
    if not condition_on_previous_text:
        command += ["--condition_on_previous_text", "False"]
    if hallucination_silence_threshold is not None:
        command += ["--hallucination_silence_threshold", str(hallucination_silence_threshold)]
    if temperature is not None:
        command += ["--temperature", str(temperature)]
    result = _run(command, timeout=timeout)
    if result.returncode != 0:
        raise VideoTranslationHouseError(
            f"faster-whisper failed (exit {result.returncode}): {(result.stderr or '').strip()[:500]}"
        )
    produced = sorted(out_dir.glob(f"{audio.stem}*.json")) or ([out_file] if out_file.exists() else [])
    if not produced:
        raise VideoTranslationHouseError("faster-whisper produced no JSON output")
    return json.loads(produced[-1].read_text(encoding="utf-8"))


def transcribe(
    audio_path: Path | str,
    out_dir: Path | str,
    *,
    provider: str | None = None,
    model: str | None = None,
    language: str | None = None,
    word_timestamps: bool = True,
    timeout: int = 3600,
    condition_on_previous_text: bool = True,
    hallucination_silence_threshold: float | None = None,
    temperature: float | None = None,
) -> ASRResult:
    """Transcribe ``audio_path`` with the resolved provider, writing engine JSON to
    ``out_dir``. Raises ``EngineUnavailableError`` if no suitable engine is installed.

    ``condition_on_previous_text`` / ``hallucination_silence_threshold`` / ``temperature`` are
    decode-control levers passed through to the engine CLI. Disabling
    ``condition_on_previous_text`` is the primary defense against repetition-hallucination loops
    (the decoder parroting its own prior output) on hard audio; the transcript retry ladder in
    ``transcript.run_transcription`` varies these across attempts."""
    audio = Path(audio_path)
    if not audio.is_file():
        raise FileNotFoundError(audio)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    decode = {
        "condition_on_previous_text": condition_on_previous_text,
        "hallucination_silence_threshold": hallucination_silence_threshold,
        "temperature": temperature,
    }
    resolved = _resolve_provider(provider)
    if resolved == "mlx-whisper":
        raw = _transcribe_mlx(audio, out, model=model, language=language,
                              word_timestamps=word_timestamps, timeout=timeout, **decode)
    else:  # faster-whisper
        raw = _transcribe_faster(audio, out, model=model, language=language,
                                 word_timestamps=word_timestamps, timeout=timeout, **decode)

    cues, has_words = _normalize_whisper_json(raw)
    return ASRResult(
        provider=resolved,
        model=model,
        language=raw.get("language") or language,
        cues=cues,
        has_word_timing=has_words,
        duration_seconds=raw.get("duration"),
        word_alignment="native-whisper" if has_words else None,
    )
