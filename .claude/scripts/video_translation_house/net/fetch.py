"""Sanctioned yt-dlp egress. No other module may shell out to yt-dlp or a vendor API.

Design mirrors publish-book/net/fetch.py:
  * A single env flag (VIDTRANS_FETCH_ENABLED) is the master switch; when unset, every
    function here raises FetchDisabled before any process is spawned.
  * yt-dlp is invoked as a subprocess (the CLI package never imports ML/network libs).
  * URLs are checked against an allowlist of hosts so an agent cannot redirect ingest
    at an arbitrary endpoint.

The pre_tool_policy hook independently blocks bare `yt-dlp` in Bash unless the flag is
set; this module is the Python-layer twin of that guard for code paths that call it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..errors import ConfigurationError, FetchDisabled, PublishDisabled

# Hosts ingest is permitted to reach. Extend deliberately; keep it short.
ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "vimeo.com", "www.vimeo.com",
}

# Hosts the sanctioned uploader (net/publish.py) is permitted to WRITE to. Kept separate
# from ALLOWED_HOSTS so read-ingest and publish-egress have independent allowlists.
PUBLISH_HOSTS = {
    "www.googleapis.com", "youtube.googleapis.com", "oauth2.googleapis.com",
    "api.twitter.com", "api.x.com", "upload.twitter.com",
    "api.telegram.org",
    "discord.com", "discordapp.com",
}

_TRUTHY = {"1", "true", "on", "yes"}


def fetch_enabled() -> bool:
    return os.environ.get("VIDTRANS_FETCH_ENABLED", "0").strip().lower() in _TRUTHY


def require_fetch_enabled() -> None:
    if not fetch_enabled():
        raise FetchDisabled(
            "network egress is disabled; set VIDTRANS_FETCH_ENABLED=1 to permit ingest"
        )


def publish_enabled() -> bool:
    """Master switch for external publication (uploads + promotional posts).

    Distinct from fetch_enabled(): ingest and publish are independently gated so a repo
    can allow reading source video without allowing it to write to any platform."""
    return os.environ.get("VIDTRANS_PUBLISH_ENABLED", "0").strip().lower() in _TRUTHY


def require_publish_enabled() -> None:
    if not publish_enabled():
        raise PublishDisabled(
            "external publication is disabled; set VIDTRANS_PUBLISH_ENABLED=1 to permit "
            "uploads/posts (the pre_tool_policy hook additionally requires "
            "VIDTRANS_EXTERNAL_WRITES=enabled for bash-level API verbs)"
        )


def check_publish_url(url: str) -> None:
    """Guard a publish endpoint against the write allowlist (defense in depth vs SSRF)."""
    host = (urlparse(url).hostname or "").lower()
    if host not in PUBLISH_HOSTS:
        raise ConfigurationError(
            f"publish URL host not in allowlist: {host or url!r} "
            f"(allowed: {', '.join(sorted(PUBLISH_HOSTS))})"
        )


# YouTube playlist ids carry a well-known prefix; video ids are exactly 11 URL-safe chars.
# A bare id has no hostname, so the allowlist check below would reject it — normalize it to a
# canonical youtube.com URL first so operators can paste either a full URL or a bare id.
_PLAYLIST_PREFIXES = ("PL", "UU", "FL", "OL", "RD", "LL", "TL", "WL", "SP")
_ID_SAFE = "-_"


def _looks_like_bare_id(s: str) -> bool:
    """True when s is a bare YouTube id (no scheme/host), not a URL or path."""
    if not s or "/" in s or ":" in s or "." in s or " " in s:
        return False
    return all(c.isalnum() or c in _ID_SAFE for c in s)


def normalize_youtube_url(url: str) -> str:
    """Expand a bare YouTube playlist/video id into a canonical youtube.com URL.

    Playlist ids (``PL…``/``UU…``/… prefixes, and always longer than an 11-char video id)
    become ``https://www.youtube.com/playlist?list=<id>``; anything else that looks like a
    bare id becomes ``https://www.youtube.com/watch?v=<id>``. A value that already parses to a
    host is returned unchanged. Kept narrow so it can't turn arbitrary text into a URL.
    """
    if not _looks_like_bare_id(url):
        return url
    if is_playlist_url(url):
        return f"https://www.youtube.com/playlist?list={url}"
    return f"https://www.youtube.com/watch?v={url}"


def is_playlist_url(url_or_id: str) -> bool:
    """True when the string looks like a YouTube playlist rather than a single video.

    A bare id is a playlist when it carries a playlist prefix and is longer than an 11-char
    video id; a full URL is a playlist when it carries a ``list=`` query param or a
    ``/playlist`` path. Used both by ``normalize_youtube_url`` (bare-id branch) and by the
    ``cmdgen`` command-printer to route between the flag-free ``add-playlist`` enumeration
    path and the single-video ingest path.
    """
    if _looks_like_bare_id(url_or_id):
        return url_or_id.startswith(_PLAYLIST_PREFIXES) and len(url_or_id) > 11
    return "list=" in url_or_id or "/playlist" in url_or_id


def _check_url(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ConfigurationError(
            f"URL host not in ingest allowlist: {host or url!r} "
            f"(allowed: {', '.join(sorted(ALLOWED_HOSTS))})"
        )


def _ytdlp() -> str:
    # PATH only — deliberately NOT util.executable's venv-bindir fallback. A `pip install
    # yt-dlp` into the project venv resolves its own isolated `certifi` bundle, which on a
    # TLS-inspecting proxy (Prisma Access) fails CERTIFICATE_VERIFY_FAILED even with
    # SSL_CERT_FILE set, while a system-installed yt-dlp (e.g. `brew install yt-dlp`) works.
    # Always use the PATH binary so behavior matches what an operator validated by hand.
    # Keep this in lockstep with what `doctor` reports as installed (shutil.which too).
    binary = shutil.which("yt-dlp")
    if not binary:
        raise ConfigurationError(
            "yt-dlp is not installed; install it on PATH, e.g. `brew install yt-dlp` "
            "(see `vid_cli.py doctor`)"
        )
    return binary


def ytdlp_probe(url: str, *, timeout: int = 120) -> dict[str, Any]:
    """Fetch metadata only (no media download) via `yt-dlp --dump-single-json`."""
    require_fetch_enabled()
    url = normalize_youtube_url(url)
    _check_url(url)
    proc = subprocess.run(  # noqa: S603 - fixed binary, validated URL, no shell
        [_ytdlp(), "--dump-single-json", "--no-warnings", "--skip-download", url],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise ConfigurationError(f"yt-dlp probe failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout)


def ytdlp_playlist_entries(url: str, *, timeout: int = 300) -> list[dict[str, Any]]:
    """Enumerate a playlist's videos — metadata only, no media download.

    Deliberate carve-out from ``require_fetch_enabled()``: enumerating a playlist reads
    only titles/ids/urls (no media bytes touch disk), so it runs even when
    ``VIDTRANS_FETCH_ENABLED`` is unset. The per-video *media* download (``ytdlp_download``)
    remains flag-gated, so this cannot become a back-door for pulling content down (rule 3).
    Still host-allowlisted (SSRF defense in depth).

    Uses ``--flat-playlist`` so yt-dlp lists entries without descending into each video.
    Returns one dict per entry: ``{id, url, title, channel, playlist_id, playlist_title}``.
    """
    url = normalize_youtube_url(url)
    _check_url(url)
    proc = subprocess.run(  # noqa: S603 - fixed binary, validated URL, no shell
        build_ytdlp_playlist_argv(url),
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise ConfigurationError(
            f"yt-dlp playlist enumeration failed ({proc.returncode}): {proc.stderr.strip()[:400]}"
        )
    data = json.loads(proc.stdout)
    playlist_id = data.get("id")
    playlist_title = data.get("title")
    entries = data.get("entries") or []
    out: list[dict[str, Any]] = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        out.append({
            "id": e.get("id"),
            "url": e.get("url") or e.get("webpage_url") or f"https://youtu.be/{e.get('id')}",
            "title": e.get("title"),
            "channel": e.get("channel") or e.get("uploader"),
            "duration_seconds": e.get("duration"),
            "playlist_id": playlist_id,
            "playlist_title": playlist_title,
        })
    return out


def build_ytdlp_download_argv(
    url: str,
    dest_dir: Path,
    *,
    write_subs: bool = True,
    sub_langs: str = "all",
    binary: str | None = None,
) -> list[str]:
    """Pure argv builder for the media-download command — no subprocess, no side effects.

    Both ``ytdlp_download`` (the real downloader) and the ``cmdgen`` command-printer call
    this, so the executed command and the printed command can never diverge. It does NOT
    normalize the url or check the host allowlist — callers that actually invoke it must
    still do both (``ytdlp_download`` does; the printer should pass an already-normalized
    url so what it displays is exactly what would run). ``binary`` lets a print-only caller
    pass the bare command name (``"yt-dlp"``) without requiring ``shutil.which`` to resolve
    locally; the default (``None``) resolves via ``_ytdlp()`` as the real downloader needs.
    """
    resolved = binary if binary is not None else _ytdlp()
    out_tmpl = str(dest_dir / "%(id)s.%(ext)s")
    cmd = [
        resolved,
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--write-info-json",
        "--no-warnings",
        "--no-playlist",
        "-o", out_tmpl,
    ]
    if write_subs:
        cmd += ["--write-auto-sub", "--write-sub", "--sub-langs", sub_langs, "--convert-subs", "srt"]
    cmd.append(url)
    return cmd


def build_ytdlp_playlist_argv(url: str, *, binary: str | None = None) -> list[str]:
    """Pure argv builder for the flag-free playlist-enumeration command (no side effects).

    Shared by ``ytdlp_playlist_entries`` (the real enumerator) and the ``cmdgen`` printer so
    they cannot drift. Same ``binary`` override semantics as ``build_ytdlp_download_argv``.
    """
    resolved = binary if binary is not None else _ytdlp()
    return [resolved, "--dump-single-json", "--flat-playlist", "--skip-download",
            "--no-warnings", url]


def ytdlp_download(
    url: str,
    dest_dir: Path,
    *,
    write_subs: bool = True,
    sub_langs: str = "all",
    timeout: int = 3600,
) -> dict[str, Any]:
    """Download best video+audio plus metadata (and optionally caption tracks).

    Returns a dict with the resolved output paths and the parsed info JSON. The caller
    is responsible for registering resulting files as artifacts.
    """
    require_fetch_enabled()
    url = normalize_youtube_url(url)
    _check_url(url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_ytdlp_download_argv(url, dest_dir, write_subs=write_subs, sub_langs=sub_langs)
    proc = subprocess.run(  # noqa: S603 - fixed binary, validated URL, no shell
        cmd, capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise ConfigurationError(f"yt-dlp download failed ({proc.returncode}): {proc.stderr.strip()[:400]}")

    info_files = sorted(dest_dir.glob("*.info.json"))
    info: dict[str, Any] = {}
    if info_files:
        info = json.loads(info_files[-1].read_text(encoding="utf-8"))
    video_id = info.get("id")
    video_path = None
    if video_id:
        for candidate in dest_dir.glob(f"{video_id}.*"):
            if candidate.suffix.lower() in {".mp4", ".mkv", ".webm"}:
                video_path = candidate
                break
    if video_path is None:
        media = [p for p in dest_dir.iterdir()
                 if p.suffix.lower() in {".mp4", ".mkv", ".webm"}]
        video_path = sorted(media)[-1] if media else None

    return {
        "info": info,
        "video_path": str(video_path) if video_path else None,
        "info_json_path": str(info_files[-1]) if info_files else None,
        "stdout_tail": proc.stdout.strip()[-400:],
    }
