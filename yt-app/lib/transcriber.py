"""Transcribe a media file into a source-language caption doc.

Pipeline: ``media.extract_wav`` (16 kHz mono PCM) → ``engines.asr.transcribe`` → assemble a
caption doc → ``captions.split_long_cues`` (rule 12: any cue over ``--max-cue`` ms is split
into proportional ≤-cap sub-cues, renumbered 0..M).

The produced doc is the canonical source-language captions: ``source_text == target_text ==
<asr cue text>`` (verbatim source; nothing is translated here). Downstream, ``translator``
fills ``target_text`` for the real translation targets and leaves this verbatim for the source.

Caption doc shape (matches the framework contract):
    {"language": <src>, "cues": [{"id", "start_ms", "end_ms", "source_text",
                                  "target_text", "flags"?}], ...}

All framework imports are lazy (env.bootstrap() wires sys.path first).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class LanguageMismatchError(RuntimeError):
    """ASR detected a different spoken language than the declared source language.

    There is no pre-download language auto-detect (rule: ASR auto-detect is unavailable), so a
    wrong ``sourceLang`` in the config would silently ASR the audio as the wrong language. After
    the transcribe, whisper reports the language it actually decoded; when that disagrees with the
    declared source we STOP here (unless ``force``) so the operator can fix ``--source-lang`` or
    explicitly accept the declared value with ``--force``.
    """

    def __init__(self, *, declared: str, detected: str) -> None:
        self.declared = declared
        self.detected = detected
        super().__init__(
            f"ASR detected spoken language '{detected}' but sourceLang is declared '{declared}'. "
            f"Re-run with --source-lang {detected} (fix the declaration), or --force to keep "
            f"'{declared}' as-is."
        )


# Whisper reports some languages under more than one tag; treat these as the same language so
# a correct declaration isn't falsely blocked by a cosmetic tag difference.
_LANGUAGE_ALIASES: dict[str, str] = {"zh-cn": "zh", "zh-tw": "zh", "iw": "he", "in": "id"}


def _canon_lang(code: str) -> str:
    code = (code or "").strip().lower()
    return _LANGUAGE_ALIASES.get(code, code)


def _same_language(a: str, b: str) -> bool:
    return _canon_lang(a) == _canon_lang(b)


def default_max_cue_ms(root: Path) -> int:
    """Company caption cap (``quality_bars.captions.max_cue_duration_ms``, default 7000)."""
    from video_translation_house.captions import max_cue_ms  # lazy

    return int(max_cue_ms(root))


def extract_audio(
    source: Path | str, dest_wav: Path | str, *, timeout: int = 3600
) -> Path:
    """Extract the ASR-format mono 16 kHz WAV from a media file."""
    from video_translation_house import media as media_mod  # lazy

    return media_mod.extract_wav(
        source, dest_wav,
        sample_rate=media_mod.ASR_SAMPLE_RATE, channels=media_mod.ASR_CHANNELS,
        timeout=timeout,
    )


def _cue_to_caption(cue: Any) -> dict[str, Any]:
    """Map an ASR ``TranscriptCue`` (or its dict) to a caption-doc cue (verbatim source)."""
    d = cue.to_dict() if hasattr(cue, "to_dict") else dict(cue)
    text = d.get("text", "") or ""
    out: dict[str, Any] = {
        "id": int(d["id"]),
        "start_ms": int(d["start_ms"]),
        "end_ms": int(d["end_ms"]),
        "source_text": text,
        "target_text": text,  # verbatim for the source language
    }
    # Carry ASR confidence signals forward for anyone inspecting the doc; harmless downstream.
    if d.get("confidence") is not None:
        out["confidence"] = d["confidence"]
    if d.get("no_speech_prob") is not None:
        out["no_speech_prob"] = d["no_speech_prob"]
    return out


def transcribe(
    audio_path: Path | str,
    out_dir: Path | str,
    *,
    language: str,
    root: Path,
    provider: str | None = None,
    model: str | None = None,
    max_cue_ms: int | None = None,
    condition_on_previous_text: bool = True,
    hallucination_silence_threshold: float | None = None,
    force: bool = False,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Transcribe ``audio_path`` into a source-language caption doc.

    ``max_cue_ms`` overrides the company cap for the long-cue auto-split (rule 12); when
    omitted the company default is used. ``condition_on_previous_text`` /
    ``hallucination_silence_threshold`` are the anti-hallucination decode levers (rule 10):
    pass ``condition_on_previous_text=False`` to break repetition loops.

    ``force=False`` (default) raises ``LanguageMismatchError`` when whisper's detected language
    disagrees with the requested ``language`` — the post-transcribe sanity check. ``force=True``
    keeps the declared language regardless (a note is left in the returned doc).

    Returns the caption doc (also useful straight-through) — the caller persists it.
    """
    from video_translation_house.captions import split_long_cues  # lazy
    from video_translation_house.engines import asr as asr_mod  # lazy

    result = asr_mod.transcribe(
        audio_path, out_dir,
        provider=provider, model=model, language=language,
        word_timestamps=True, timeout=timeout,
        condition_on_previous_text=condition_on_previous_text,
        hallucination_silence_threshold=hallucination_silence_threshold,
    )
    detected = (result.language or language or "").strip().lower()
    declared = (language or "").strip().lower()
    mismatch = bool(detected) and bool(declared) and not _same_language(detected, declared)
    if mismatch and not force:
        raise LanguageMismatchError(declared=declared, detected=detected)

    cues = [_cue_to_caption(c) for c in result.cues]
    cap = int(max_cue_ms) if max_cue_ms else default_max_cue_ms(root)
    cues = split_long_cues(cues, max_ms=cap, language=language)
    doc: dict[str, Any] = {
        "language": language,
        "provider": result.provider,
        "model": result.model,
        "detected_language": detected or None,
        "source_language": result.language or language,
        "duration_seconds": result.duration_seconds,
        "cues": cues,
    }
    if mismatch and force:
        doc["language_mismatch_forced"] = {"declared": declared, "detected": detected}
    return doc
