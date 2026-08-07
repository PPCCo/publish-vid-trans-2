"""Standalone `speed` verb: uniformly re-time ANY video file (audio+video together).

    vid_cli.py speed 1.25 path/to/my-file.mp4
        -> path/to/my-file_1.25.mp4   (source untouched)

A DELIBERATE, constant playback-speed change — audio and video are scaled by the same
factor, so they stay in sync (this is NOT the per-cue rubber-banding rule 5 forbids). Works
on any video, including files this tool did not produce; the source is never modified.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import media
from .errors import VideoTranslationHouseError
from .util import load_json  # noqa: F401  (kept for symmetry / potential future use)

_VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}


def _factor_tag(factor: float) -> str:
    """Filename-safe tag for a speed factor: 1.25 -> '1.25', 2.0 -> '2', 1.5 -> '1.5'."""
    text = f"{factor:.4f}".rstrip("0").rstrip(".")
    return text or "1"


def _default_dest(source: Path, factor: float) -> Path:
    """Same folder as the source, with the factor appended: my-file.mp4 -> my-file_1.25.mp4."""
    return source.with_name(f"{source.stem}_{_factor_tag(factor)}{source.suffix}")


def respeed(
    root: Path,
    factor: float,
    source_path: str,
    *,
    dest: str | None = None,
) -> dict[str, Any]:
    """Speed up (or slow down) ``source_path`` by ``factor`` into a new file.

    ``source_path`` is taken as given — absolute, or relative to the current working directory.
    When ``dest`` is omitted the output lands beside the source as ``<stem>_<factor><suffix>``.
    Refuses to overwrite the source file.
    """
    try:
        factor = float(factor)
    except (TypeError, ValueError) as exc:
        raise VideoTranslationHouseError(f"speed factor must be a number, got {factor!r}") from exc
    if factor <= 0:
        raise VideoTranslationHouseError(f"speed factor must be positive, got {factor}")

    src = Path(source_path).expanduser()
    if not src.is_absolute():
        src = (Path.cwd() / src).resolve()
    else:
        src = src.resolve()
    if not src.is_file():
        raise VideoTranslationHouseError(f"source video not found: {source_path}")
    if src.suffix.lower() not in _VIDEO_SUFFIXES:
        raise VideoTranslationHouseError(
            f"{src.name} is not a recognized video ({sorted(_VIDEO_SUFFIXES)})"
        )

    if dest:
        out = Path(dest).expanduser()
        out = out if out.is_absolute() else (Path.cwd() / out).resolve()
    else:
        out = _default_dest(src, factor)
    if out.resolve() == src.resolve():
        raise VideoTranslationHouseError(
            "refusing to overwrite the source file; choose a different --out"
        )

    src_ms = media.audio_duration_ms(src)
    media.respeed_video(src, out, factor=factor)
    dest_ms = media.audio_duration_ms(out)
    return {
        "status": "ok",
        "source": str(src),
        "dest": str(out),
        "factor": factor,
        "src_duration_ms": src_ms,
        "dest_duration_ms": dest_ms,
        "source_untouched": True,
    }
