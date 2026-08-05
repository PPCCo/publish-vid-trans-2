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


def _check_url(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ConfigurationError(
            f"URL host not in ingest allowlist: {host or url!r} "
            f"(allowed: {', '.join(sorted(ALLOWED_HOSTS))})"
        )


def _ytdlp() -> str:
    # util.executable resolves PATH first, then the venv bin dir next to sys.executable —
    # so a `pip install yt-dlp` into the project venv is found even when the venv isn't
    # activated (the CLI is run as `.venv/bin/python3 …`). Keeps this in lockstep with
    # what `doctor` reports as installed.
    from ..util import executable

    binary = executable("yt-dlp")
    if not binary:
        raise ConfigurationError("yt-dlp is not installed; see `vid_cli.py doctor`")
    return binary


def ytdlp_probe(url: str, *, timeout: int = 120) -> dict[str, Any]:
    """Fetch metadata only (no media download) via `yt-dlp --dump-single-json`."""
    require_fetch_enabled()
    _check_url(url)
    proc = subprocess.run(  # noqa: S603 - fixed binary, validated URL, no shell
        [_ytdlp(), "--dump-single-json", "--no-warnings", "--skip-download", url],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise ConfigurationError(f"yt-dlp probe failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout)


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
    _check_url(url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(dest_dir / "%(id)s.%(ext)s")
    cmd = [
        _ytdlp(),
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
