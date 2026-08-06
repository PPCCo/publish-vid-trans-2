"""The single sanctioned network boundary for publish-vid-trans.

Every process that reaches the network — `yt-dlp` for ingest, any vendor ASR/MT/TTS
API — is invoked from this package and nowhere else, so "what can leave the machine
and why" is auditable in one place. All egress is disabled unless VIDTRANS_FETCH_ENABLED
is truthy, mirroring publish-book's RESEARCH_FETCH_ENABLED posture.
"""
from __future__ import annotations

from .fetch import (
    FetchDisabled,
    check_publish_url,
    fetch_enabled,
    publish_enabled,
    require_fetch_enabled,
    require_publish_enabled,
    ytdlp_download,
    ytdlp_playlist_entries,
    ytdlp_probe,
)
from .publish import (
    discord_post,
    post_promotion,
    telegram_post,
    x_post,
    youtube_upload,
)

__all__ = [
    "FetchDisabled",
    "check_publish_url",
    "fetch_enabled",
    "publish_enabled",
    "require_fetch_enabled",
    "require_publish_enabled",
    "ytdlp_download",
    "ytdlp_playlist_entries",
    "ytdlp_probe",
    "youtube_upload",
    "x_post",
    "telegram_post",
    "discord_post",
    "post_promotion",
]
