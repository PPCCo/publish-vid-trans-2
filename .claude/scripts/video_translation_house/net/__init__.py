"""The single sanctioned network boundary for publish-vid-trans.

Every process that reaches the network — `yt-dlp` for ingest, any vendor ASR/MT/TTS
API — is invoked from this package and nowhere else, so "what can leave the machine
and why" is auditable in one place. All egress is disabled unless VIDTRANS_FETCH_ENABLED
is truthy, mirroring publish-book's RESEARCH_FETCH_ENABLED posture.
"""
from __future__ import annotations

from .fetch import (
    FetchDisabled,
    fetch_enabled,
    require_fetch_enabled,
    ytdlp_download,
    ytdlp_probe,
)

__all__ = [
    "FetchDisabled",
    "fetch_enabled",
    "require_fetch_enabled",
    "ytdlp_download",
    "ytdlp_probe",
]
