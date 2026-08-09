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

from . import obs
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


def _stderr_tail(stderr: str, lines: int = 8) -> str:
    """The last ``lines`` of ffmpeg stderr — the actual error.

    ffmpeg prints its version/config banner FIRST, so ``stderr[:400]`` is all banner and hides
    the real failure at the tail. Every ffmpeg error in this module surfaces the tail instead.
    """
    return "\n".join((stderr or "").strip().splitlines()[-lines:])


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
        raise ConfigurationError(f"ffprobe failed ({proc.returncode}): {_stderr_tail(proc.stderr)}")
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
            "pix_fmt": video.get("pix_fmt"),
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
    obs.phase("extracting audio (ffmpeg)")
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
            f"ffmpeg WAV extraction failed ({proc.returncode}): {_stderr_tail(proc.stderr)}"
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
        raise ConfigurationError(f"ffmpeg slice failed ({proc.returncode}): {_stderr_tail(proc.stderr)}")
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
        raise ConfigurationError(f"ffmpeg silence failed ({proc.returncode}): {_stderr_tail(proc.stderr)}")
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
        raise ConfigurationError(f"ffmpeg atempo failed ({proc.returncode}): {_stderr_tail(proc.stderr)}")
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
        raise ConfigurationError(f"ffmpeg re-encode failed ({proc.returncode}): {_stderr_tail(proc.stderr)}")
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
    """Concatenate WAV parts (silence + cue clips, in timeline order) into one track.

    Uses ffmpeg's **concat demuxer** (a single ``-i playlist.txt`` input) rather than N separate
    ``-i`` inputs fed to an N-way ``filter_complex`` concat. The demuxer opens **one** input handle
    regardless of how many parts there are and builds no per-input filter graph, so it does not
    scale file-descriptor / process pressure with cue count. The old N-input form worked at small N
    but proved fragile on a long speech (hundreds of parts) under concurrent ffmpeg load — it could
    die early with only the version banner emitted (observed `rc 232`), which looked like a data
    bug but was resource contention. All parts here are already uniform ``pcm_s16le`` at the same
    ``sample_rate``/``channels`` (written by ``silent_wav``/``time_stretch``/``_reencode``), so the
    demuxer is exactly equivalent; we still set ``-ac``/``-ar``/``-c:a`` on the output as a guard.
    """
    dest = Path(dest_wav)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not parts:
        raise ConfigurationError("concat_wavs: no parts to concatenate")
    n = len(parts)
    # Concat-demuxer playlist: one `file '<abs-path>'` line per part, in order. Single quotes in a
    # path are escaped per the demuxer's rule ('\'' ). Written next to the destination so cleanup is
    # trivial; absolute paths mean the listfile location doesn't matter.
    listfile = dest.with_suffix(dest.suffix + ".concat.txt")
    lines = []
    for part in parts:
        p = str(Path(part).resolve()).replace("'", "'\\''")
        lines.append(f"file '{p}'")
    listfile.write_text("\n".join(lines) + "\n")
    obs.phase(f"concatenating audio ({n} parts, ffmpeg)")
    try:
        command = [
            _require("ffmpeg"), "-y",
            "-f", "concat", "-safe", "0", "-i", str(listfile),
            "-ac", str(channels), "-ar", str(sample_rate),
            "-c:a", "pcm_s16le", str(dest),
        ]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
        if proc.returncode != 0:
            raise ConfigurationError(
                f"ffmpeg concat failed ({proc.returncode}, {n} parts via concat demuxer): "
                f"{_stderr_tail(proc.stderr)}"
            )
    finally:
        listfile.unlink(missing_ok=True)
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
    obs.phase("loudness normalize (ffmpeg)")
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
        raise ConfigurationError(f"ffmpeg loudnorm failed ({proc.returncode}): {_stderr_tail(proc.stderr)}")
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
    obs.phase("muxing video (ffmpeg)")
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise ConfigurationError(f"ffmpeg mux failed ({proc.returncode}): {_stderr_tail(proc.stderr)}")
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
    obs.phase("muxing video, burning subtitles (ffmpeg re-encode)")
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise ConfigurationError(
            f"ffmpeg burned-in mux failed ({proc.returncode}): {_stderr_tail(proc.stderr)}"
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
        raise ConfigurationError(f"ffmpeg slice_video failed ({proc.returncode}): {_stderr_tail(proc.stderr)}")
    if not dest.exists():
        raise ConfigurationError(f"ffmpeg reported success but no MP4 at {dest}")
    return dest


def freeze_segment(
    source: Path | str,
    dest_mp4: Path | str,
    *,
    at_seconds: float,
    duration_ms: int,
    audio_sample_rate: int = 44100,
    audio_channels: int = 2,
    pix_fmt: str | None = "yuv420p",
    video_crf: int = 18,
    audio_bitrate: str = "192k",
    timeout: int = 600,
) -> Path:
    """Produce a clip that freezes ``source``'s frame at ``at_seconds`` for ``duration_ms``.

    Used by the freeze-frame dubbing path: when a dubbed cue's audio runs longer than its
    caption slot, the picture is paused on its last frame for the overflow instead of
    over-speeding the audio. A matching silent audio stream is generated (``anullsrc``) so the
    clip carries both a video AND an audio stream — ``concat_videos`` requires every part to
    have both. ``audio_sample_rate``/``audio_channels`` should match the adjacent source clips'
    audio (probe one via ``probe_summary``) so the concat never hits a mismatched-audio surprise.

    A single input frame is seized via a fast input-side ``-ss`` (which frame precisely does not
    matter for a freeze) and ``tpad=stop_mode=clone:stop_duration`` clones it for the requested
    duration. Re-encoded to H.264/AAC (like ``slice_video``) so the clip has a self-contained GOP
    ``concat_videos`` can join.
    """
    dest = Path(dest_mp4)
    dest.parent.mkdir(parents=True, exist_ok=True)
    seconds = max(0.0, duration_ms) / 1000
    layout = "mono" if audio_channels == 1 else "stereo"
    pad = f"tpad=stop_mode=clone:stop_duration={seconds:.3f}"
    vfilter = f"[0:v]trim=end_frame=1,setpts=PTS-STARTPTS,{pad}[v]"
    command = [
        _require("ffmpeg"), "-y",
        "-ss", f"{max(0.0, at_seconds):.3f}", "-i", str(source),
        "-f", "lavfi", "-i", f"anullsrc=r={audio_sample_rate}:cl={layout}",
        "-filter_complex", vfilter,
        "-map", "[v]", "-map", "1:a",
        "-t", f"{seconds:.3f}",
        "-c:v", "libx264", "-crf", str(video_crf), "-preset", "medium",
        "-pix_fmt", pix_fmt or "yuv420p",
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-ac", str(audio_channels), "-ar", str(audio_sample_rate),
        "-avoid_negative_ts", "make_zero", str(dest),
    ]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise ConfigurationError(
            f"ffmpeg freeze_segment failed ({proc.returncode}): {_stderr_tail(proc.stderr)}")
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
    obs.phase(f"rebuilding picture ({n} clips, ffmpeg re-encode)")
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise ConfigurationError(f"ffmpeg concat_videos failed ({proc.returncode}): {_stderr_tail(proc.stderr)}")
    if not dest.exists():
        raise ConfigurationError(f"ffmpeg reported success but no MP4 at {dest}")
    return dest


def normalize_wav(
    source: Path | str,
    dest_wav: Path | str,
    *,
    sample_rate: int = DUB_SAMPLE_RATE,
    channels: int = DUB_CHANNELS,
    timeout: int = 600,
) -> Path:
    """Re-encode a WAV to the dub sample rate/channel layout WITHOUT changing its tempo.

    The constant-audio-speed path (CLAUDE.md rule 5 / TASK 2): a dubbed cue's audio is
    NEVER time-stretched to fit its caption slot — it is emitted at its natural TTS length
    and the picture is re-timed around it. This is the public entry point for that pass; it
    just normalizes format so the back-to-back concat is uniform (identity in duration).
    """
    return _reencode(source, dest_wav, sample_rate=sample_rate, channels=channels, timeout=timeout)


# --- still-image picture (TASK 1) --------------------------------------------
# Some languages display one fixed image for the whole runtime instead of the source video,
# with the dub over it (different image per language; some keep the source video). A static
# frame has no motion, so no freeze/trim re-timing is needed — we simply loop the image for
# exactly the dub's length and mux the dub over it.

def still_image_video(
    image: Path | str,
    dest_mp4: Path | str,
    *,
    duration_ms: int,
    width: int | None = None,
    height: int | None = None,
    fps: int = 25,
    video_crf: int = 18,
    timeout: int = 1800,
) -> Path:
    """Build a silent H.264 MP4 that shows ``image`` frozen for ``duration_ms``.

    ``-loop 1 -i img -t <seconds>`` repeats the single still frame; the picture is scaled to
    fit inside ``width x height`` (preserving aspect) and padded to fill it with black, so the
    output has the exact even-dimension canvas H.264/yuv420p needs. When ``width``/``height``
    are omitted the image's own (even-snapped) dimensions are used. No audio stream is written
    — the caller muxes the dub over this picture with ``mux_video``.
    """
    dest = Path(dest_mp4)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img = Path(image)
    if not img.is_file():
        raise ConfigurationError(f"still image not found: {img}")
    seconds = max(0.001, duration_ms / 1000)
    vf_parts: list[str] = []
    if width and height:
        w, h = int(width), int(height)
        vf_parts.append(
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    # Guarantee even output dimensions even when no explicit canvas is given.
    vf_parts.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    vf = ",".join(vf_parts)
    command = [
        _require("ffmpeg"), "-y",
        "-loop", "1", "-i", str(img),
        "-t", f"{seconds:.3f}",
        "-r", str(fps),
        "-vf", vf,
        "-c:v", "libx264", "-crf", str(video_crf), "-preset", "medium",
        "-pix_fmt", "yuv420p",
        "-an", str(dest),
    ]
    obs.phase("rendering still-image picture (ffmpeg)")
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise ConfigurationError(
            f"ffmpeg still_image_video failed ({proc.returncode}): {_stderr_tail(proc.stderr)}")
    if not dest.exists():
        raise ConfigurationError(f"ffmpeg reported success but no MP4 at {dest}")
    return dest


# --- uniform whole-file re-time (TASK 3) -------------------------------------
# A DELIBERATE, CONSTANT playback-speed change applied to a whole file: audio AND video are
# scaled by the SAME factor so they stay in sync — this is NOT the per-cue rubber-banding that
# rule 5 forbids. Used both for the per-language default speed at mux and for the standalone
# `speed` verb (works on any video, even ones this tool didn't produce).

def respeed_video(
    source: Path | str,
    dest_mp4: Path | str,
    *,
    factor: float,
    video_crf: int = 18,
    audio_bitrate: str = "192k",
    timeout: int = 3600,
) -> Path:
    """Uniformly re-time a video by ``factor`` (>1 = faster/shorter) — audio+video together.

    Video timestamps are scaled with ``setpts=PTS/factor`` and audio tempo with a chained
    ``atempo`` (``_atempo_chain`` handles factors outside ffmpeg's 0.5–2.0 per-instance range),
    so a 1.25x request shortens BOTH streams to 80% length while keeping them mutually aligned.
    Re-encodes to H.264/AAC. The source file is never modified — output is a new file.
    """
    if factor <= 0:
        raise ConfigurationError(f"speed factor must be positive, got {factor}")
    src = Path(source)
    if not src.is_file():
        raise ConfigurationError(f"source video not found: {src}")
    dest = Path(dest_mp4)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if abs(factor - 1.0) < 1e-6:
        # No-op speed: still produce a distinct output file (re-encode) so callers get a file.
        command = [
            _require("ffmpeg"), "-y", "-i", str(src),
            "-c:v", "libx264", "-crf", str(video_crf), "-preset", "medium",
            "-c:a", "aac", "-b:a", audio_bitrate, str(dest),
        ]
    else:
        command = [
            _require("ffmpeg"), "-y", "-i", str(src),
            "-filter:v", f"setpts=PTS/{factor:.6f}",
            "-filter:a", _atempo_chain(factor),
            "-c:v", "libx264", "-crf", str(video_crf), "-preset", "medium",
            "-c:a", "aac", "-b:a", audio_bitrate, str(dest),
        ]
    obs.phase(f"re-timing video {factor:.3f}x (ffmpeg re-encode)")
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise ConfigurationError(f"ffmpeg respeed_video failed ({proc.returncode}): {_stderr_tail(proc.stderr)}")
    if not dest.exists():
        raise ConfigurationError(f"ffmpeg reported success but no MP4 at {dest}")
    return dest
