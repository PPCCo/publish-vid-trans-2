"""Standalone `tts-sample` verb: A/B TTS engines per language on a source clip.

    vid_cli.py tts-sample <video-path-or-id> --languages en,zh,ar,ur,fa
        [--engines xtts,piper] [--start 0 --duration 70] [--samples-per-lang 3]
        [--text-source <file>] [--out-dir <dir>] [--clone-ref <wav>]

Purpose (see xtts-piper-alternatives.md): generate a few short samples per language so you can
compare (1) synthesis WALL-CLOCK time and (2) VOICE QUALITY across engines before committing a
default. It measures the persistent-XTTS-worker speedup (2nd..Nth cue reuse cached latents) against
a piper baseline, all on one clip.

This is PURE MECHANISM (rule 1): it never touches state.json / manifest.json / approvals / events
and never writes under a project except the sample WAVs + the scratch report under `--out-dir`
(default `<cwd>/tts-samples/`). The report is NOT a registered artifact. Modeled on `speed.py`.

The reference clip (`source/audio.wav[start:start+duration]`) doubles as the XTTS clone reference,
so — as you asked — a Quranic-verse window recited in the source voice is cloned into the sample,
matching how the real pipeline behaves.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

from . import dubbing as dubbing_mod
from . import media
from .engines import tts as tts_mod
from .errors import VideoTranslationHouseError
from .paths import ProjectPaths, is_valid_video_id

_VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}

# Curated, self-contained sample sentences (rule: no ASR dependency). Two neutral lines + one that
# names the language, per language — enough to hear timbre, prosody, and any obvious mispronunciation
# without needing the source transcript. Kept short so each cue synthesizes quickly.
_SAMPLE_SENTENCES: dict[str, list[str]] = {
    "en": [
        "In the name of God, the most gracious, the most merciful.",
        "Truly, mankind is in a state of loss, except those who believe and do good.",
        "This is an English voice sample for comparing text-to-speech engines.",
    ],
    "ar": [
        "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ.",
        "إِنَّ الْإِنسَانَ لَفِي خُسْرٍ.",
        "هَذِهِ عَيِّنَةٌ صَوْتِيَّةٌ بِاللُّغَةِ الْعَرَبِيَّةِ.",
    ],
    "ur": [
        "اللہ کے نام سے جو بڑا مہربان نہایت رحم والا ہے۔",
        "بے شک انسان خسارے میں ہے۔",
        "یہ اردو زبان کا ایک آوازی نمونہ ہے۔",
    ],
    "fa": [
        "به نام خداوند بخشندهٔ مهربان.",
        "به‌درستی که انسان در زیان است.",
        "این یک نمونهٔ صدایی به زبان فارسی است.",
    ],
    "zh": [
        "奉至仁至慈的真主之名。",
        "确实，人类都处于亏损之中。",
        "这是一段用于比较语音引擎的中文示例。",
    ],
    "es": [
        "En el nombre de Dios, el Clemente, el Misericordioso.",
        "En verdad, el ser humano está en pérdida.",
        "Esta es una muestra de voz en español para comparar motores de síntesis.",
    ],
    "ru": [
        "Во имя Аллаха, Милостивого, Милосердного.",
        "Воистину, человек в убытке.",
        "Это образец голоса на русском языке для сравнения движков.",
    ],
}


def _resolve_source(root: Path, video_path_or_id: str) -> Path:
    """Resolve the audio source: a file path (video/audio) OR a project/video id whose
    `source/audio.wav` we use. Returns a media path ffmpeg can slice."""
    candidate = Path(video_path_or_id).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate)
    if candidate.is_file() and candidate.suffix.lower() in (_VIDEO_SUFFIXES | _AUDIO_SUFFIXES):
        return candidate.resolve()

    # Not a file path -> treat as a project / catalog video id.
    if is_valid_video_id(video_path_or_id):
        src_wav = ProjectPaths(root, video_path_or_id).source_dir / "audio.wav"
        if src_wav.is_file():
            return src_wav
        raise VideoTranslationHouseError(
            f"project {video_path_or_id!r} has no source/audio.wav yet — ingest it first "
            f"(fetch-gated `ingest run`), or pass a local video/audio file path instead."
        )
    raise VideoTranslationHouseError(
        f"could not resolve {video_path_or_id!r} as a video/audio file or a valid video id"
    )


def _engines_for_language(root: Path, language: str, requested: list[str] | None,
                          have_clone_ref: bool) -> list[str]:
    """Which engines to sample for a language when the caller doesn't pin `--engines`.

    Uses the SAME routing the real pipeline uses (no piper here — the operator dislikes its
    quality): a `clone_languages` language samples `xtts` (needs a clone ref); every other
    language samples its `tts.per_language` engine (e.g. fa/ur -> `mms`). Falls back to `xtts`
    for a clone-ref language with no config, else whatever `per_language`/default resolves to.
    """
    if requested:
        return requested
    try:
        clone_langs = dubbing_mod.clone_languages(root)
    except Exception:  # noqa: BLE001
        clone_langs = []
    if language in clone_langs:
        return ["xtts"] if have_clone_ref else []
    engine = tts_mod._configured_provider(root, language)
    return [engine] if engine else []


def _load_texts(language: str, text_source: str | None, n: int) -> list[str]:
    if text_source:
        raw = Path(text_source).expanduser().read_text(encoding="utf-8")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if not lines:
            raise VideoTranslationHouseError(f"--text-source {text_source} has no non-empty lines")
        return lines[:n]
    sentences = _SAMPLE_SENTENCES.get(language)
    if not sentences:
        raise VideoTranslationHouseError(
            f"no curated sample sentences for language {language!r}; pass --text-source <file> "
            f"(known: {', '.join(sorted(_SAMPLE_SENTENCES))})"
        )
    return sentences[:n]


def run_samples(
    root: Path,
    video_path_or_id: str,
    *,
    languages: list[str],
    engines: list[str] | None = None,
    start_seconds: float = 0.0,
    duration_seconds: float = 70.0,
    samples_per_lang: int = 3,
    text_source: str | None = None,
    out_dir: str | None = None,
    clone_ref: str | None = None,
) -> dict[str, Any]:
    """Synthesize a few timed samples per (language, engine) and write a JSON report.

    Report shape:
      {lang: {engine: [{sample_id, text, wav_path, elapsed_ms, duration_ms, rtf, error?}]}}
    `rtf` is real-time factor = elapsed_ms / duration_ms (lower is faster than real time).
    """
    if not languages:
        raise VideoTranslationHouseError("--languages is required (comma-separated ISO codes)")
    samples_per_lang = max(1, int(samples_per_lang))

    source = _resolve_source(root, video_path_or_id)

    out = Path(out_dir).expanduser() if out_dir else (Path.cwd() / "tts-samples")
    out = out if out.is_absolute() else (Path.cwd() / out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    # Build the reference clip once (also the XTTS clone reference — a source-voice window incl. any
    # recited verse, matching the real pipeline). An explicit --clone-ref overrides.
    if clone_ref:
        ref = Path(clone_ref).expanduser()
        ref = ref if ref.is_absolute() else (Path.cwd() / ref).resolve()
        if not ref.is_file():
            raise VideoTranslationHouseError(f"--clone-ref not found: {clone_ref}")
    else:
        ref = out / "clone-ref.wav"
        media.slice_wav(
            source, ref,
            start_seconds=float(start_seconds), duration_seconds=float(duration_seconds),
            sample_rate=media.DUB_SAMPLE_RATE, channels=media.DUB_CHANNELS,
        )

    report: dict[str, Any] = {}
    for language in languages:
        texts = _load_texts(language, text_source, samples_per_lang)
        lang_engines = _engines_for_language(root, language, engines, have_clone_ref=ref.is_file())
        report[language] = {}
        for engine in lang_engines:
            rows: list[dict[str, Any]] = []
            # xtts is a clone engine; hand it the reference. piper/others ignore clone_ref.
            ref_for_engine = ref if engine == "xtts" else None
            for idx, text in enumerate(texts):
                wav = out / f"{language}.{engine}.sample{idx}.wav"
                row: dict[str, Any] = {"sample_id": idx, "text": text, "wav_path": str(wav)}
                # Engine-specific model resolution:
                #  - xtts: clone engine, no model here (staged XTTS-v2 default + clone ref).
                #  - mms:  derives facebook/mms-tts-<iso> from the language itself; no registry.
                #  - piper/kokoro/…: need a concrete voice .onnx from the company `dubbing.voices`
                #    registry (male default, then female), exactly as `run_dub` resolves it.
                model = None
                if engine not in ("xtts", "mms"):
                    picked = (dubbing_mod.resolve_dub_voice(root, language, "male")
                              or dubbing_mod.resolve_dub_voice(root, language, "female"))
                    if picked is None:
                        row.update({"elapsed_ms": None, "duration_ms": None, "rtf": None,
                                    "error": f"no {engine} voice staged for {language!r} in "
                                             f"company.dubbing.voices (male/female)"})
                        rows.append(row)
                        continue
                    model = picked["model"]
                t0 = time.perf_counter()
                try:
                    tts_mod.synthesize_cue(
                        text, wav, provider=engine, language=language, model=model,
                        clone_ref=ref_for_engine, root=root,
                    )
                    elapsed_ms = round((time.perf_counter() - t0) * 1000)
                    dur_ms = media.audio_duration_ms(wav)
                    row.update({
                        "elapsed_ms": elapsed_ms,
                        "duration_ms": dur_ms,
                        "rtf": round(elapsed_ms / dur_ms, 3) if dur_ms else None,
                    })
                except Exception as exc:  # noqa: BLE001 - a failed engine shouldn't abort the sweep
                    row.update({"elapsed_ms": None, "duration_ms": None, "rtf": None,
                                "error": f"{type(exc).__name__}: {exc}"})
                rows.append(row)
            report[language][engine] = rows

    report_path = out / "tts-sample-report.json"
    import json

    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "status": "ok",
        "source": str(source),
        "clone_ref": str(ref),
        "out_dir": str(out),
        "report_path": str(report_path),
        "languages": languages,
        "clip_start_seconds": float(start_seconds),
        "clip_duration_seconds": float(duration_seconds),
        "report": report,
    }
