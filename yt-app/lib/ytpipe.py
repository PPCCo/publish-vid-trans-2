"""Single-video pipeline sequencer: download → extract → transcribe → translate → dub → mux.

Drives one video end to end over plain files, updating its ``status.json`` after every stage
so an interrupted run resumes exactly where it left off (a ``done`` stage with a present
artifact is skipped). No state machine, no gates, no approvals — this is the non-Claude route.

Output layout under ``<out_root>/<video_id>/``::

    source/audio.wav            source/<video>.<ext>   source/info.json
    captions/<lang>.json        captions/<lang>.srt    captions/<lang>.vtt
    audio/dub.<lang>.wav        audio/freeze-plan.<lang>.json   audio/sync.<lang>.json
    video/dubbed.<lang>.mp4
    status.json

The dub and mux stages take a caller-supplied ``dub_lock`` (a ``threading.Semaphore(1)`` owned
by the batch runner) and hold it around the ENTIRE render — concurrent ffmpeg dubs exhaust FDs
and die (plan §3). Transcribe/translate hold no lock.

Framework imports stay lazy; ``env.bootstrap()`` must have run before any stage.
"""
from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path
from typing import Any

from . import dubber, muxer, status as status_mod, transcriber, translator
from .langspec import LangSpec, build_langspec

# A no-op context manager for the (unusual) single-threaded case with no shared lock.
_NULL_LOCK = contextlib.nullcontext()


def _now() -> str:
    from video_translation_house.util import utc_now  # lazy

    return utc_now()


def _write_json(path: Path, doc: Any) -> Path:
    from video_translation_house.util import atomic_write_json  # lazy

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, doc)
    return path


def _render_sidecar_captions(caption_doc: dict[str, Any], dest_dir: Path, language: str) -> None:
    """Write standalone ``captions.<lang>.srt`` and ``.vtt`` beside the JSON."""
    from video_translation_house.captions import render as render_captions  # lazy
    from video_translation_house.util import atomic_write_text  # lazy

    dest_dir.mkdir(parents=True, exist_ok=True)
    for fmt in ("srt", "vtt"):
        text = render_captions(caption_doc, fmt)
        atomic_write_text(dest_dir / f"captions.{language}.{fmt}",
                          text if text.endswith("\n") else text + "\n")


def _source_video_path(source_dir: Path) -> Path | None:
    for child in sorted(source_dir.glob("*")):
        if child.suffix.lower() in {".mp4", ".mkv", ".webm"}:
            return child
    return None


class VideoPipeline:
    """Runs one video's stages against an ``out_root/<id>/`` directory."""

    def __init__(
        self,
        root: Path,
        cfg: dict[str, Any],
        *,
        out_root: Path | str,
        dub_lock: "threading.Semaphore | None" = None,
        log=print,
    ) -> None:
        self.root = root
        self.cfg = cfg
        self.video_id = str(cfg.get("id") or "").strip()
        self.url = cfg.get("url")
        if not self.video_id:
            raise ValueError("pipeline config needs an 'id' (video id)")
        self.dir = Path(out_root).expanduser().resolve() / self.video_id
        self.source_dir = self.dir / "source"
        self.captions_dir = self.dir / "captions"
        self.audio_dir = self.dir / "audio"
        self.video_dir = self.dir / "video"
        self.dub_lock = dub_lock
        self._log = log
        # When True, keep the declared source language even if ASR detects a different one
        # (skips the post-transcribe LanguageMismatchError). Set per-video via cfg["force"].
        self.force = bool(cfg.get("force"))
        # Validated language plan (fails fast on ur:xtts etc., before any download).
        self.spec: LangSpec = build_langspec(cfg, root)

    # -- helpers ------------------------------------------------------------
    def _lock(self):
        return self.dub_lock if self.dub_lock is not None else _NULL_LOCK

    def log(self, msg: str) -> None:
        self._log(f"[{self.video_id}] {msg}")

    def _load_status(self) -> dict[str, Any]:
        return status_mod.load_status(self.dir, self.video_id, url=self.url)

    def _save(self, doc: dict[str, Any]) -> None:
        status_mod.save_status(self.dir, doc)

    def _caption_doc(self, language: str) -> dict[str, Any]:
        return json.loads((self.captions_dir / f"{language}.json").read_text(encoding="utf-8"))

    # -- stages -------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        """Run every stage in order, resuming from status.json. Returns the final status doc."""
        for note in self.spec.notes:
            self.log(f"note: {note}")
        doc = self._load_status()
        self._stage_download(doc)
        self._stage_extract_audio(doc)
        self._stage_transcribe(doc)
        self._stage_translate(doc)
        self._stage_dub(doc)
        self._stage_mux(doc)
        self.log("done")
        return doc

    def _stage_download(self, doc: dict[str, Any]) -> None:
        if not status_mod.should_run(doc, "download"):
            self.log("download: skip (done)")
            return
        from .downloader import download_video, normalize_url

        status_mod.mark_running(doc, "download", now=_now()); self._save(doc)
        try:
            self.source_dir.mkdir(parents=True, exist_ok=True)
            url = normalize_url(self.url or self.video_id)
            result = download_video(url, self.source_dir)
            video_path = result.get("video_path")
            # Persist the info json for provenance.
            if result.get("info"):
                _write_json(self.source_dir / "info.json", result["info"])
            status_mod.mark_done(doc, "download", artifact=str(video_path), now=_now())
            self.log(f"download: {Path(video_path).name}")
        except Exception as exc:  # noqa: BLE001 - record + re-raise so batch marks the video failed
            status_mod.mark_failed(doc, "download", error=repr(exc), now=_now()); self._save(doc)
            raise
        self._save(doc)

    def _stage_extract_audio(self, doc: dict[str, Any]) -> None:
        if not status_mod.should_run(doc, "extract_audio"):
            self.log("extract_audio: skip (done)")
            return
        status_mod.mark_running(doc, "extract_audio", now=_now()); self._save(doc)
        try:
            src_video = _source_video_path(self.source_dir)
            if src_video is None:
                raise FileNotFoundError("no source video to extract audio from")
            audio_wav = self.source_dir / "audio.wav"
            transcriber.extract_audio(src_video, audio_wav)
            status_mod.mark_done(doc, "extract_audio", artifact=str(audio_wav), now=_now())
            self.log("extract_audio: audio.wav")
        except Exception as exc:  # noqa: BLE001
            status_mod.mark_failed(doc, "extract_audio", error=repr(exc), now=_now()); self._save(doc)
            raise
        self._save(doc)

    def _stage_transcribe(self, doc: dict[str, Any]) -> None:
        if not status_mod.should_run(doc, "transcribe"):
            self.log("transcribe: skip (done)")
            return
        status_mod.mark_running(doc, "transcribe", now=_now()); self._save(doc)
        try:
            audio_wav = self.source_dir / "audio.wav"
            asr = self.cfg.get("asr") or {}
            src_doc = transcriber.transcribe(
                audio_wav, self.source_dir,
                language=self.spec.source_lang, root=self.root,
                provider=asr.get("provider"), model=asr.get("model"),
                max_cue_ms=asr.get("max_cue_ms"), force=self.force,
            )
            detected = src_doc.get("detected_language")
            if src_doc.get("language_mismatch_forced"):
                self.log(f"transcribe: WARNING detected '{detected}' != declared "
                         f"'{self.spec.source_lang}' — kept declared (--force)")
            # The source-language caption doc is one of the produced captions (verbatim).
            src_json = self.captions_dir / f"{self.spec.source_lang}.json"
            _write_json(src_json, src_doc)
            _render_sidecar_captions(src_doc, self.captions_dir, self.spec.source_lang)
            status_mod.mark_done(doc, "transcribe", artifact=str(src_json), now=_now())
            self.log(f"transcribe: {len(src_doc.get('cues', []))} cues ({self.spec.source_lang})")
        except Exception as exc:  # noqa: BLE001
            status_mod.mark_failed(doc, "transcribe", error=repr(exc), now=_now()); self._save(doc)
            raise
        self._save(doc)

    def _stage_translate(self, doc: dict[str, Any]) -> None:
        # The source language's verbatim captions are produced by the transcribe stage; the
        # translate stage fills every OTHER caption language. Each language resumes independently.
        src_doc = self._caption_doc(self.spec.source_lang)
        parallel = int(self.cfg.get("translate_parallel", 4))
        provider = (self.cfg.get("translation") or {}).get("provider") or self.cfg.get("mt_provider")
        for language in self.spec.caption_langs:
            if language == self.spec.source_lang:
                continue
            if not status_mod.should_run(doc, "translate", language=language):
                self.log(f"translate[{language}]: skip (done)")
                continue
            status_mod.mark_running(doc, "translate", language=language, now=_now()); self._save(doc)
            try:
                docs = translator.translate_all(
                    src_doc, src_lang=self.spec.source_lang, caption_langs=[language],
                    root=self.root, provider=provider, parallel=parallel,
                )
                cap = docs[language]
                out = self.captions_dir / f"{language}.json"
                _write_json(out, cap)
                _render_sidecar_captions(cap, self.captions_dir, language)
                status_mod.mark_done(doc, "translate", language=language, artifact=str(out), now=_now())
                self.log(f"translate[{language}]: {len(cap.get('cues', []))} cues")
            except Exception as exc:  # noqa: BLE001
                status_mod.mark_failed(doc, "translate", language=language, error=repr(exc), now=_now())
                self._save(doc)
                raise
            self._save(doc)

    def _stage_dub(self, doc: dict[str, Any]) -> None:
        images = self.cfg.get("images") or {}
        clone_trim = int((self.cfg.get("xtts_worker") or {}).get("clone_ref_trim_seconds", 25))
        audio_wav = self.source_dir / "audio.wav"
        src_video = _source_video_path(self.source_dir)
        for language in self.spec.dub_langs:
            if not status_mod.should_run(doc, "dub", language=language):
                self.log(f"dub[{language}]: skip (done)")
                continue
            status_mod.mark_running(doc, "dub", language=language, now=_now()); self._save(doc)
            try:
                caption_doc = self._caption_doc(language)
                dub_wav = self.audio_dir / f"dub.{language}.wav"
                # Hold the global dub lock around the ENTIRE render (plan §3).
                with self._lock():
                    result = dubber.run_dub(
                        self.root, language=language, caption_doc=caption_doc, dub_wav=dub_wav,
                        is_clone=self.spec.is_clone(language),
                        source_audio=audio_wav if audio_wav.is_file() else None,
                        source_video=src_video,
                        still_image=images.get(language),
                        provider=self.spec.providers.get(language),
                        clone_ref_trim_seconds=clone_trim,
                    )
                if result.get("freeze_plan") is not None:
                    _write_json(self.audio_dir / f"freeze-plan.{language}.json", result["freeze_plan"])
                _write_json(self.audio_dir / f"sync.{language}.json", result["sync"])
                sync = result["sync"]
                if sync.get("silent_span_over_bar"):
                    self.log(f"dub[{language}]: WARNING max silent span "
                             f"{sync.get('max_silent_span_ms')}ms exceeds the bar")
                status_mod.mark_done(doc, "dub", language=language, artifact=result["dub"], now=_now())
                self.log(f"dub[{language}]: dub.{language}.wav")
            except Exception as exc:  # noqa: BLE001
                status_mod.mark_failed(doc, "dub", language=language, error=repr(exc), now=_now())
                self._save(doc)
                raise
            self._save(doc)

    def _stage_mux(self, doc: dict[str, Any]) -> None:
        images = self.cfg.get("images") or {}
        speeds = self.cfg.get("playback_speed") or {}
        mode = self.cfg.get("mux_mode") or "soft-subs"
        src_video = _source_video_path(self.source_dir)
        for language in self.spec.dub_langs:
            if not status_mod.should_run(doc, "mux", language=language):
                self.log(f"mux[{language}]: skip (done)")
                continue
            status_mod.mark_running(doc, "mux", language=language, now=_now()); self._save(doc)
            try:
                caption_doc = self._caption_doc(language)
                dub_wav = self.audio_dir / f"dub.{language}.wav"
                freeze_plan = self._load_freeze_plan(language)
                dst = self.video_dir / f"dubbed.{language}.mp4"
                try:
                    speed = float(speeds.get(language, 1.0))
                except (TypeError, ValueError):
                    speed = 1.0
                with self._lock():
                    result = muxer.run_mux(
                        self.root, language=language, dub_wav=dub_wav, dest_mp4=dst,
                        source_video=src_video, still_image=images.get(language),
                        freeze_plan=freeze_plan, caption_doc=caption_doc,
                        mode=mode, playback_speed=speed,
                    )
                status_mod.mark_done(doc, "mux", language=language, artifact=result["dubbed_video"], now=_now())
                self.log(f"mux[{language}]: dubbed.{language}.mp4"
                         f"{' (retimed)' if result.get('retimed') else ''}")
            except Exception as exc:  # noqa: BLE001
                status_mod.mark_failed(doc, "mux", language=language, error=repr(exc), now=_now())
                self._save(doc)
                raise
            self._save(doc)

    def _load_freeze_plan(self, language: str) -> dict[str, Any] | None:
        path = self.audio_dir / f"freeze-plan.{language}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None


def run_video(
    root: Path,
    cfg: dict[str, Any],
    *,
    out_root: Path | str,
    dub_lock: "threading.Semaphore | None" = None,
    log=print,
) -> dict[str, Any]:
    """Convenience wrapper: build a ``VideoPipeline`` and run it."""
    return VideoPipeline(root, cfg, out_root=out_root, dub_lock=dub_lock, log=log).run()
