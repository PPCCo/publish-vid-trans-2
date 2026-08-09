"""Reconcile human verse-override edits into the English review-gloss worksheet.

At the TRANSCRIPT_QA_GATE the framework produces an English review gloss of the source speech
(``english_gloss.py``). For a Quran/tafsir source, a companion **review aid** may exist at
``transcript/english-verses-gloss.worksheet.json`` — it lists ONLY the verse-bearing gloss cues,
each with the verse citation(s), the source text, my English rendering (``translated_text``,
identical to that cue's ``target_text`` in the gloss worksheet) and an empty
``modified_translation`` the human fills ONLY for renderings they want to change.

This module copies those human overrides back into the gloss worksheet. It is designed to be:

  * **runnable standalone** — ``vid_cli.py transcript reconcile-verses <id>`` — so the human can
    apply their edits and inspect the result before importing; and
  * **the FIRST action of ``transcript english-import``** — so a plain import always picks up the
    latest overrides with no extra step.

Rule-1 clean: both files it touches (``english-gloss.worksheet.json`` and
``english-verses-gloss.worksheet.json``) are **fill-in artifacts**, NOT CLI-owned state and NOT
registered artifacts (only the canonical ``english-gloss.json`` that ``english-import`` builds is
registered/hashed). This module never touches ``state.json``/manifest/approvals; it only rewrites
worksheet text and appends an event.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .events import append_event
from .paths import ProjectPaths
from .util import atomic_write_json, load_json, project_lock

VERSES_WORKSHEET = "english-verses-gloss.worksheet.json"
GLOSS_WORKSHEET = "english-gloss.worksheet.json"


def _rel(path: Path, paths: ProjectPaths) -> str:
    return path.resolve().relative_to(paths.directory.resolve()).as_posix()


def _verses_path(paths: ProjectPaths) -> Path:
    return paths.transcript_dir / VERSES_WORKSHEET


def _gloss_worksheet_path(paths: ProjectPaths) -> Path:
    return paths.transcript_dir / GLOSS_WORKSHEET


def reconcile_verses_into_gloss(
    root: Path,
    project_id: str,
    *,
    actor: str = "agent",
) -> dict[str, Any]:
    """Merge non-empty ``modified_translation`` overrides into the gloss worksheet.

    For every cue in ``english-verses-gloss.worksheet.json`` whose ``modified_translation`` is a
    non-empty string, set the matching cue's ``target_text`` in ``english-gloss.worksheet.json``
    (by cue id) and mirror the override back into that cue's ``translated_text`` in the verses file
    so the two stay in sync. Idempotent. No-op when the verses review file is absent or holds no
    overrides — so ``english-import`` still works on projects that never made one.

    Returns ``{applied, skipped_missing_cue, worksheet, verses_file, note?}``.
    """
    paths = ProjectPaths(root, project_id).require()
    verses_src = _verses_path(paths)
    if not verses_src.exists():
        return {"applied": [], "skipped_missing_cue": [], "note": "no verses review file"}

    verses = load_json(verses_src)
    overrides: dict[Any, str] = {}
    for cue in verses.get("cues", []) or []:
        mod = cue.get("modified_translation")
        if isinstance(mod, str) and mod.strip():
            overrides[cue["id"]] = mod.strip()
    if not overrides:
        return {
            "applied": [],
            "skipped_missing_cue": [],
            "verses_file": _rel(verses_src, paths),
            "note": "no modified_translation overrides to apply",
        }

    gloss_src = _gloss_worksheet_path(paths)
    if not gloss_src.exists():
        return {
            "applied": [],
            "skipped_missing_cue": sorted(overrides, key=str),
            "verses_file": _rel(verses_src, paths),
            "note": "gloss worksheet not found — run `transcript english-export` first",
        }
    gloss = load_json(gloss_src)

    gloss_by_id = {c["id"]: c for c in gloss.get("cues", []) or []}
    verses_by_id = {c["id"]: c for c in verses.get("cues", []) or []}

    applied: list[Any] = []
    skipped: list[Any] = []
    for cid, text in overrides.items():
        gcue = gloss_by_id.get(cid)
        if gcue is None:
            skipped.append(cid)
            continue
        gcue["target_text"] = text
        # keep the review file's translated_text in sync (leave modified_translation as-is)
        vcue = verses_by_id.get(cid)
        if vcue is not None:
            vcue["translated_text"] = text
        applied.append(cid)

    if applied:
        with project_lock(paths.lock):
            atomic_write_json(gloss_src, gloss)
            atomic_write_json(verses_src, verses)
        append_event(paths.events, project_id, "ENGLISH_GLOSS_VERSES_RECONCILED", actor, {
            "applied": sorted(applied, key=str),
            "skipped_missing_cue": sorted(skipped, key=str),
        })

    return {
        "applied": sorted(applied, key=str),
        "skipped_missing_cue": sorted(skipped, key=str),
        "worksheet": _rel(gloss_src, paths),
        "verses_file": _rel(verses_src, paths),
    }
