"""Tests for `ingest ensure` (ingest.ensure_source_present): a media-restore that re-fetches
the source video/WAV if deleted, without touching top-level project state.

  * no-op when the video + WAV are already on disk (restored: False);
  * re-fetch + re-register (source-video/source-audio) + SOURCE_RESTORED event when missing,
    using the flag-gated network module (monkeypatched here — no real egress);
  * clean FetchDisabled (not a crash) when media is missing and egress is off.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import artifacts, ingest, project  # noqa: E402
from video_translation_house.errors import FetchDisabled  # noqa: E402
from video_translation_house.events import read_events  # noqa: E402
from video_translation_house.paths import ProjectPaths  # noqa: E402


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "CLAUDE.md").write_text("test repo\n")
    shutil.copytree(REPO / ".claude" / "schemas", root / ".claude" / "schemas")
    shutil.copytree(REPO / ".claude" / "config", root / ".claude" / "config")
    (root / "projects").mkdir()
    (root / "catalog").mkdir()
    return root


VID = "yt-ensuresrc099"
URL = "https://youtube.com/watch?v=ensuresrc099"


def _init(root: Path) -> ProjectPaths:
    project.init_project(root, VID, url=URL, target_languages=["en", "fr"])
    return ProjectPaths(root, VID)


def _place_media(paths: ProjectPaths, *, video: bool = True, wav: bool = True) -> None:
    paths.source_dir.mkdir(parents=True, exist_ok=True)
    if video:
        (paths.source_dir / "video.mp4").write_bytes(b"fake-mp4-bytes")
    if wav:
        (paths.source_dir / "audio.wav").write_bytes(b"fake-wav-bytes")


def test_noop_when_media_present(repo: Path):
    paths = _init(repo)
    _place_media(paths)
    result = ingest.ensure_source_present(repo, VID)
    assert result["restored"] is False
    events = [e["event"] for e in read_events(paths.events)]
    assert "SOURCE_RESTORED" not in events


def test_refetch_when_missing(repo: Path, monkeypatch: pytest.MonkeyPatch):
    paths = _init(repo)
    # No media on disk. Stub the network download + WAV extraction (no ffmpeg / egress).
    def fake_download(url, dest_dir, *, write_subs=True):
        video = Path(dest_dir) / "video.mp4"
        video.write_bytes(b"downloaded-mp4")
        return {"video_path": str(video), "info": {}}

    def fake_extract(video_path, wav_path):
        Path(wav_path).write_bytes(b"extracted-wav")

    monkeypatch.setattr("video_translation_house.net.ytdlp_download", fake_download)
    monkeypatch.setattr("video_translation_house.ingest.extract_wav", fake_extract)

    result = ingest.ensure_source_present(repo, VID)
    assert result["restored"] is True
    assert (paths.source_dir / "video.mp4").is_file()
    assert (paths.source_dir / "audio.wav").is_file()

    # Artifacts re-registered so provenance stays intact.
    kinds = {a["type"] for a in artifacts.list_artifacts(repo, VID)}
    assert {"source-video", "source-audio"} <= kinds

    events = [e["event"] for e in read_events(paths.events)]
    assert "SOURCE_RESTORED" in events


def test_fetch_disabled_when_missing_and_egress_off(repo: Path, monkeypatch: pytest.MonkeyPatch):
    paths = _init(repo)  # noqa: F841 — ensures project exists; media deliberately absent
    # Real ytdlp_download: with egress off it raises FetchDisabled before spawning anything.
    monkeypatch.delenv("VIDTRANS_FETCH_ENABLED", raising=False)
    with pytest.raises(FetchDisabled):
        ingest.ensure_source_present(repo, VID)
