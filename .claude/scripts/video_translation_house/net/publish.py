"""The single sanctioned external-WRITE boundary for publish-vid-trans.

Every upload or promotional post that leaves the machine originates here and nowhere else,
so "what we publish, where, and with which credentials" is auditable in one file. Posture,
mirroring net/fetch.py:

  * require_publish_enabled() runs FIRST in every live function — nothing touches a platform
    API unless VIDTRANS_PUBLISH_ENABLED is truthy (the pre_tool_policy hook independently
    requires VIDTRANS_EXTERNAL_WRITES=enabled for bash-level API verbs: defense in depth).
  * Every function accepts dry_run=True and, in that mode, performs NO network I/O and
    returns the exact request it WOULD send (request_preview). This is the path tests and
    the default skill guidance exercise.
  * SDKs (googleapiclient, tweepy) are imported LAZILY inside functions; a missing SDK
    raises PublishDisabled so absence degrades gracefully instead of crashing at import.
  * Credentials are read from environment variables named by a `credentials_ref` prefix
    (documented in .env.example); this module never reads a secrets file.

None of these functions know about lifecycle/state — distribution.py drives them and records
outcomes through the CLI. They return plain dicts; the caller persists the manifest.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError, PublishDisabled
from .fetch import check_publish_url, require_publish_enabled

# ---------------------------------------------------------------------------------------
# credential resolution
# ---------------------------------------------------------------------------------------

def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"missing required environment variable {name} (see .env.example)")
    return value


def _resolve_youtube_creds(credentials_ref: str) -> dict[str, str]:
    """OAuth installed-app creds: <REF>_CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN."""
    return {
        "client_id": _env(f"{credentials_ref}_CLIENT_ID"),
        "client_secret": _env(f"{credentials_ref}_CLIENT_SECRET"),
        "refresh_token": _env(f"{credentials_ref}_REFRESH_TOKEN"),
    }


def _resolve_x_creds(credentials_ref: str) -> dict[str, str]:
    return {
        "api_key": _env(f"{credentials_ref}_API_KEY"),
        "api_secret": _env(f"{credentials_ref}_API_SECRET"),
        "access_token": _env(f"{credentials_ref}_ACCESS_TOKEN"),
        "access_secret": _env(f"{credentials_ref}_ACCESS_SECRET"),
    }


# ---------------------------------------------------------------------------------------
# YouTube (Data API v3, resumable videos.insert + captions.insert)
# ---------------------------------------------------------------------------------------

def youtube_upload(
    *,
    video_path: str | Path,
    title: str,
    description: str,
    tags: list[str] | None = None,
    category_id: str = "25",
    privacy: str = "private",
    channel_id: str | None = None,
    credentials_ref: str = "YT_DEFAULT",
    caption_files: list[tuple[str, str | Path]] | None = None,
    playlist_id: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Upload a video to YouTube via the Data API v3.

    dry_run (default) returns the exact insert request body without any network call.
    Live mode requires VIDTRANS_PUBLISH_ENABLED + the googleapiclient/google-auth SDKs +
    the OAuth refresh-token creds under `credentials_ref`.
    """
    video_path = Path(video_path)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    request_preview = {
        "platform": "youtube",
        "endpoint": "youtube.videos.insert",
        "part": "snippet,status",
        "body": body,
        "media_file": str(video_path),
        "channel_id": channel_id,
        "captions": [{"language": lang, "file": str(f)} for lang, f in (caption_files or [])],
        "playlist_id": playlist_id,
    }
    if dry_run:
        return {"platform": "youtube", "dry_run": True, "status": "prepared",
                "request_preview": request_preview}

    require_publish_enabled()
    if not video_path.is_file():
        raise ConfigurationError(f"video file not found: {video_path}")
    try:
        from google.auth.transport.requests import Request as GoogleRequest  # noqa: PLC0415
        from google.oauth2.credentials import Credentials  # noqa: PLC0415
        from googleapiclient.discovery import build  # noqa: PLC0415
        from googleapiclient.http import MediaFileUpload  # noqa: PLC0415
    except ImportError as exc:  # graceful degradation, never a crash
        raise PublishDisabled(
            "YouTube upload needs google-api-python-client + google-auth-oauthlib "
            "(`pip install '.[publish]'`)"
        ) from exc

    creds_env = _resolve_youtube_creds(credentials_ref)
    creds = Credentials(
        None,
        refresh_token=creds_env["refresh_token"],
        client_id=creds_env["client_id"],
        client_secret=creds_env["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube.force-ssl"],
    )
    creds.refresh(GoogleRequest())
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True)
    insert = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _status, response = insert.next_chunk()
    video_id = response["id"]
    url = f"https://www.youtube.com/watch?v={video_id}"

    caption_results = []
    for lang, cap_file in (caption_files or []):
        cap_path = Path(cap_file)
        if not cap_path.is_file():
            caption_results.append({"language": lang, "status": "skipped", "reason": "file missing"})
            continue
        cap_media = MediaFileUpload(str(cap_path), mimetype="application/octet-stream", resumable=False)
        youtube.captions().insert(
            part="snippet",
            body={"snippet": {"videoId": video_id, "language": lang, "name": ""}},
            media_body=cap_media,
        ).execute()
        caption_results.append({"language": lang, "status": "uploaded"})

    if playlist_id:
        youtube.playlistItems().insert(
            part="snippet",
            body={"snippet": {"playlistId": playlist_id,
                              "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
        ).execute()

    return {"platform": "youtube", "dry_run": False, "status": "uploaded",
            "video_id": video_id, "url": url, "privacy": privacy,
            "captions": caption_results, "request_preview": request_preview}


# ---------------------------------------------------------------------------------------
# X / Twitter (API v2 create tweet; optional media via v1.1 upload)
# ---------------------------------------------------------------------------------------

def x_post(
    *,
    message: str,
    credentials_ref: str = "X",
    media_path: str | Path | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Post to X/Twitter (API v2). dry_run returns the tweet body without posting."""
    request_preview = {
        "platform": "x",
        "endpoint": "POST https://api.twitter.com/2/tweets",
        "body": {"text": message},
        "media_file": str(media_path) if media_path else None,
    }
    if dry_run:
        return {"platform": "x", "dry_run": True, "status": "prepared",
                "request_preview": request_preview}

    require_publish_enabled()
    try:
        import tweepy  # noqa: PLC0415
    except ImportError as exc:
        raise PublishDisabled("X posting needs tweepy (`pip install '.[publish]'`)") from exc

    creds = _resolve_x_creds(credentials_ref)
    client = tweepy.Client(
        consumer_key=creds["api_key"], consumer_secret=creds["api_secret"],
        access_token=creds["access_token"], access_token_secret=creds["access_secret"],
    )
    media_ids = None
    if media_path:
        media_path = Path(media_path)
        if not media_path.is_file():
            raise ConfigurationError(f"media file not found: {media_path}")
        auth = tweepy.OAuth1UserHandler(
            creds["api_key"], creds["api_secret"], creds["access_token"], creds["access_secret"],
        )
        api_v1 = tweepy.API(auth)
        uploaded = api_v1.media_upload(str(media_path))
        media_ids = [uploaded.media_id_string]
    resp = client.create_tweet(text=message, media_ids=media_ids)
    tweet_id = str(resp.data["id"])
    return {"platform": "x", "dry_run": False, "status": "posted", "post_id": tweet_id,
            "url": f"https://x.com/i/web/status/{tweet_id}", "request_preview": request_preview}


# ---------------------------------------------------------------------------------------
# Telegram (Bot API — sendMessage) and Discord (webhook) — plain HTTPS, no SDK
# ---------------------------------------------------------------------------------------

def _http_post_json(url: str, payload: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    check_publish_url(url)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - host checked against PUBLISH_HOSTS
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
            return {"http_status": resp.status, "body": json.loads(raw) if raw else {}}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        host = urllib.parse.urlparse(url).hostname
        raise ConfigurationError(f"HTTP {exc.code} from {host}: {detail}") from exc


def telegram_post(
    *,
    message: str,
    credentials_ref: str = "TELEGRAM",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Send a message to a Telegram channel/chat via the Bot API.

    Creds: <REF>_BOT_TOKEN and <REF>_CHANNEL (channel @username or numeric chat id)."""
    request_preview = {
        "platform": "telegram",
        "endpoint": "POST https://api.telegram.org/bot<token>/sendMessage",
        "body": {"chat_id": "<from env>", "text": message, "disable_web_page_preview": False},
    }
    if dry_run:
        return {"platform": "telegram", "dry_run": True, "status": "prepared",
                "request_preview": request_preview}

    require_publish_enabled()
    token = _env(f"{credentials_ref}_BOT_TOKEN")
    chat_id = _env(f"{credentials_ref}_CHANNEL")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    result = _http_post_json(url, {"chat_id": chat_id, "text": message})
    msg = result["body"].get("result", {})
    post_id = str(msg.get("message_id", "")) or None
    return {"platform": "telegram", "dry_run": False, "status": "posted", "post_id": post_id,
            "request_preview": request_preview}


def discord_post(
    *,
    message: str,
    credentials_ref: str = "DISCORD",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Post to a Discord channel via an incoming webhook. Creds: <REF>_WEBHOOK_URL."""
    request_preview = {
        "platform": "discord",
        "endpoint": "POST <DISCORD_WEBHOOK_URL>",
        "body": {"content": message},
    }
    if dry_run:
        return {"platform": "discord", "dry_run": True, "status": "prepared",
                "request_preview": request_preview}

    require_publish_enabled()
    webhook = _env(f"{credentials_ref}_WEBHOOK_URL")
    check_publish_url(webhook)
    _http_post_json(webhook, {"content": message})
    return {"platform": "discord", "dry_run": False, "status": "posted", "post_id": None,
            "request_preview": request_preview}


# Dispatch table for promotional posts (video upload goes through youtube_upload directly).
PROMO_DISPATCH = {
    "x": x_post,
    "telegram": telegram_post,
    "discord": discord_post,
}


def post_promotion(platform: str, *, message: str, credentials_ref: str,
                   dry_run: bool = True) -> dict[str, Any]:
    """Route a promotional post to the right automatable-platform function."""
    fn = PROMO_DISPATCH.get(platform)
    if fn is None:
        raise ConfigurationError(f"platform {platform!r} is not automatable via net/publish")
    return fn(message=message, credentials_ref=credentials_ref, dry_run=dry_run)
