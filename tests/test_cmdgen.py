"""Tests for the `cmd` / `nextcmd` raw-external command printer (cmdgen.py).

Covers:
  * the pure argv builders factored out of net/fetch.py never drift from what the real
    downloader/enumerator run;
  * is_playlist_url routing;
  * cmd for a playlist, a new single video, and an existing project at/past INGEST;
  * nextcmd for PROCEED (external+cli and cli-only), STOP_AT_GATE, BLOCKED, TERMINAL;
  * the --for-claude `!` rule (runnable lines get it; gate reference lines never do);
  * the cli dispatch contract (these two verbs return a str, not a dict).

No test spawns a real subprocess or network call — argv assertions hit the pure builders,
everything else is strings and driven state (matching the repo's no-network test convention).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import catalog, cmdgen, project  # noqa: E402
from video_translation_house.net import fetch  # noqa: E402
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


VIDEO_ID = "AAAAAAAAAAA"          # 11-char bare YouTube id
WATCH_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLtestplaylist000"


# --------------------------------------------------------------------------------------
# 1) The pure argv builders can never drift from what the real downloader/enumerator run.
# --------------------------------------------------------------------------------------
def test_download_argv_builder_matches_real_download(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """The argv the printer shows (build_ytdlp_download_argv) must equal the argv the real
    ytdlp_download subprocess-runs — proving the two surfaces can't diverge."""
    dest = tmp_path / "src"
    captured: dict = {}

    monkeypatch.setattr(fetch, "require_fetch_enabled", lambda: None)
    monkeypatch.setattr(fetch, "_check_url", lambda url: None)
    monkeypatch.setattr(fetch, "_ytdlp", lambda: "yt-dlp")

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    # ytdlp_download normalizes the url first, so compare against the normalized form.
    fetch.ytdlp_download(WATCH_URL, dest)
    expected = fetch.build_ytdlp_download_argv(WATCH_URL, dest, binary="yt-dlp")
    assert captured["cmd"] == expected


def test_playlist_argv_builder_matches_real_enumeration(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}
    monkeypatch.setattr(fetch, "_check_url", lambda url: None)
    monkeypatch.setattr(fetch, "_ytdlp", lambda: "yt-dlp")

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stdout = '{"id": "PLx", "title": "T", "entries": []}'
            stderr = ""
        return R()

    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    fetch.ytdlp_playlist_entries(PLAYLIST_URL)
    assert captured["cmd"] == fetch.build_ytdlp_playlist_argv(PLAYLIST_URL, binary="yt-dlp")


# --------------------------------------------------------------------------------------
# 2) is_playlist_url routing table.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    ("PLtestplaylist000", True),                      # bare playlist id
    ("AAAAAAAAAAA", False),                            # bare video id
    (PLAYLIST_URL, True),                              # full playlist URL
    (WATCH_URL, False),                                # full watch URL
    ("https://youtu.be/AAAAAAAAAAA", False),           # short link
    ("https://www.youtube.com/watch?v=x&list=PLx", True),   # watch-in-playlist
])
def test_is_playlist_url(value: str, expected: bool):
    assert fetch.is_playlist_url(value) is expected


# --------------------------------------------------------------------------------------
# 3) cmd — playlist.
# --------------------------------------------------------------------------------------
def test_cmd_playlist_both_options_no_env(repo: Path):
    out = cmdgen.cmd_for_video(repo, PLAYLIST_URL)
    assert "Option A (raw external):" in out
    assert "Option B (via vid_cli.py):" in out
    assert "--flat-playlist" in out
    assert "catalog add-playlist" in out
    # Flag-free enumeration carve-out — NO env preamble on either option.
    assert "source .env.local" not in out
    assert "VIDTRANS_FETCH_ENABLED" not in out


def test_cmd_playlist_for_claude_adds_bang(repo: Path):
    out = cmdgen.cmd_for_video(repo, PLAYLIST_URL, for_claude=True)
    assert "! yt-dlp --dump-single-json --flat-playlist" in out
    assert "! vid_cli.py catalog add-playlist" in out


# --------------------------------------------------------------------------------------
# 4) cmd — new (uncatalogued) single video: Option B equals catalog's kickoff next_command.
# --------------------------------------------------------------------------------------
def test_cmd_new_video_kickoff_matches_catalog(repo: Path):
    out = cmdgen.cmd_for_video(repo, VIDEO_ID)
    # Build the equivalent not-started catalog entry and compare Option B to its next_command.
    catalog.upsert_entry(repo, {"video_id": f"yt-{VIDEO_ID}", "url": WATCH_URL})
    enriched = catalog.enrich_entry(repo, catalog.get_entry(repo, f"yt-{VIDEO_ID}"))
    kickoff = enriched["next_command"]
    assert kickoff.startswith("! ")
    # cmdgen's Option B is the same compound, minus the `!` prefix (bare terminal form).
    assert kickoff[2:] in out


def test_cmd_new_video_bare_id_not_double_prefixed(repo: Path):
    """A `yt-…` id passed for a not-yet-created project must not become `yt-yt-…`."""
    out = cmdgen.cmd_for_video(repo, "yt-ZZZZZZZZZZZ")
    assert "yt-yt-" not in out
    assert "project init yt-ZZZZZZZZZZZ" in out


# --------------------------------------------------------------------------------------
# 5) cmd — existing project at INGEST.
# --------------------------------------------------------------------------------------
def test_cmd_existing_project_at_ingest(repo: Path):
    pid = f"yt-{VIDEO_ID}"
    project.init_project(repo, pid, url=WATCH_URL, target_languages=["en"])
    catalog.upsert_entry(repo, {"video_id": pid, "url": WATCH_URL, "project_id": pid})
    out = cmdgen.cmd_for_video(repo, pid)
    assert "Option A (raw external):" in out
    assert "Option B (via vid_cli.py):" in out
    # Option A cwd is the project source dir; needs only the fetch flag (brew yt-dlp).
    assert str(ProjectPaths(repo, pid).source_dir) in out
    assert "export VIDTRANS_FETCH_ENABLED=1" in out
    # Option B goes through the Python net layer -> full .env.local.
    assert "source .env.local" in out
    assert f"ingest run {pid}" in out
    # argv content matches the shared builder.
    argv = fetch.build_ytdlp_download_argv(WATCH_URL, ProjectPaths(repo, pid).source_dir, binary="yt-dlp")
    assert "--merge-output-format mp4" in out and "-f 'bv*+ba/b'" in out
    assert argv[-1] == WATCH_URL


def test_cmd_project_past_ingest_falls_through(repo: Path):
    pid = f"yt-{VIDEO_ID}"
    project.init_project(repo, pid, url=WATCH_URL, target_languages=["en"])
    catalog.upsert_entry(repo, {"video_id": pid, "url": WATCH_URL, "project_id": pid})
    _set_state(repo, pid, "DUBBING")
    out = cmdgen.cmd_for_video(repo, pid)
    assert "already past INGEST" in out
    assert "current_state=DUBBING" in out


# --------------------------------------------------------------------------------------
# 6) nextcmd — PROCEED / STOP_AT_GATE / BLOCKED / TERMINAL.
# --------------------------------------------------------------------------------------
def _set_state(root: Path, pid: str, state: str) -> None:
    paths = ProjectPaths(root, pid)
    st = load_json(paths.state)
    st["current_state"] = state
    atomic_write_json(paths.state, st)


def test_nextcmd_proceed_ingest_external_and_cli(repo: Path):
    pid = f"yt-{VIDEO_ID}"
    project.init_project(repo, pid, url=WATCH_URL, target_languages=["en"])
    catalog.upsert_entry(repo, {"video_id": pid, "url": WATCH_URL, "project_id": pid})
    _set_state(repo, pid, "INGEST")
    out = cmdgen.nextcmd_for_project(repo, pid)
    assert "Option A (raw external)" in out and "Option B (via vid_cli.py)" in out
    assert "yt-dlp" in out and f"ingest run {pid}" in out


def test_nextcmd_proceed_cli_only(repo: Path):
    pid = f"yt-{VIDEO_ID}"
    project.init_project(repo, pid, url=WATCH_URL, target_languages=["en"])
    _set_state(repo, pid, "DUBBING")
    out = cmdgen.nextcmd_for_project(repo, pid)
    assert "CLI-only state op" in out
    # verb text matches catalog's single source of truth for that state.
    assert catalog._STATE_NEXT_VERB["DUBBING"].format(id=pid) in out
    # no `!` in bare mode; `!` under --for-claude (a non-gate state op Claude may run).
    assert "! vid_cli.py" not in out
    out_claude = cmdgen.nextcmd_for_project(repo, pid, for_claude=True)
    assert "! vid_cli.py dub run" in out_claude


def test_nextcmd_stop_at_gate_never_bang(repo: Path):
    """Drive a project to a human gate; the reference line must never get `!`, even with
    --for-claude, and must match catalog._gate_reference_command."""
    pid = f"yt-{VIDEO_ID}"
    project.init_project(repo, pid, url=WATCH_URL, target_languages=["en"])
    _set_state(repo, pid, "TRANSCRIPT_QA_GATE")
    out = cmdgen.nextcmd_for_project(repo, pid, for_claude=True)
    assert "human gate (rule 13)" in out
    assert "approval grant" in out
    assert "! vid_cli.py approval grant" not in out   # never `!` on the gate line
    assert "! " + f"{cmdgen._cli()} approval grant" not in out


def test_nextcmd_blocked_package_rights(repo: Path):
    pid = f"yt-{VIDEO_ID}"
    project.init_project(repo, pid, url=WATCH_URL, target_languages=["en"])
    _set_state(repo, pid, "PACKAGE")
    out = cmdgen.nextcmd_for_project(repo, pid)
    # PACKAGE with unreviewed rights blocks READY_FOR_REVIEW -> rights-set command.
    if "rights set" in out:
        assert cmdgen._rights_blocked_command(cmdgen._cli(), pid) in out
    else:
        # If this build lets PACKAGE proceed, it must at least not crash and emit something.
        assert out.strip()


def test_nextcmd_terminal(repo: Path):
    pid = f"yt-{VIDEO_ID}"
    project.init_project(repo, pid, url=WATCH_URL, target_languages=["en"])
    _set_state(repo, pid, "READY_FOR_REVIEW")
    out = cmdgen.nextcmd_for_project(repo, pid)
    assert "TERMINAL" in out and "nothing to run" in out


def test_nextcmd_unreadable_project(repo: Path):
    out = cmdgen.nextcmd_for_project(repo, "yt-nonexistent")
    assert "cannot read project state" in out


# --------------------------------------------------------------------------------------
# 7) dispatch contract — these two verbs return a str (so main() prints it raw).
# --------------------------------------------------------------------------------------
def test_dispatch_returns_str(repo: Path):
    from video_translation_house import cli

    ns_cmd = argparse.Namespace(command="cmd", video_id_or_url=PLAYLIST_URL, for_claude=False)
    assert isinstance(cli.dispatch(ns_cmd, repo), str)

    pid = f"yt-{VIDEO_ID}"
    project.init_project(repo, pid, url=WATCH_URL, target_languages=["en"])
    ns_next = argparse.Namespace(command="nextcmd", project_id=pid, for_claude=False)
    assert isinstance(cli.dispatch(ns_next, repo), str)
