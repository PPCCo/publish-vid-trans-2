"""Split an audio/video file's channels — a NEW leaf verb (no framework equivalent).

Pure ffmpeg subprocess, offline. Three layouts:

  * ``mono``   — downmix every channel into one mono file (``-ac 1``).
  * ``stereo`` — force a 2-channel stereo file (``-ac 2``); a mono source is duplicated
    to L+R, a >2-channel source is downmixed.
  * ``all``    — de-interleave EACH source channel into its own mono file via the
    ``channelsplit`` filter, named ``<stem>.ch<N>.wav`` (0-indexed by ffmpeg's channel
    order — FL, FR, FC, LFE, …). This is the "split channels" operation the plan's R1
    calls for (e.g. pull a lav-mic channel out of a multi-track recording).

Output is always PCM WAV (``pcm_s16le``); the operator picks a target sample rate. The
source is never modified. All framework imports are lazy (env.bootstrap() must have wired
sys.path first).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

# Channel layouts we know how to split into distinct mono streams. ffmpeg's channelsplit
# needs an explicit input layout; we probe it from the source and map to these names.
_LAYOUT_BY_COUNT = {1: "mono", 2: "stereo", 3: "3.0", 4: "quad", 6: "5.1", 8: "7.1"}

_LAYOUTS = ("mono", "stereo", "all")


def _ffmpeg() -> str:
    """Resolve the ffmpeg binary the same way the framework's media layer does."""
    from video_translation_house.media import _require  # lazy

    return _require("ffmpeg")


def _probe_channels(source: Path) -> int:
    """Channel count of the source's first audio stream (0 if none)."""
    from video_translation_house import media as media_mod  # lazy

    summary = media_mod.probe_summary(source)
    audio = summary.get("audio") or {}
    try:
        return int(audio.get("channels") or 0)
    except (TypeError, ValueError):
        return 0


def _run(cmd: list[str], *, timeout: int) -> None:
    proc = subprocess.run(  # noqa: S603 - fixed binary, path args, no shell
        cmd, capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
        raise RuntimeError(f"ffmpeg channel-split failed ({proc.returncode}): {tail}")


def split_channels(
    source: Path | str,
    *,
    out_dir: Path | str | None = None,
    layout: str = "all",
    sample_rate: int = 48000,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Split ``source``'s audio channels per ``layout``; return the produced WAV path(s).

    ``out_dir`` defaults to the source's own directory. Returns
    ``{"layout", "channels_in", "outputs": [<abs paths>]}``. ``all`` on a mono source is a
    no-op split (one file), which is the honest result rather than an error.
    """
    src = Path(source).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"source not found: {src}")
    layout = str(layout).strip().lower()
    if layout not in _LAYOUTS:
        raise ValueError(f"layout must be one of {list(_LAYOUTS)}, got {layout!r}")

    dest_dir = Path(out_dir).expanduser().resolve() if out_dir else src.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = src.stem
    ff = _ffmpeg()

    if layout in ("mono", "stereo"):
        ac = 1 if layout == "mono" else 2
        out = dest_dir / f"{stem}.{layout}.wav"
        _run(
            [ff, "-y", "-i", str(src), "-vn", "-ac", str(ac), "-ar", str(sample_rate),
             "-c:a", "pcm_s16le", str(out)],
            timeout=timeout,
        )
        return {"layout": layout, "channels_in": _probe_channels(src),
                "outputs": [str(out)]}

    # layout == "all": one mono WAV per source channel via channelsplit.
    n = _probe_channels(src)
    if n <= 1:
        # Nothing to split — emit the single channel as a mono file (honest no-op).
        out = dest_dir / f"{stem}.ch0.wav"
        _run(
            [ff, "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sample_rate),
             "-c:a", "pcm_s16le", str(out)],
            timeout=timeout,
        )
        return {"layout": "all", "channels_in": n, "outputs": [str(out)]}

    ff_layout = _LAYOUT_BY_COUNT.get(n)
    if ff_layout is None:
        raise ValueError(
            f"cannot split a {n}-channel source (no known layout); "
            f"re-run with --layout mono/stereo to downmix instead."
        )
    # Build one output-per-channel: channelsplit fans the input into N labelled pads, each
    # mapped to its own -ar/pcm output. Channel labels are ffmpeg's canonical order for the
    # layout (c0, c1, …); we number the files by that order.
    labels = [f"[c{i}]" for i in range(n)]
    filtergraph = f"channelsplit=channel_layout={ff_layout}{''.join(labels)}"
    cmd = [ff, "-y", "-i", str(src), "-filter_complex", filtergraph]
    outputs: list[str] = []
    for i in range(n):
        out = dest_dir / f"{stem}.ch{i}.wav"
        cmd += ["-map", f"[c{i}]", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(out)]
        outputs.append(str(out))
    _run(cmd, timeout=timeout)
    return {"layout": "all", "channels_in": n, "outputs": outputs}
