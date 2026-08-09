"""Observability module: 24h-TTL rotating ndjson debug log + a background stderr heartbeat.

These test the log/heartbeat contract, not any pipeline behavior — obs is pure instrumentation
(no state/artifact/approval writes). Focus:
  * start/end records are written as valid ndjson;
  * size rotation creates a `.1` backup;
  * the 24h prune deletes an old-mtime file;
  * `obs.phase` / `obs.progress` are safe no-ops when no heartbeat is active;
  * the heartbeat renders a liveness line and never leaks onto stdout.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import obs  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_obs():
    """obs.get_logger is idempotent via a module singleton; reset it (and the current heartbeat)
    around every test so each gets a fresh logger bound to its own tmp root."""
    prev_logger = obs._logger
    prev_current = obs._current
    logger = logging.getLogger(obs._LOGGER_NAME)
    saved_handlers = list(logger.handlers)
    logger.handlers.clear()
    obs._logger = None
    obs._current = None
    try:
        yield
    finally:
        for h in list(logger.handlers):
            h.close()
        logger.handlers.clear()
        logger.handlers.extend(saved_handlers)
        obs._logger = prev_logger
        obs._current = prev_current


def _read_ndjson(path: Path) -> list[dict]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]  # asserts every line is valid JSON


def test_logs_path_under_claude_logs(tmp_path):
    assert obs.logs_path(tmp_path) == tmp_path / ".claude" / "logs" / "vid_cli.ndjson"


def test_start_end_records_written(tmp_path):
    obs.get_logger(tmp_path)
    obs.log_info("cli start", kind="start", cmd="dub run", project_id="vid_x")
    obs.log_info("cli done", kind="end", cmd="dub run", project_id="vid_x", rc=0)

    records = _read_ndjson(obs.logs_path(tmp_path))
    kinds = [r.get("kind") for r in records]
    assert "start" in kinds and "end" in kinds
    start = next(r for r in records if r.get("kind") == "start")
    assert start["cmd"] == "dub run"
    assert start["project_id"] == "vid_x"
    assert start["level"] == "INFO"
    assert start["msg"] == "cli start"
    # ISO-8601 UTC timestamp.
    assert start["ts"].endswith("Z") and "T" in start["ts"]


def test_log_helpers_noop_before_init(tmp_path):
    # No get_logger() call — helpers must be silent no-ops, not raise, and write nothing.
    obs.log_info("orphan", kind="start")
    obs.log_error("orphan err")
    assert not obs.logs_path(tmp_path).exists()


def test_error_record_level(tmp_path):
    obs.get_logger(tmp_path)
    obs.log_error("cli error", kind="end", error_type="ValueError", rc=2)
    rec = _read_ndjson(obs.logs_path(tmp_path))[-1]
    assert rec["level"] == "ERROR"
    assert rec["error_type"] == "ValueError"
    assert rec["rc"] == 2


def test_size_rotation_creates_backup(tmp_path, monkeypatch):
    # Shrink the rotation threshold so a handful of records forces a rollover.
    monkeypatch.setattr(obs, "_LOG_MAX_BYTES", 512)
    obs.get_logger(tmp_path)
    for i in range(200):
        obs.log_info("filler", kind="heartbeat", i=i, padding="x" * 40)
    active = obs.logs_path(tmp_path)
    backup = Path(str(active) + ".1")
    assert active.exists()
    assert backup.exists(), "size rotation should have created vid_cli.ndjson.1"


def test_24h_prune_deletes_old_file(tmp_path):
    directory = obs._log_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    stale = directory / "vid_cli.ndjson.9"
    stale.write_text('{"old": true}\n', encoding="utf-8")
    # Backdate it well past the 24h TTL.
    old = time.time() - (obs._LOG_TTL_SECONDS + 3600)
    os.utime(stale, (old, old))

    obs._prune_old_logs(directory)
    assert not stale.exists(), "prune should delete a backup older than the 24h TTL"


def test_24h_prune_keeps_fresh_file(tmp_path):
    directory = obs._log_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    fresh = directory / "vid_cli.ndjson.1"
    fresh.write_text('{"fresh": true}\n', encoding="utf-8")
    obs._prune_old_logs(directory)
    assert fresh.exists(), "a recently-written backup must survive the prune"


def test_phase_progress_noop_without_heartbeat():
    # No heartbeat installed — these must be safe no-ops (deep call sites call them unconditionally).
    assert obs.current() is None
    obs.phase("dubbing fa")          # must not raise
    obs.progress(3, 10, unit="cue")  # must not raise


def test_heartbeat_renders_progress_line():
    hb = obs.Heartbeat("dub run", "vid_x", interval=30.0)
    hb.set_phase("dubbing fa (synthesizing cues)")
    hb.set_progress(142, 430, unit="cue")
    line = hb._render()
    assert line.startswith("vid_cli")
    assert "dub run" in line and "vid_x" in line
    assert "cue 142/430" in line
    assert "elapsed" in line


def test_heartbeat_ticks_to_its_stream(tmp_path):
    import io
    obs.get_logger(tmp_path)  # so the mirror-to-log path is exercised too
    stream = io.StringIO()
    # The Heartbeat clamps interval to a 1.0s floor, so sleep past it to guarantee a tick.
    hb = obs.Heartbeat("transcript run", "vid_y", interval=1.0, stream=stream)
    obs.set_current(hb)
    hb.start()
    obs.phase("transcribing (mlx attempt 1/4)")
    time.sleep(1.3)  # allow at least one tick past the 1s floor
    hb.stop()
    obs.set_current(None)
    out = stream.getvalue()
    assert "vid_cli" in out
    assert "transcribing" in out


def test_heartbeat_enabled_env(monkeypatch):
    monkeypatch.delenv("VIDTRANS_NO_HEARTBEAT", raising=False)
    assert obs.heartbeat_enabled() is True
    monkeypatch.setenv("VIDTRANS_NO_HEARTBEAT", "1")
    assert obs.heartbeat_enabled() is False
    monkeypatch.setenv("VIDTRANS_NO_HEARTBEAT", "yes")
    assert obs.heartbeat_enabled() is False


def test_heartbeat_interval_env(monkeypatch):
    monkeypatch.delenv("VIDTRANS_HEARTBEAT_SECS", raising=False)
    assert obs.heartbeat_interval() == obs._DEFAULT_HEARTBEAT_SECS
    monkeypatch.setenv("VIDTRANS_HEARTBEAT_SECS", "7.5")
    assert obs.heartbeat_interval() == 7.5
    monkeypatch.setenv("VIDTRANS_HEARTBEAT_SECS", "not-a-number")
    assert obs.heartbeat_interval() == obs._DEFAULT_HEARTBEAT_SECS  # bad value falls back
