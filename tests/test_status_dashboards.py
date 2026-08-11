"""Tests for the status-dashboard CLI enhancements (C1/C2) backing the `/hi` router.

  * catalog.summarize tallies enriched entries by derived status bucket (shared by
    `catalog list` and `catalog playlist`, so the two never drift);
  * `catalog list` returns {video_count, status_summary, videos}, status_summary always
    covering the FULL unfiltered set while --status / --playlist filter only `videos`;
  * `project list` enriches each row with status / autonomy_action / next_command and returns
    {projects_count, status_summary, projects} with a --status filter.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import catalog, project  # noqa: E402
from video_translation_house.cli import build_parser, dispatch  # noqa: E402
from video_translation_house.paths import ProjectPaths  # noqa: E402
from video_translation_house.util import atomic_write_json, load_json  # noqa: E402


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


def _run(root: Path, argv: list[str]):
    """Invoke a CLI verb through the real parser + dispatch (no subprocess)."""
    args = build_parser().parse_args(argv)
    return dispatch(args, root)


def _force_state(root: Path, project_id: str, current_state: str) -> None:
    paths = ProjectPaths(root, project_id)
    st = load_json(paths.state)
    st["current_state"] = current_state
    atomic_write_json(paths.state, st)


# --------------------------------------------------------------------------------------
# catalog.summarize
# --------------------------------------------------------------------------------------
def test_summarize_tallies_by_status_bucket() -> None:
    entries = [
        {"status": "not-started"}, {"status": "not-started"},
        {"status": "in-progress"}, {"status": "done"},
        {},  # missing status -> counted as unknown, never raises
    ]
    assert catalog.summarize(entries) == {
        "not-started": 2, "in-progress": 1, "done": 1, "unknown": 1,
    }


# --------------------------------------------------------------------------------------
# C1 — catalog list
# --------------------------------------------------------------------------------------
def test_catalog_list_returns_summary_and_video_count(repo: Path) -> None:
    # Two not-started (no project) + one in-progress (real project fresh at INGEST).
    catalog.upsert_entry(repo, {"video_id": "yt-AAAAAAAAAAA",
                                "url": "https://youtu.be/AAAAAAAAAAA"})
    catalog.upsert_entry(repo, {"video_id": "yt-BBBBBBBBBBB",
                                "url": "https://youtu.be/BBBBBBBBBBB"})
    project.init_project(repo, "yt-CCCCCCCCCCC", url="https://youtu.be/CCCCCCCCCCC",
                         target_languages=["en", "fr"])
    catalog.upsert_entry(repo, {"video_id": "yt-CCCCCCCCCCC",
                                "url": "https://youtu.be/CCCCCCCCCCC",
                                "project_id": "yt-CCCCCCCCCCC"})

    out = _run(repo, ["catalog", "list"])
    assert out["video_count"] == 3
    assert out["status_summary"] == {"not-started": 2, "in-progress": 1}
    assert len(out["videos"]) == 3  # unfiltered


def test_catalog_list_status_filter_scopes_videos_not_summary(repo: Path) -> None:
    catalog.upsert_entry(repo, {"video_id": "yt-AAAAAAAAAAA",
                                "url": "https://youtu.be/AAAAAAAAAAA"})
    project.init_project(repo, "yt-CCCCCCCCCCC", url="https://youtu.be/CCCCCCCCCCC",
                         target_languages=["en"])
    catalog.upsert_entry(repo, {"video_id": "yt-CCCCCCCCCCC",
                                "url": "https://youtu.be/CCCCCCCCCCC",
                                "project_id": "yt-CCCCCCCCCCC"})

    out = _run(repo, ["catalog", "list", "--status", "in-progress"])
    # summary still covers the full set (honest aggregate)...
    assert out["video_count"] == 2
    assert out["status_summary"] == {"not-started": 1, "in-progress": 1}
    # ...but videos is filtered to the requested bucket only.
    assert [v["video_id"] for v in out["videos"]] == ["yt-CCCCCCCCCCC"]


def test_catalog_list_status_filter_accepts_csv(repo: Path) -> None:
    catalog.upsert_entry(repo, {"video_id": "yt-AAAAAAAAAAA",
                                "url": "https://youtu.be/AAAAAAAAAAA"})
    project.init_project(repo, "yt-CCCCCCCCCCC", url="https://youtu.be/CCCCCCCCCCC",
                         target_languages=["en"])
    catalog.upsert_entry(repo, {"video_id": "yt-CCCCCCCCCCC",
                                "url": "https://youtu.be/CCCCCCCCCCC",
                                "project_id": "yt-CCCCCCCCCCC"})

    out = _run(repo, ["catalog", "list", "--status", "in-progress,not-started"])
    assert {v["video_id"] for v in out["videos"]} == {"yt-AAAAAAAAAAA", "yt-CCCCCCCCCCC"}


def test_catalog_list_playlist_filter(repo: Path) -> None:
    catalog.upsert_entry(repo, {"video_id": "yt-AAAAAAAAAAA",
                                "url": "https://youtu.be/AAAAAAAAAAA",
                                "playlist_id": "PLone"})
    catalog.upsert_entry(repo, {"video_id": "yt-BBBBBBBBBBB",
                                "url": "https://youtu.be/BBBBBBBBBBB",
                                "playlist_id": "PLtwo"})

    out = _run(repo, ["catalog", "list", "--playlist", "PLone"])
    assert out["video_count"] == 2  # summary spans both playlists
    assert [v["video_id"] for v in out["videos"]] == ["yt-AAAAAAAAAAA"]


# --------------------------------------------------------------------------------------
# C2 — project list
# --------------------------------------------------------------------------------------
def test_project_list_enriches_rows(repo: Path) -> None:
    project.init_project(repo, "yt-DDDDDDDDDDD", url="https://youtu.be/DDDDDDDDDDD",
                         target_languages=["en", "fr"])
    out = _run(repo, ["project", "list"])
    assert out["projects_count"] == 1
    assert out["status_summary"] == {"in-progress": 1}
    row = out["projects"][0]
    assert row["project_id"] == "yt-DDDDDDDDDDD"
    assert row["current_state"] == "INGEST"
    assert row["status"] == "in-progress"
    assert row["autonomy_action"] == "PROCEED"
    assert row["next_command"] and "ingest run yt-DDDDDDDDDDD" in row["next_command"]


def test_project_list_status_filter(repo: Path) -> None:
    project.init_project(repo, "yt-DDDDDDDDDDD", url="https://youtu.be/DDDDDDDDDDD",
                         target_languages=["en"])
    project.init_project(repo, "yt-EEEEEEEEEEE", url="https://youtu.be/EEEEEEEEEEE",
                         target_languages=["en"])
    _force_state(repo, "yt-EEEEEEEEEEE", "READY_FOR_REVIEW")

    out = _run(repo, ["project", "list", "--status", "in-progress"])
    # summary honest over the full set (1 in-progress + 1 done)...
    assert out["status_summary"] == {"in-progress": 1, "done": 1}
    # ...projects filtered to in-progress only.
    assert [p["project_id"] for p in out["projects"]] == ["yt-DDDDDDDDDDD"]


def test_project_list_done_row_has_no_next_command(repo: Path) -> None:
    project.init_project(repo, "yt-EEEEEEEEEEE", url="https://youtu.be/EEEEEEEEEEE",
                         target_languages=["en"])
    _force_state(repo, "yt-EEEEEEEEEEE", "READY_FOR_REVIEW")
    out = _run(repo, ["project", "list"])
    row = out["projects"][0]
    assert row["status"] == "done"
    assert row["autonomy_action"] == "TERMINAL"
    assert row["next_command"] is None
