"""Per-video ``status.json`` — the sidecar that makes a batch run resumable.

Independent of the framework's ``state.json`` (yt-app deliberately bypasses the state
machine). One file per video under the video's output dir, written atomically (tmp +
``os.replace``) so an interrupted write never leaves a half-file behind.

Stage model (plan §9): six stages, three of them language-scoped.

    stages: {
      download:      {status, started_at, finished_at, artifact, error},
      extract_audio: {status, ...},
      transcribe:    {status, ..., artifact},
      translate:     {status, per_language: {<lang>: {status, ..., artifact}}},
      dub:           {status, per_language: {<lang>: {...}}},
      mux:           {status, per_language: {<lang>: {...}}},
    }

``status`` ∈ ``pending | running | done | failed``.

Resume rule (``should_run``): run a stage/lang UNLESS it is ``done`` AND its recorded
artifact exists non-empty on disk. A ``running`` found at load (a crash mid-stage) is
treated as ``pending`` and re-runs. Partial artifacts are never deleted here — a re-run
overwrites them. All timestamps come from the CALLER (``now``) because the framework's
clock helper is fine here, but keeping it a parameter makes the logic pure + testable.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

STAGES = ("download", "extract_audio", "transcribe", "translate", "dub", "mux")
LANG_SCOPED = ("translate", "dub", "mux")

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

SCHEMA_VERSION = "1.0"


def _blank_stage(scoped: bool) -> dict[str, Any]:
    base: dict[str, Any] = {"status": PENDING, "started_at": None,
                            "finished_at": None, "artifact": None, "error": None}
    if scoped:
        base["per_language"] = {}
    return base


def new_status(video_id: str, *, url: str | None = None) -> dict[str, Any]:
    """A fresh status doc with every stage pending."""
    return {
        "schema_version": SCHEMA_VERSION,
        "video_id": video_id,
        "url": url,
        "stages": {name: _blank_stage(name in LANG_SCOPED) for name in STAGES},
    }


def status_path(video_dir: Path | str) -> Path:
    return Path(video_dir).expanduser().resolve() / "status.json"


def _coerce_running_to_pending(doc: dict[str, Any]) -> dict[str, Any]:
    """A ``running`` stage/lang at load time is a crash artifact → treat as pending."""
    for name, stage in doc.get("stages", {}).items():
        if stage.get("status") == RUNNING:
            stage["status"] = PENDING
        for lang_entry in (stage.get("per_language") or {}).values():
            if lang_entry.get("status") == RUNNING:
                lang_entry["status"] = PENDING
    return doc


def load_status(video_dir: Path | str, video_id: str, *, url: str | None = None) -> dict[str, Any]:
    """Load the video's status doc, or a fresh one if none exists. ``running`` → ``pending``."""
    path = status_path(video_dir)
    if not path.is_file():
        return new_status(video_id, url=url)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt/truncated status is safer to discard than to trust — every stage re-checks
        # its artifact on disk anyway, so a fresh doc simply re-verifies what's already done.
        return new_status(video_id, url=url)
    # Backfill any stages a newer schema added.
    doc.setdefault("stages", {})
    for name in STAGES:
        doc["stages"].setdefault(name, _blank_stage(name in LANG_SCOPED))
        if name in LANG_SCOPED:
            doc["stages"][name].setdefault("per_language", {})
    return _coerce_running_to_pending(doc)


def save_status(video_dir: Path | str, doc: dict[str, Any]) -> Path:
    """Atomically write the status doc (tmp file in the same dir + ``os.replace``)."""
    path = status_path(video_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".status-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def _artifact_ok(artifact: Any) -> bool:
    """An artifact is 'present' if the path exists and is a non-empty file."""
    if not artifact:
        return False
    p = Path(str(artifact))
    return p.is_file() and p.stat().st_size > 0


def _entry(doc: dict[str, Any], stage: str, language: str | None) -> dict[str, Any] | None:
    st = doc.get("stages", {}).get(stage)
    if st is None:
        return None
    if language is None:
        return st
    return (st.get("per_language") or {}).get(language)


def should_run(doc: dict[str, Any], stage: str, *, language: str | None = None) -> bool:
    """True unless the stage/lang is ``done`` with a present non-empty artifact.

    A stage with no artifact concept (download/extract are the resumable ones with artifacts;
    a stage recorded ``done`` but ``artifact=None`` is treated as needing a re-run to be safe —
    every real stage here records an artifact).
    """
    entry = _entry(doc, stage, language)
    if entry is None:
        return True
    if entry.get("status") != DONE:
        return True
    return not _artifact_ok(entry.get("artifact"))


def _set(doc: dict[str, Any], stage: str, language: str | None, **fields: Any) -> dict[str, Any]:
    st = doc["stages"][stage]
    if language is None:
        st.update(fields)
        return st
    per = st.setdefault("per_language", {})
    entry = per.setdefault(language, _blank_stage(False))
    entry.update(fields)
    # A scoped parent stage is 'running' while any lang runs and 'done' when all its langs are done.
    return entry


def mark_running(doc: dict[str, Any], stage: str, *, language: str | None = None,
                 now: str | None = None) -> None:
    _set(doc, stage, language, status=RUNNING, started_at=now, finished_at=None, error=None)
    if language is not None:
        doc["stages"][stage]["status"] = RUNNING


def mark_done(doc: dict[str, Any], stage: str, *, language: str | None = None,
              artifact: str | None = None, now: str | None = None) -> None:
    _set(doc, stage, language, status=DONE, finished_at=now, artifact=artifact, error=None)
    if language is not None:
        _roll_up_scoped(doc, stage)


def mark_failed(doc: dict[str, Any], stage: str, *, language: str | None = None,
                error: str | None = None, now: str | None = None) -> None:
    _set(doc, stage, language, status=FAILED, finished_at=now, error=error)
    if language is not None:
        # A scoped parent stage is 'failed' if any of its langs failed.
        doc["stages"][stage]["status"] = FAILED


def _roll_up_scoped(doc: dict[str, Any], stage: str) -> None:
    """Set a language-scoped parent stage's status from its per-language children."""
    st = doc["stages"][stage]
    per = st.get("per_language") or {}
    if not per:
        return
    statuses = {e.get("status") for e in per.values()}
    if statuses == {DONE}:
        st["status"] = DONE
    elif FAILED in statuses:
        st["status"] = FAILED
    elif RUNNING in statuses:
        st["status"] = RUNNING
    else:
        st["status"] = PENDING
