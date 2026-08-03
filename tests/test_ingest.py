"""Phase 1 tests: catalog merge semantics, language ID, ingest guards, net egress gate.

These avoid the network and (mostly) avoid ffmpeg so they run anywhere. A single probe
test is skipped when ffmpeg is absent.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import catalog, langid, project  # noqa: E402
from video_translation_house.errors import ConfigurationError, FetchDisabled  # noqa: E402
from video_translation_house.net import fetch  # noqa: E402


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "CLAUDE.md").write_text("test repo\n")
    shutil.copytree(REPO / ".claude" / "schemas", root / ".claude" / "schemas")
    shutil.copytree(REPO / ".claude" / "config", root / ".claude" / "config")
    (root / "projects").mkdir()
    return root


def _mkproject(root: Path):
    return project.init_project(
        root, "yt-vid00000001", url="https://youtube.com/watch?v=vid00000001",
        target_languages=["en", "ar"], audio_languages=["en"],
    )


# --- catalog merge semantics -------------------------------------------------

def test_catalog_add_then_merge_logs_diff(repo: Path):
    catalog.upsert_entry(repo, {"video_id": "yt-abc", "url": "https://youtu.be/abc"})
    entry = catalog.get_entry(repo, "yt-abc")
    assert entry["rights_status"] == "unreviewed"
    assert entry["added_at"]

    # A merge with a new title must not wipe existing fields and must log a diff.
    catalog.upsert_entry(repo, {"video_id": "yt-abc", "url": "https://youtu.be/abc",
                                "title": "Lecture 1", "language": "fa"})
    merged = catalog.get_entry(repo, "yt-abc")
    assert merged["title"] == "Lecture 1"
    assert merged["language"] == "fa"
    assert merged["url"] == "https://youtu.be/abc"

    events = (repo / "catalog" / "events.ndjson").read_text().strip().splitlines()
    kinds = [__import__("json").loads(e)["event"] for e in events]
    assert kinds == ["CATALOG_ENTRY_ADDED", "CATALOG_ENTRY_MERGED"]


def test_catalog_null_does_not_erase(repo: Path):
    catalog.upsert_entry(repo, {"video_id": "yt-abc", "url": "u", "title": "Keep me"})
    catalog.upsert_entry(repo, {"video_id": "yt-abc", "url": "u", "title": None})
    assert catalog.get_entry(repo, "yt-abc")["title"] == "Keep me"


def test_catalog_rejects_unknown_field(repo: Path):
    with pytest.raises(ConfigurationError):
        catalog.upsert_entry(repo, {"video_id": "yt-abc", "url": "u", "bogus": 1})


def test_catalog_rejects_bad_video_id(repo: Path):
    with pytest.raises(ConfigurationError):
        catalog.upsert_entry(repo, {"video_id": "Bad ID!", "url": "u"})


# --- language id -------------------------------------------------------------

def test_normalize_language():
    assert langid.normalize_language("Persian") == "fa"
    assert langid.normalize_language("ara") == "ar"
    assert langid.normalize_language("en") == "en"
    assert langid.normalize_language("klingon-longform") is None


def test_langid_set_updates_state_and_catalog(repo: Path):
    _mkproject(repo)
    catalog.upsert_entry(repo, {"video_id": "yt-vid00000001", "url": "u"})
    langid.set_language(repo, "yt-vid00000001", "fa", source="manual", confidence=0.9)
    st = project.project_status(repo, "yt-vid00000001")["state"]
    assert st["source_language"] == "fa"
    assert catalog.get_entry(repo, "yt-vid00000001")["language"] == "fa"


def test_langid_detect_is_nonblocking(repo: Path):
    result = langid.detect_from_audio(repo, repo / "nope.wav")
    assert result["available"] is False
    assert result["detected"] is None


# --- net egress gate ---------------------------------------------------------

def test_fetch_disabled_by_default(monkeypatch):
    monkeypatch.delenv("VIDTRANS_FETCH_ENABLED", raising=False)
    assert fetch.fetch_enabled() is False
    with pytest.raises(FetchDisabled):
        fetch.ytdlp_probe("https://youtube.com/watch?v=x")


def test_fetch_url_allowlist(monkeypatch):
    monkeypatch.setenv("VIDTRANS_FETCH_ENABLED", "1")
    with pytest.raises(ConfigurationError):
        fetch.ytdlp_probe("https://evil.example.com/x")


# --- ingest guards -----------------------------------------------------------

def test_ingest_requires_ingest_state(repo: Path):
    from video_translation_house import ingest, state
    _mkproject(repo)
    state.transition(repo, "yt-vid00000001", "LANGUAGE_ID", "agent")
    with pytest.raises(ConfigurationError):
        ingest.ingest_project(repo, "yt-vid00000001")


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe not installed")
def test_probe_summary_on_generated_clip(repo: Path, tmp_path: Path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    from video_translation_house.media import probe_summary
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=128x128:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest", str(clip)],
        capture_output=True, check=True,
    )
    summary = probe_summary(clip)
    assert summary["video"]["width"] == 128
    assert summary["audio"]["codec"]
    assert summary["duration_seconds"] and summary["duration_seconds"] > 0


def _touch_env():  # keep os import used even if ffmpeg tests skip
    return os.environ.get("PATH")
