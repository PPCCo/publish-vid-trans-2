"""Ingest orchestration: download -> extract WAV -> probe -> catalog -> register.

This is the only pipeline stage that pulls third-party content onto the machine, so it
is deliberately conservative:
  * All egress goes through net.fetch (VIDTRANS_FETCH_ENABLED gated).
  * The catalog entry and source metadata start rights_status="unreviewed"; nothing
    downstream may be packaged for distribution until a human sets rights (enforced in
    state.transition_blockers on PACKAGE -> READY_FOR_REVIEW).
  * Source video/audio are registered as content-addressed artifacts so every later
    stage can prove which bytes it consumed.

`ingest run` is idempotent: if the source video already exists on disk it is not
re-downloaded unless force=True.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import artifacts as artifacts_mod
from .catalog import upsert_entry
from .errors import ConfigurationError
from .events import append_event
from .langid import language_from_metadata
from .media import extract_wav, probe_summary
from .paths import ProjectPaths
from .state import transition
from .util import atomic_write_json, load_json, load_yaml, project_lock, utc_now
from .validation import require_valid


def _existing_source_video(paths: ProjectPaths) -> Path | None:
    for child in sorted(paths.source_dir.glob("*")):
        if child.suffix.lower() in {".mp4", ".mkv", ".webm"}:
            return child
    return None


def _write_source_metadata(paths: ProjectPaths, meta: dict[str, Any]) -> Path:
    require_valid_meta(paths.root, meta)
    dest = paths.source_dir / "metadata.json"
    atomic_write_json(dest, meta)
    return dest


def require_valid_meta(root: Path, meta: dict[str, Any]) -> None:
    require_valid(root, meta, "source-metadata.schema.json")


def ingest_project(
    root: Path,
    project_id: str,
    *,
    actor: str = "agent",
    force: bool = False,
    write_subs: bool = True,
    advance: bool = True,
) -> dict[str, Any]:
    """Run ingest for an already-initialized project (project.yaml holds the URL)."""
    paths = ProjectPaths(root, project_id).require()
    config = load_yaml(paths.config)
    url = (config.get("source") or {}).get("url")
    if not url:
        raise ConfigurationError(f"project {project_id} has no source.url in project.yaml")

    state = load_json(paths.state)
    if state["current_state"] != "INGEST":
        raise ConfigurationError(
            f"ingest expects state INGEST, project is at {state['current_state']}"
        )

    downloaded_at = None
    info: dict[str, Any] = {}
    video_path = _existing_source_video(paths)

    if video_path is None or force:
        from .net import ytdlp_download  # imported lazily so a no-fetch CLI never loads it

        result = ytdlp_download(url, paths.source_dir, write_subs=write_subs)
        info = result.get("info", {})
        video_path = Path(result["video_path"]) if result.get("video_path") else None
        downloaded_at = utc_now()
        if video_path is None:
            raise ConfigurationError("yt-dlp completed but produced no video file")
    else:
        # Reuse any previously fetched info.json for metadata on a re-run.
        info_files = sorted(paths.source_dir.glob("*.info.json"))
        if info_files:
            info = load_json(info_files[-1])

    # Extract normalized WAV for ASR.
    wav_path = paths.source_dir / "audio.wav"
    if not wav_path.exists() or force:
        extract_wav(video_path, wav_path)

    summary = probe_summary(video_path)
    metadata_lang, metadata_conf = language_from_metadata(info)
    subs = [s for s in (info.get("subtitles") or {})]

    meta = {
        "schema_version": "1.0",
        "project_id": project_id,
        "url": url,
        "title": info.get("title") or (config.get("source") or {}).get("title"),
        "channel": info.get("channel") or info.get("uploader")
                   or (config.get("source") or {}).get("channel"),
        "channel_id": info.get("channel_id"),
        "uploader": info.get("uploader"),
        "upload_date": info.get("upload_date"),
        "duration_seconds": summary.get("duration_seconds") or info.get("duration"),
        "detected_language": metadata_lang,
        "language_confidence": metadata_conf,
        "language_source": "youtube-metadata" if metadata_lang else None,
        "dialect": None,
        "has_youtube_captions": sorted(subs),
        "video": {"path": _rel(video_path, paths), **summary.get("video", {})},
        "audio": {"path": _rel(wav_path, paths), **summary.get("audio", {})},
        "probed_at": utc_now(),
        "downloaded_at": downloaded_at,
    }

    with project_lock(paths.lock):
        _write_source_metadata(paths, meta)

    # Register source artifacts (content-addressed provenance root of the whole project).
    video_artifact = artifacts_mod.register_artifact(
        root, project_id, video_path, "source-video", "INGEST", actor,
    )
    audio_artifact = artifacts_mod.register_artifact(
        root, project_id, wav_path, "source-audio", "INGEST", actor,
    )

    # Merge into the repo-global catalog (never silently overwrites).
    upsert_entry(root, {
        "video_id": project_id,
        "url": url,
        "title": meta["title"],
        "channel": meta["channel"],
        "channel_id": meta["channel_id"],
        "duration_seconds": meta["duration_seconds"],
        "upload_date": meta["upload_date"],
        "language": metadata_lang,
        "language_confidence": metadata_conf,
        "language_source": "youtube-metadata" if metadata_lang else None,
        "has_youtube_captions": sorted(subs),
        "downloaded_at": downloaded_at,
        "local_paths": {"video": _rel(video_path, paths), "audio": _rel(wav_path, paths)},
        "rights_status": "unreviewed",
        "project_id": project_id,
    }, actor=actor)

    append_event(paths.events, project_id, "INGEST_COMPLETED", actor, {
        "video_sha256": video_artifact["sha256"],
        "audio_sha256": audio_artifact["sha256"],
        "duration_seconds": meta["duration_seconds"],
        "metadata_language": metadata_lang,
    })

    advanced = False
    if advance:
        transition(root, project_id, "LANGUAGE_ID", actor, reason="ingest complete")
        advanced = True

    return {
        "project_id": project_id,
        "source_video": video_artifact,
        "source_audio": audio_artifact,
        "metadata": meta,
        "advanced_to": "LANGUAGE_ID" if advanced else "INGEST",
    }


def _rel(path: Path, paths: ProjectPaths) -> str:
    return path.resolve().relative_to(paths.directory.resolve()).as_posix()


def ensure_source_present(
    root: Path,
    project_id: str,
    *,
    actor: str = "agent",
    write_subs: bool = True,
) -> dict[str, Any]:
    """Guarantee the source video (and its WAV) exist on disk, re-fetching if deleted.

    Unlike ``ingest_project``, this is a media-restore, not a pipeline stage: it does NOT
    require the project to be at ``INGEST`` and never advances state. It exists so a later
    stage that actually needs the source bytes — ``package mux`` (packaging._source_video) or a
    ``dub run --clone`` reference — can transparently recover from a deleted ``source/`` dir
    (media is expensive to keep; a translate/dub-only re-run doesn't need it, but mux does).

    If the video is already on disk, this is a no-op (``restored: False``). Otherwise it
    re-downloads via the sanctioned, ``VIDTRANS_FETCH_ENABLED``-gated network module (raising
    ``FetchDisabled`` when egress is off), re-extracts the WAV, and re-registers the
    ``source-video``/``source-audio`` artifacts so provenance stays intact (mirrors ingest and
    project._preserved_source_files' re-registration).
    """
    paths = ProjectPaths(root, project_id).require()
    video_path = _existing_source_video(paths)
    wav_path = paths.source_dir / "audio.wav"
    if video_path is not None and wav_path.exists():
        return {"project_id": project_id, "restored": False,
                "video": _rel(video_path, paths), "audio": _rel(wav_path, paths)}

    config = load_yaml(paths.config)
    url = (config.get("source") or {}).get("url")
    if not url:
        raise ConfigurationError(f"project {project_id} has no source.url in project.yaml")

    if video_path is None:
        from .net import ytdlp_download  # lazy: a no-fetch CLI never loads it

        result = ytdlp_download(url, paths.source_dir, write_subs=write_subs)
        video_path = Path(result["video_path"]) if result.get("video_path") else None
        if video_path is None:
            raise ConfigurationError("yt-dlp completed but produced no video file")

    if not wav_path.exists():
        extract_wav(video_path, wav_path)

    video_artifact = artifacts_mod.register_artifact(
        root, project_id, video_path, "source-video", "INGEST", actor,
    )
    audio_artifact = artifacts_mod.register_artifact(
        root, project_id, wav_path, "source-audio", "INGEST", actor,
    )
    append_event(paths.events, project_id, "SOURCE_RESTORED", actor, {
        "video_sha256": video_artifact["sha256"],
        "audio_sha256": audio_artifact["sha256"],
    })
    return {"project_id": project_id, "restored": True,
            "video": _rel(video_path, paths), "audio": _rel(wav_path, paths),
            "source_video": video_artifact, "source_audio": audio_artifact}
