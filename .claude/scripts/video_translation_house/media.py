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


def _escape_subtitles_path(path: Path) -> str:
    # ffmpeg filter-graph args split on ':' and treat '\' as an escape char, so a plain
    # path (esp. on absolute POSIX/Windows paths) must be escaped before it goes inside
    # subtitles=filename='...'.
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def mux_video_burned_in(
    video_source: Path | str,
    audio_wav: Path | str,
    subs: Path | str,
    dest_mp4: Path | str,
    *,
    font_name: str | None = None,
    audio_bitrate: str = "192k",
    video_crf: int = 18,
    timeout: int = 3600,
) -> Path:
    """Mux picture + dub track and burn the subtitle track into the picture (re-encoded).

    Unlike ``mux_video``'s soft ``mov_text`` track, burned-in captions are rendered as
    pixels via the ``subtitles`` filter — required for platforms that don't reliably
    render soft subs, and the only option that survives screenshots/clips. This forces a
    video re-encode (``libx264``, CRF ``video_crf``) since ffmpeg cannot burn subtitles
    into a stream it is only copying. ``font_name`` selects the family libass renders with
    (e.g. "Noto Sans CJK" / "Noto Sans Arabic") so CJK/Cyrillic/Arabic scripts don't
    fall back to missing-glyph boxes; see ``tools.default.json``'s ``fonts`` map.
    """
    dest = Path(dest_mp4)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subs_arg = f"filename='{_escape_subtitles_path(Path(subs))}'"
    if font_name:
        subs_arg += f":force_style='FontName={font_name}'"
    command = [
        _require("ffmpeg"), "-y",
        "-i", str(video_source),
        "-i", str(audio_wav),
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", f"subtitles={subs_arg}",
        "-c:v", "libx264", "-crf", str(video_crf), "-preset", "medium",
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-shortest", str(dest),
    ]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise ConfigurationError(
            f"ffmpeg burned-in mux failed ({proc.returncode}): {proc.stderr.strip()[:400]}"
        )
    if not dest.exists():
        raise ConfigurationError(f"ffmpeg reported success but no MP4 at {dest}")
    return dest


# --- selection clip ops (segment resolution / assembly) ----------------------
# Cutting a source video at arbitrary timecodes and concatenating windows both require
# a re-encode by default: seeking to a non-keyframe start with stream-copy would either
# snap to the previous keyframe (imprecise) or emit a broken GOP, and concatenating clips
# with independent GOP structures via stream-copy fails unless every join lands on a
# keyframe. `libx264 -crf 18` matches the quality bar used by mux_video_burned_in.

def slice_video(
    source: Path | str,
    dest_mp4: Path | str,
    *,
    start_seconds: float,
    duration_seconds: float,
    reencode: bool = True,
    video_crf: int = 18,
    audio_bitrate: str = "192k",
    timeout: int = 3600,
) -> Path:
    """Cut one time window out of a source video into its own MP4.

    Placing ``-ss``/``-t`` AFTER ``-i`` makes the seek frame-accurate (input-side seeking
    is fast but snaps to keyframes). ``reencode=True`` (default) re-encodes to H.264/AAC so
    the cut is exact and the resulting clip has a self-contained GOP that ``concat_videos``
    can join. ``reencode=False`` stream-copies (only safe when the caller knows the cut
    points fall on keyframes — e.g. never for arbitrary human timecodes).
    """
    dest = Path(dest_mp4)
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _require("ffmpeg"), "-y",
        "-i", str(source),
        "-ss", f"{max(0.0, start_seconds):.3f}",
        "-t", f"{max(0.0, duration_seconds):.3f}",
        "-map", "0:v:0", "-map", "0:a:0?",
    ]
    if reencode:
        command += [
            "-c:v", "libx264", "-crf", str(video_crf), "-preset", "medium",
            "-c:a", "aac", "-b:a", audio_bitrate,
        ]
    else:
        command += ["-c", "copy"]
    command += ["-avoid_negative_ts", "make_zero", str(dest)]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise ConfigurationError(f"ffmpeg slice_video failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    if not dest.exists():
        raise ConfigurationError(f"ffmpeg reported success but no MP4 at {dest}")
    return dest


def concat_videos(
    parts: list[Path | str],
    dest_mp4: Path | str,
    *,
    video_crf: int = 18,
    audio_bitrate: str = "192k",
    timeout: int = 3600,
) -> Path:
    """Concatenate video clips (in the given order) into one MP4 via the concat filter.

    Mirrors ``concat_wavs``: uses the filter graph (not the concat demuxer) so clips with
    differing timebases/GOP structures join cleanly, re-encoding once to a uniform
    H.264/AAC output. Every part is expected to have both a video and an audio stream —
    ``slice_video``/mux outputs always do (audio is silence if the source had none isn't
    guaranteed, so callers building caption-only joins must ensure an audio stream exists).
    """
    dest = Path(dest_mp4)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not parts:
        raise ConfigurationError("concat_videos: no parts to concatenate")
    command = [_require("ffmpeg"), "-y"]
    for part in parts:
        command += ["-i", str(part)]
    n = len(parts)
    filter_complex = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
    command += [
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-crf", str(video_crf), "-preset", "medium",
        "-c:a", "aac", "-b:a", audio_bitrate,
        str(dest),
    ]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise ConfigurationError(f"ffmpeg concat_videos failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    if not dest.exists():
        raise ConfigurationError(f"ffmpeg reported success but no MP4 at {dest}")
    return dest
