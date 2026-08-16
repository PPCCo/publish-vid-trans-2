"""Media download + playlist enumeration — thin wrappers over ``net.fetch``.

Two operations, mirroring the framework's carve-out (CLAUDE.md rule 3):

  * ``download_video(url, dest_dir)`` — pulls best video+audio + metadata via
    ``net.fetch.ytdlp_download``. Media download is FLAG-GATED: ``ytdlp_download`` calls
    ``require_fetch_enabled()`` internally, and ``lib.env.bootstrap()`` sets
    ``VIDTRANS_FETCH_ENABLED=1`` by default, so this works out of the box for the local
    operator — but honors an operator who explicitly unset the flag.
  * ``playlist_entries(url)`` — FLAG-FREE metadata enumeration via
    ``net.fetch.ytdlp_playlist_entries`` (``--flat-playlist --skip-download``); lists a
    playlist's video ids/urls/titles without pulling any media.

Both are host-allowlisted inside ``net.fetch``. All framework imports are lazy.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def fetch_enabled() -> bool:
    """True iff media downloads are permitted (``VIDTRANS_FETCH_ENABLED`` is truthy).

    ``lib.env.bootstrap()`` defaults the flag on, but it deliberately won't override an operator
    who has explicitly exported ``VIDTRANS_FETCH_ENABLED=0`` (e.g. a sourced framework
    ``.env.local``). This lets the batch runner preflight the flag and fail with an actionable
    message instead of a bare ``FetchDisabled`` buried in a per-video error.
    """
    from video_translation_house.net.fetch import fetch_enabled as _fe  # lazy

    return _fe()


def normalize_url(url_or_id: str) -> str:
    """Canonicalize a bare id or messy URL to a watch URL (framework rule)."""
    from video_translation_house.net.fetch import normalize_youtube_url  # lazy

    return normalize_youtube_url(url_or_id)


def is_playlist(url_or_id: str) -> bool:
    from video_translation_house.net.fetch import is_playlist_url  # lazy

    return is_playlist_url(url_or_id)


def download_video(
    url: str,
    dest_dir: Path | str,
    *,
    write_subs: bool = True,
    sub_langs: str = "all",
    timeout: int = 3600,
) -> dict[str, Any]:
    """Download one video's media + metadata into ``dest_dir``.

    Returns ``net.fetch.ytdlp_download``'s dict: ``{info, video_path, info_json_path,
    stdout_tail}``. Raises if ``VIDTRANS_FETCH_ENABLED`` is unset (require_fetch_enabled)
    or the download fails.
    """
    from video_translation_house.net.fetch import ytdlp_download  # lazy

    dest = Path(dest_dir).expanduser().resolve()
    return ytdlp_download(
        url, dest, write_subs=write_subs, sub_langs=sub_langs, timeout=timeout,
    )


def playlist_entries(url: str, *, timeout: int = 300) -> list[dict[str, Any]]:
    """Enumerate a playlist's entries (metadata only, no media, no flag needed).

    Returns a list of ``{id, url, title, channel, duration_seconds, playlist_id,
    playlist_title}`` dicts.
    """
    from video_translation_house.net.fetch import ytdlp_playlist_entries  # lazy

    return ytdlp_playlist_entries(url, timeout=timeout)
