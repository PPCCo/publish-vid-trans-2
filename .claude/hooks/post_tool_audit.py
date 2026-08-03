#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for value in (str(HERE), str(HERE.parent / "scripts")):
    if value not in sys.path:
        sys.path.insert(0, value)

from hooklib import active_project, project_root, read_input


def main() -> int:
    from video_translation_house.events import append_event
    from video_translation_house.paths import ProjectPaths
    from video_translation_house.util import sha256_file

    payload = read_input()
    root = project_root(payload)
    tool = str(payload.get("tool_name") or "")
    if tool not in {"Write", "Edit", "NotebookEdit"}:
        return 0
    project_id = active_project(payload, root)
    if not project_id:
        return 0
    tool_input = payload.get("tool_input") or {}
    value = tool_input.get("file_path") or tool_input.get("path")
    if not value:
        return 0
    path = Path(str(value))
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        return 0
    try:
        paths = ProjectPaths(root, project_id).require()
        digest = sha256_file(path)
        append_event(
            paths.events, project_id, "CLAUDE_TOOL_USED", "agent",
            {"tool": tool, "path": str(path.relative_to(paths.directory)) if path.is_relative_to(paths.directory) else str(path), "sha256": digest},
        )
    except Exception:  # noqa: BLE001
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
