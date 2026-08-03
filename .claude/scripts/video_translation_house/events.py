from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .util import random_token, utc_now


def append_event(
    events_path: Path,
    project_id: str,
    event: str,
    actor: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = {
        "event_id": f"evt-{utc_now().replace(':', '').replace('-', '')}-{random_token(8)}",
        "time": utc_now(),
        "event": event,
        "project_id": project_id,
        "actor": actor,
        "details": details or {},
    }
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
    return record


def read_events(events_path: Path, tail: int | None = None) -> list[dict[str, Any]]:
    if not events_path.exists():
        return []
    events: list[dict[str, Any]] = []
    with events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    if tail is not None:
        return events[-tail:]
    return events
