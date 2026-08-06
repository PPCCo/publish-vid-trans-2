"""Tests for Feature F: playlist ingest + catalog next_command / review_files.

  * ytdlp_playlist_entries is a metadata-only carve-out — it does NOT call require_fetch_enabled;
  * add_playlist indexes each video with playlist_id and derives a valid yt-<id>;
  * an already-catalogued (in-progress) video keeps its status + project_id and merely gains the
    playlist fields — it moves under the playlist, no duplicate row;
  * enrich_entry derives the right next_command per autonomy_action and only attaches
    review_files at a STOP_AT_GATE step.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import catalog, project  # noqa: E402
from video_translation_house.util import load_json  # noqa: E402


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


PLAYLIST_URL = "https://youtube.com/playlist?list=PLtestplaylist"

_FAKE_ENTRIES = [
    {"id": "AAAAAAAAAAA", "url": "https://youtu.be/AAAAAAAAAAA", "title": "One",
     "channel": "Ch", "duration_seconds": 100, "playlist_id": "PLtestplaylist",
     "playlist_title": "My Playlist"},
    {"id": "BBBBBBBBBBB", "url": "https://youtu.be/BBBBBBBBBBB", "title": "Two",
     "channel": "Ch", "duration_seconds": 200, "playlist_id": "PLtestplaylist",
     "playlist_title": "My Playlist"},
]


def _stub_entries(monkeypatch: pytest.MonkeyPatch, entries=_FAKE_ENTRIES) -> None:
    monkeypatch.setattr("video_translation_house.net.ytdlp_playlist_entries",
                        lambda url, **kw: list(entries))


def test_playlist_enumeration_is_flag_free(monkeypatch: pytest.MonkeyPatch):
    """ytdlp_playlist_entries must NOT gate on VIDTRANS_FETCH_ENABLED (metadata-only carve-out).
    We assert it never calls require_fetch_enabled by making that raise if invoked."""
    from video_translation_house.net import fetch

    monkeypatch.delenv("VIDTRANS_FETCH_ENABLED", raising=False)

    def _boom() -> None:
        raise AssertionError("require_fetch_enabled must not be called during enumeration")

    monkeypatch.setattr(fetch, "require_fetch_enabled", _boom)
    monkeypatch.setattr(fetch, "_check_url", lambda url: None)
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stdout = ('{"id": "PLx", "title": "T", "entries": '
                      '[{"id": "AAAAAAAAAAA", "title": "One"}]}')
            stderr = ""
        return R()

    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    monkeypatch.setattr(fetch, "_ytdlp", lambda: "yt-dlp")
    out = fetch.ytdlp_playlist_entries(PLAYLIST_URL)
    assert out and out[0]["id"] == "AAAAAAAAAAA"
    assert "--flat-playlist" in captured["cmd"]


def test_add_playlist_indexes_videos(repo: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_entries(monkeypatch)
    result = catalog.add_playlist(repo, PLAYLIST_URL)
    assert result["playlist_id"] == "PLtestplaylist"
    assert set(result["added"]) == {"yt-AAAAAAAAAAA", "yt-BBBBBBBBBBB"}
    assert result["linked_existing"] == []

    entries = catalog.list_entries(repo)
    ids = {e["video_id"] for e in entries}
    assert ids == {"yt-AAAAAAAAAAA", "yt-BBBBBBBBBBB"}
    for e in entries:
        assert e["playlist_id"] == "PLtestplaylist"
        assert e["playlist_title"] == "My Playlist"

    # Reverse index populated.
    pl = catalog.get_playlist(repo, "PLtestplaylist")
    assert pl is not None
    assert set(pl["video_ids"]) == {"yt-AAAAAAAAAAA", "yt-BBBBBBBBBBB"}


def test_existing_video_moves_under_playlist_no_dup(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # A video already catalogued + in-progress (has project_id + non-default rights).
    catalog.upsert_entry(repo, {
        "video_id": "yt-AAAAAAAAAAA", "url": "https://youtu.be/AAAAAAAAAAA",
        "project_id": "yt-AAAAAAAAAAA", "rights_status": "licensed",
    })
    _stub_entries(monkeypatch)
    result = catalog.add_playlist(repo, PLAYLIST_URL)

    assert "yt-AAAAAAAAAAA" in result["linked_existing"]
    assert "yt-AAAAAAAAAAA" not in result["added"]

    entries = [e for e in catalog.list_entries(repo) if e["video_id"] == "yt-AAAAAAAAAAA"]
    assert len(entries) == 1  # no duplicate row
    e = entries[0]
    assert e["playlist_id"] == "PLtestplaylist"      # gained playlist grouping
    assert e["project_id"] == "yt-AAAAAAAAAAA"        # status preserved
    assert e["rights_status"] == "licensed"           # status preserved


def test_enrich_kickoff_for_uncatalogued_project(repo: Path):
    catalog.upsert_entry(repo, {"video_id": "yt-CCCCCCCCCCC",
                                "url": "https://youtu.be/CCCCCCCCCCC"})
    entry = catalog.get_entry(repo, "yt-CCCCCCCCCCC")
    enriched = catalog.enrich_entry(repo, entry)
    assert "project init yt-CCCCCCCCCCC" in enriched["next_command"]
    assert "ingest run" in enriched["next_command"]
    assert "review_files" not in enriched


def test_enrich_proceed_has_command_no_review_files(repo: Path):
    # A real project fresh at INGEST -> PROCEED (no gate) -> command but no review_files.
    project.init_project(repo, "yt-DDDDDDDDDDD", url="https://youtu.be/DDDDDDDDDDD",
                         target_languages=["en", "fr"])
    catalog.upsert_entry(repo, {"video_id": "yt-DDDDDDDDDDD",
                                "url": "https://youtu.be/DDDDDDDDDDD",
                                "project_id": "yt-DDDDDDDDDDD"})
    enriched = catalog.enrich_entry(repo, catalog.get_entry(repo, "yt-DDDDDDDDDDD"))
    # At INGEST the next step is a non-gate PROCEED; review_files must be absent off-gate.
    assert enriched["next_command"] is not None
    assert "review_files" not in enriched


def test_enrich_gate_attaches_review_files(repo: Path):
    from video_translation_house import langid, state  # noqa: F401
    from video_translation_house.paths import ProjectPaths
    from video_translation_house.util import atomic_write_json, utc_now

    project.init_project(repo, "yt-EEEEEEEEEEE", url="https://youtu.be/EEEEEEEEEEE",
                         target_languages=["en", "fr"])
    langid.set_language(repo, "yt-EEEEEEEEEEE", "fa", source="manual", confidence=0.9)
    # Drive to TRANSLATION_QA_GATE with en (human-reviewed) still needing approval, and
    # write the gate report doc so review_files can resolve to it.
    paths = ProjectPaths(repo, "yt-EEEEEEEEEEE")
    st = load_json(paths.state)
    st["previous_state"] = "TRANSLATION"
    st["current_state"] = "TRANSLATION_QA_GATE"
    for _lang, t in st["language_tracks"].items():
        if not t.get("skip_translation"):
            t["stage"] = "TRANSLATION_QA_GATE"
            t["status"] = "in_progress"
            t["updated_at"] = utc_now()
    atomic_write_json(paths.state, st)
    report = paths.gate_report("translation-qa")
    report.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(report, {"report_type": "translation-qa", "decision": "PASS"})

    catalog.upsert_entry(repo, {"video_id": "yt-EEEEEEEEEEE",
                                "url": "https://youtu.be/EEEEEEEEEEE",
                                "project_id": "yt-EEEEEEEEEEE"})
    enriched = catalog.enrich_entry(repo, catalog.get_entry(repo, "yt-EEEEEEEEEEE"))
    assert "approval grant" in enriched["next_command"]
    assert enriched.get("review_files")
    assert any("translation-qa" in f for f in enriched["review_files"])
