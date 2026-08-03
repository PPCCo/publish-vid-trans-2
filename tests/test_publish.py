"""Phase 6 tests: the sanctioned external-write module (net/publish.py).

These exercise ONLY the dry-run / prepare path (no credentials, no network) plus the guard
posture: require_publish_enabled() raises when VIDTRANS_PUBLISH_ENABLED is unset, and the
publish-host allowlist rejects a foreign endpoint. No live API is ever contacted.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house.errors import ConfigurationError, PublishDisabled  # noqa: E402
from video_translation_house.net import fetch, publish  # noqa: E402


@pytest.fixture(autouse=True)
def _publish_disabled(monkeypatch):
    """Default posture for these tests: publishing OFF (no live path ever runs)."""
    monkeypatch.delenv("VIDTRANS_PUBLISH_ENABLED", raising=False)


# --- flag posture ------------------------------------------------------------

def test_require_publish_enabled_raises_when_unset(monkeypatch):
    monkeypatch.delenv("VIDTRANS_PUBLISH_ENABLED", raising=False)
    assert fetch.publish_enabled() is False
    with pytest.raises(PublishDisabled):
        fetch.require_publish_enabled()


def test_publish_enabled_true_when_flag_set(monkeypatch):
    monkeypatch.setenv("VIDTRANS_PUBLISH_ENABLED", "1")
    assert fetch.publish_enabled() is True
    fetch.require_publish_enabled()  # does not raise


def test_check_publish_url_allows_known_hosts_rejects_foreign():
    fetch.check_publish_url("https://api.telegram.org/bot123/sendMessage")
    fetch.check_publish_url("https://discord.com/api/webhooks/1/abc")
    with pytest.raises(ConfigurationError):
        fetch.check_publish_url("https://evil.example.com/steal")


# --- dry-run request previews ------------------------------------------------

def test_youtube_dry_run_returns_insert_body(tmp_path):
    vid = tmp_path / "dubbed.mp4"
    vid.write_bytes(b"fake")
    res = publish.youtube_upload(
        video_path=vid, title="A talk [en]", description="body\n\nChapters:\n0:00 Intro",
        tags=["speech"], category_id="25", privacy="private",
        caption_files=[("en", tmp_path / "captions.en.srt")], dry_run=True,
    )
    assert res["dry_run"] is True and res["status"] == "prepared"
    prev = res["request_preview"]
    assert prev["endpoint"] == "youtube.videos.insert"
    assert prev["body"]["snippet"]["title"] == "A talk [en]"
    assert prev["body"]["status"]["privacyStatus"] == "private"
    assert prev["captions"] == [{"language": "en", "file": str(tmp_path / "captions.en.srt")}]


def test_x_dry_run_returns_tweet_body():
    res = publish.x_post(message="hello world", dry_run=True)
    assert res["dry_run"] is True
    assert res["request_preview"]["body"] == {"text": "hello world"}
    assert "2/tweets" in res["request_preview"]["endpoint"]


def test_telegram_and_discord_dry_run():
    tg = publish.telegram_post(message="hi", dry_run=True)
    assert tg["request_preview"]["body"]["text"] == "hi"
    dc = publish.discord_post(message="hi", dry_run=True)
    assert dc["request_preview"]["body"] == {"content": "hi"}


def test_post_promotion_dispatch_and_unknown_platform():
    res = publish.post_promotion("telegram", message="hi", credentials_ref="TELEGRAM", dry_run=True)
    assert res["platform"] == "telegram" and res["dry_run"] is True
    with pytest.raises(ConfigurationError):
        publish.post_promotion("reddit", message="hi", credentials_ref="REDDIT", dry_run=True)


# --- live path degrades gracefully when SDK / creds absent -------------------

def test_youtube_live_without_flag_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDTRANS_PUBLISH_ENABLED", raising=False)
    vid = tmp_path / "dubbed.mp4"
    vid.write_bytes(b"fake")
    # dry_run=False but flag unset: require_publish_enabled() short-circuits before any SDK.
    with pytest.raises(PublishDisabled):
        publish.youtube_upload(video_path=vid, title="t", description="d", dry_run=False)


def test_telegram_live_without_flag_raises(monkeypatch):
    monkeypatch.delenv("VIDTRANS_PUBLISH_ENABLED", raising=False)
    with pytest.raises(PublishDisabled):
        publish.telegram_post(message="hi", dry_run=False)
