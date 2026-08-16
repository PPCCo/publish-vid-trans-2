"""Resumable multi-video batch runner.

Runs a list of videos concurrently through ``ytpipe.VideoPipeline``, up to
``parallel_executions`` at once (default 3), each resuming from its own ``status.json``.

**The one shared serialization point is ``DUB_LOCK``** — a module-global
``threading.Semaphore(1)`` handed to every pipeline. The pipeline holds it around the ENTIRE
dub render AND the ENTIRE mux render, so at most one ffmpeg-heavy dub/mux runs across the whole
batch at any instant (concurrent ffmpeg dubs exhaust file descriptors and die on the banner —
rc 232, surfaced as a misleading "concat failed (232)"; verified 2026-08-07, plan §3). Every
OTHER stage — download, extract, transcribe, translate — runs freely up to the pool width, so
the batch still parallelizes the token/network-heavy work while serializing the FD-heavy work.

Input is either a single bare id/url (single-video mode, uses ``defaults.json`` verbatim) or a
``--list`` project file (``{"videos": [{"id","url","overrides"?}, …]}``); each entry's
``overrides`` deep-merge over the defaults (``config.effective_config``).

A per-video failure is isolated: it is recorded in that video's ``status.json`` and reported in
the batch summary, but never aborts the other videos.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import ytpipe

# --- the ONE cross-video serialization point (see module docstring) --------------------
DUB_LOCK = threading.Semaphore(1)

# Default output root for produced videos, relative to the yt-app package.
_DEFAULT_OUT_ROOT = Path(__file__).resolve().parent.parent / "outputs"


def _entries_from_list(list_path: Path | str) -> list[dict[str, Any]]:
    """Read a project-list JSON into a list of ``{id,url,overrides}`` entries."""
    from video_translation_house.util import load_json  # lazy

    doc = load_json(Path(list_path).expanduser().resolve())
    videos = doc.get("videos") if isinstance(doc, dict) else None
    if not isinstance(videos, list) or not videos:
        raise ValueError(f"{list_path}: expected a non-empty 'videos' array")
    return [dict(v) for v in videos]


def _entry_from_ref(ref: str) -> dict[str, Any]:
    """A bare id or url → a single-video entry (defaults applied, no overrides).

    We derive the id from a URL where possible so the output dir is stable; the framework's
    ``normalize_youtube_url`` + yt-dlp will still resolve the real id at download time.
    """
    ref = str(ref).strip()
    # A youtube watch URL carries ?v=<id>; a bare id has no scheme/slash.
    vid = ref
    if "://" in ref or "/" in ref:
        from urllib.parse import parse_qs, urlparse  # lazy stdlib

        parsed = urlparse(ref)
        qs = parse_qs(parsed.query)
        if qs.get("v"):
            vid = qs["v"][0]
        elif parsed.path:
            vid = parsed.path.rstrip("/").rsplit("/", 1)[-1] or ref
    return {"id": vid, "url": ref}


def build_entries(
    *, ref: str | None = None, list_path: str | None = None
) -> list[dict[str, Any]]:
    """Resolve the batch input into entries. Exactly one of ``ref`` / ``list_path``."""
    if bool(ref) == bool(list_path):
        raise ValueError("pass exactly one of a bare id/url OR --list <file>")
    if list_path:
        return _entries_from_list(list_path)
    return [_entry_from_ref(ref)]  # type: ignore[list-item]


def run_batch(
    root: Path,
    *,
    ref: str | None = None,
    list_path: str | None = None,
    out_root: Path | str | None = None,
    parallel: int | None = None,
    log=print,
) -> dict[str, Any]:
    """Run one or many videos concurrently; return a summary dict.

    ``parallel`` overrides the config's ``parallel_executions`` (pool width). The dub/mux
    serialization is independent of pool width — it is always at most one via ``DUB_LOCK``.
    Returns ``{"total", "succeeded": [ids], "failed": [{"id","error"}], "results": {id: status}}``.
    """
    defaults = config_mod.load_defaults()
    entries = build_entries(ref=ref, list_path=list_path)
    configs = [config_mod.effective_config(defaults, e) for e in entries]

    out = Path(out_root).expanduser().resolve() if out_root else _DEFAULT_OUT_ROOT
    width = int(parallel if parallel is not None else defaults.get("parallel_executions", 3))
    width = max(1, width)

    log(f"batch: {len(configs)} video(s), pool={width}, dub/mux serialized (1 at a time)")

    results: dict[str, Any] = {}
    succeeded: list[str] = []
    failed: list[dict[str, str]] = []

    def _run_one(cfg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        vid = str(cfg.get("id") or cfg.get("url") or "?")
        status = ytpipe.run_video(root, cfg, out_root=out, dub_lock=DUB_LOCK, log=log)
        return vid, status

    with ThreadPoolExecutor(max_workers=width) as pool:
        futures = {pool.submit(_run_one, cfg): cfg for cfg in configs}
        for fut in as_completed(futures):
            cfg = futures[fut]
            vid = str(cfg.get("id") or cfg.get("url") or "?")
            try:
                vid, status = fut.result()
                results[vid] = status
                succeeded.append(vid)
            except Exception as exc:  # noqa: BLE001 - isolate per-video failure
                failed.append({"id": vid, "error": repr(exc)})
                log(f"[{vid}] FAILED: {exc!r}")

    log(f"batch done: {len(succeeded)} ok, {len(failed)} failed")
    return {
        "total": len(configs),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }
