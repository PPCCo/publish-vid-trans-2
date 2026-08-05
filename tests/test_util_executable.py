"""Regression tests for util.executable() venv-bin fallback.

The CLI is run as `.venv/bin/python3 …` without activating the venv, so the venv's bin/
dir is not on PATH. Tools pip-installed into that venv (yt-dlp, kokoro, faster-whisper, …)
must still be discoverable, or `doctor` and `ingest` wrongly report them "not installed".
The venv's python3 is a symlink to the base interpreter, so the resolver must NOT follow
that symlink when deriving the bin dir.
"""
from __future__ import annotations

import os
import sys

from video_translation_house.util import executable


def test_resolves_tool_next_to_interpreter_not_on_path(tmp_path, monkeypatch):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    interpreter = fake_bin / ("python.exe" if os.name == "nt" else "python3")
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)

    tool_name = "faketool"
    tool = fake_bin / (f"{tool_name}.exe" if os.name == "nt" else tool_name)
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)

    # PATH deliberately excludes fake_bin, so only the sys.executable fallback can find it.
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setattr(sys, "prefix", str(tmp_path))

    assert executable(tool_name) == str(tool)


def test_missing_tool_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "bin" / "python3"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert executable("definitely-not-a-real-binary-xyz") is None


def test_path_takes_precedence(tmp_path, monkeypatch):
    path_bin = tmp_path / "pathbin"
    path_bin.mkdir()
    name = "ffprobe-ish"
    on_path = path_bin / (f"{name}.exe" if os.name == "nt" else name)
    on_path.write_text("#!/bin/sh\n")
    on_path.chmod(0o755)
    monkeypatch.setenv("PATH", str(path_bin))
    assert executable(name) == str(on_path)
