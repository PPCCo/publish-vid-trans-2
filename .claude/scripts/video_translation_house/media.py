"""FFmpeg/ffprobe wrappers. Pure subprocess, no ML, no network.

These mirror how publish-book shells out to ffmpeg: the deterministic CLI orchestrates
external binaries rather than importing heavy libraries. Every function is safe to run
offline and never reaches the network.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .util import executable

# ASR baseline expects mono 16 kHz PCM WAV (Whisper-family standard input).
ASR_SAMPLE_RATE = 16000
ASR_CHANNELS = 1

# Dub baseline: mono 24 kHz PCM WAV (common TTS output rate; upmixed at mux time).
DUB_SAMPLE_RATE = 24000
DUB_CHANNELS = 1


def _require(binary: str) -> str:
    path = executable(binary)
    if not path:
        raise ConfigurationError(f"{binary} is not installed; see `vid_cli.py doctor`")
    return path


def ffprobe_streams(media_path: Path | str, *, timeout: int = 120) -> dict[str, Any]:
    """Return the parsed `ffprobe -show_format -show_streams` JSON for a media file."""
    proc = subprocess.run(  # noqa: S603 - fixed binary, path arg, no shell
        [
            _require("ffprobe"), "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(media_path),
        ],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise ConfigurationError(f"ffprobe failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout)


def probe_summary(media_path: Path | str) -> dict[str, Any]:
    """Condense ffprobe output into the fields source metadata cares about."""
    data = ffprobe_streams(media_path)
    fmt = data.get("format", {})
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})

    def _num(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    fps = None
    rate = video.get("r_frame_rate")
    if rate and "/" in str(rate):
        num, den = str(rate).split("/", 1)
        if _num(den):
            fps = round(_num(num) / _num(den), 3)

    duration = _num(fmt.get("duration"))
    return {
        "duration_seconds": duration,
        "video": {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": fps,
        } if video else {},
        "audio": {
            "codec": audio.get("codec_name"),
            "sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
            "channels": audio.get("channels"),
        } if audio else {},
    }


def extract_wav(
    source: Path | str,
    dest_wav: Path | str,
    *,
    sample_rate: int = ASR_SAMPLE_RATE,
    channels: int = ASR_CHANNELS,
    timeout: int = 3600,
) -> Path:
    """Extract a normalized mono 16 kHz PCM WAV suitable for ASR (`-ac 1 -ar 16000`)."""
    dest = Path(dest_wav)
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(  # noqa: S603 - fixed binary, path args, no shell
        [
            _require("ffmpeg"), "-y", "-i", str(source),
            "-vn", "-ac", str(channels), "-ar", str(sample_rate),
            "-c:a", "pcm_s16le", str(dest),
        ],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise ConfigurationError(
            f"ffmpeg WAV extraction failed ({proc.returncode}): {proc.stderr.strip()[:400]}"
        )
    if not dest.exists():
        raise ConfigurationError(f"ffmpeg reported success but no WAV at {dest}")
    return dest


def slice_wav(
    source: Path | str,
    dest_wav: Path | str,
    *,
    start_seconds: float = 0.0,
    duration_seconds: float = 60.0,
    sample_rate: int = ASR_SAMPLE_RATE,
    channels: int = ASR_CHANNELS,
    timeout: int = 300,
) -> Path:
    """Extract a short WAV window (used by cheap language-ID on ~60s of audio)."""
    dest = Path(dest_wav)
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(  # noqa: S603 - fixed binary, path args, no shell
        [
            _require("ffmpeg"), "-y", "-ss", str(start_seconds), "-t", str(duration_seconds),
            "-i", str(source), "-vn", "-ac", str(channels), "-ar", str(sample_rate),
            "-c:a", "pcm_s16le", str(dest),
        ],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise ConfigurationError(f"ffmpeg slice failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    return dest


# --- dub audio ops (Phase 4) -------------------------------------------------
# All pure ffmpeg subprocess, offline. Time-stretch prefers the rubberband CLI when
# present (declared optional in tools.default.json) for higher-quality tempo change;
# otherwise it falls back to ffmpeg's `atempo` filter. Callers enforce the stretch cap
# from company.default.json — these helpers just execute a requested factor.

def audio_duration_ms(media_path: Path | str) -> int:
    """Duration of an audio file in whole milliseconds (0 if unknown)."""
    summary = probe_summary(media_path)
    seconds = summary.get("duration_seconds")
    if seconds is None:
        return 0
    return max(0, round(float(seconds) * 1000))


def silent_wav(
    dest_wav: Path | str,
    *,
    duration_ms: int,
    sample_rate: int = DUB_SAMPLE_RATE,
    channels: int = DUB_CHANNELS,
    timeout: int = 120,
) -> Path:
    """Write a WAV of pure silence (used to pad gaps between cues)."""
    dest = Path(dest_wav)
    dest.parent.mkdir(parents=True, exist_ok=True)
    layout = "mono" if channels == 1 else "stereo"
    proc = subprocess.run(  # noqa: S603 - fixed binary, path arg, no shell
        [
            _require("ffmpeg"), "-y", "-f", "lavfi",
            "-i", f"anullsrc=r={sample_rate}:cl={layout}",
            "-t", f"{max(0, duration_ms) / 1000:.3f}",
            "-ac", str(channels), "-ar", str(sample_rate),
            "-c:a", "pcm_s16le", str(dest),
        ],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise ConfigurationError(f"ffmpeg silence failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    return dest


def _atempo_chain(factor: float) -> str:
    """ffmpeg's atempo accepts 0.5..2.0 per instance; chain to reach any factor."""
    factor = max(0.25, min(factor, 100.0))
    parts: list[float] = []
    remaining = factor
    while remaining > 2.0:
        parts.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        parts.append(0.5)
        remaining /= 0.5
    parts.append(remaining)
    return ",".join(f"atempo={p:.6f}" for p in parts)


def time_stretch(
    source: Path | str,
    dest_wav: Path | str,
    *,
    factor: float,
    sample_rate: int = DUB_SAMPLE_RATE,
    channels: int = DUB_CHANNELS,
    timeout: int = 600,
) -> Path:
    """Scale playback tempo by ``factor`` (>1 = faster/shorter) without shifting pitch.

    ``factor`` is synthesized_duration / target_duration: a clip longer than its slot
    needs factor>1 to compress into the slot. Prefers the ``rubberband`` CLI; falls back
    to chained ffmpeg ``atempo``.
    """
    dest = Path(dest_wav)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if abs(factor - 1.0) < 1e-6:
        # No stretch requested — just normalize format so downstream concat is uniform.
        return _reencode(source, dest, sample_rate=sample_rate, channels=channels, timeout=timeout)

    if executable("rubberband"):
        # rubberband --tempo <factor> in.wav out.wav
        proc = subprocess.run(  # noqa: S603 - fixed binary, path args, no shell
            [_require("rubberband"), "--tempo", f"{factor:.6f}", str(source), str(dest)],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if proc.returncode == 0 and dest.exists():
            return _reencode(dest, dest, sample_rate=sample_rate, channels=channels, timeout=timeout)
        # fall through to ffmpeg on any rubberband failure

    proc = subprocess.run(  # noqa: S603 - fixed binary, path args, no shell
        [
            _require("ffmpeg"), "-y", "-i", str(source),
            "-filter:a", _atempo_chain(factor),
            "-ac", str(channels), "-ar", str(sample_rate),
            "-c:a", "pcm_s16le", str(dest),
        ],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise ConfigurationError(f"ffmpeg atempo failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    return dest


def _reencode(
    source: Path | str, dest_wav: Path | str, *,
    sample_rate: int, channels: int, timeout: int,
) -> Path:
    """Normalize a WAV to the dub sample rate/channel layout (no tempo change)."""
    dest = Path(dest_wav)
    tmp = dest.with_suffix(".reenc.wav") if Path(source) == dest else dest
    proc = subprocess.run(  # noqa: S603 - fixed binary, path args, no shell
        [
            _require("ffmpeg"), "-y", "-i", str(source),
            "-ac", str(channels), "-ar", str(sample_rate),
            "-c:a", "pcm_s16le", str(tmp),
        ],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise ConfigurationError(f"ffmpeg re-encode failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    if tmp != dest:
        tmp.replace(dest)
    return dest


def concat_wavs(
    parts: list[Path | str],
    dest_wav: Path | str,
    *,
    sample_rate: int = DUB_SAMPLE_RATE,
    channels: int = DUB_CHANNELS,
    timeout: int = 1800,
) -> Path:
    """Concatenate WAV parts (silence + cue clips, in timeline order) into one track."""
    dest = Path(dest_wav)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not parts:
        raise ConfigurationError("concat_wavs: no parts to concatenate")
    command = [_require("ffmpeg"), "-y"]
    for part in parts:
        command += ["-i", str(part)]
    n = len(parts)
    filter_complex = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"
    command += [
        "-filter_complex", filter_complex,
        "-map", "[out]", "-ac", str(channels), "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", str(dest),
    ]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise ConfigurationError(f"ffmpeg concat failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    return dest


def loudnorm(
    source: Path | str,
    dest_wav: Path | str,
    *,
    target_lufs: float = -16.0,
    sample_rate: int = DUB_SAMPLE_RATE,
    channels: int = DUB_CHANNELS,
    timeout: int = 900,
) -> Path:
    """Single-pass EBU R128 loudness normalization to ``target_lufs`` (I)."""
    dest = Path(dest_wav)
    tmp = dest.with_suffix(".norm.wav") if Path(source) == dest else dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(  # noqa: S603 - fixed binary, path args, no shell
        [
            _require("ffmpeg"), "-y", "-i", str(source),
            "-filter:a", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11",
            "-ac", str(channels), "-ar", str(sample_rate),
            "-c:a", "pcm_s16le", str(tmp),
        ],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise ConfigurationError(f"ffmpeg loudnorm failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    if tmp != dest:
        tmp.replace(dest)
    return dest


# --- video mux (Phase 5) -----------------------------------------------------

def mux_video(
    video_source: Path | str,
    audio_wav: Path | str,
    dest_mp4: Path | str,
    *,
    subs: Path | str | None = None,
    audio_bitrate: str = "192k",
    timeout: int = 3600,
) -> Path:
    """Mux a source video's picture with a dub track into one MP4 (offline, picture copied).

    * ``-map 0:v:0 -c:v copy`` keeps the source video stream bit-for-bit (no re-encode).
    * The dub WAV is encoded to AAC (``-c:a aac``) and mapped as the sole audio track.
    * ``-shortest`` trims to the shorter of picture/dub so a slightly-off dub cannot leave a
      trailing black/silent tail.
    * When ``subs`` (an SRT/VTT path) is given, it is added as a soft ``mov_text`` subtitle
      track (toggleable, not burned in).
    """
    dest = Path(dest_mp4)
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _require("ffmpeg"), "-y",
        "-i", str(video_source),
        "-i", str(audio_wav),
    ]
    maps = ["-map", "0:v:0", "-map", "1:a:0"]
    codecs = ["-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate]
    if subs is not None:
        command += ["-i", str(subs)]
        maps += ["-map", "2:s:0"]
        codecs += ["-c:s", "mov_text"]
    command += maps + codecs + ["-shortest", str(dest)]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise ConfigurationError(f"ffmpeg mux failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    if not dest.exists():
        raise ConfigurationError(f"ffmpeg reported success but no MP4 at {dest}")
    return dest
