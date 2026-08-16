#!/usr/bin/env python3
"""yt-app — a standalone (non-Claude) A/V pipeline CLI.

Runs the download → split → transcribe → translate → dub → mux chain over plain files, with
NO state machine, gates, approvals, or event log. It imports the publish-vid-trans framework's
proven leaf functions in place (``lib/env.py::bootstrap`` wires ``sys.path`` first) and
thin-reimplements the two state-coupled orchestrators (dub, mux) as pure functions.

Deliberate deviations from framework policy (documented loudly in the user guide):
  * No ``voice_clone_consent`` gate — cloning defaults on for clone-languages (``--no-clone``
    to opt out). This is a local operator tool with no rights record.
  * ``fa`` has no staged piper voice, so a ``fa`` dub fails loud (as the framework's does).

Usage: ``run.sh <verb> [args]`` (or ``<repo>/.venv/bin/python3 cli.py <verb> [args]``).
Bare ``python3`` is hook-blocked and lacks the venv deps — always use ``run.sh``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The framework's leaf modules are only importable AFTER sys.path is wired. bootstrap() also
# self-configures the fetch flag / CA bundle / HF offline env. It MUST run before any
# `video_translation_house` import (all of ours are lazy, inside functions/verbs, to honor that).
from lib import env as _env

ROOT = _env.bootstrap()


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip().lower() for p in value.split(",") if p.strip()]


# --------------------------------------------------------------------------------------
# Verb handlers. Each takes the parsed args namespace and returns an int exit code.
# --------------------------------------------------------------------------------------
def cmd_split_channels(args) -> int:
    from lib import channels

    result = channels.split_channels(
        args.input, out_dir=args.out_dir, layout=args.layout,
        sample_rate=args.sr,
    )
    _print_json(result)
    return 0


def cmd_probe(args) -> int:
    from video_translation_house import media as media_mod

    _print_json(media_mod.probe_summary(Path(args.input).expanduser().resolve()))
    return 0


def cmd_extract_audio(args) -> int:
    from video_translation_house import media as media_mod

    src = Path(args.input).expanduser().resolve()
    dest = Path(args.out).expanduser().resolve() if args.out else src.with_suffix(".wav")
    media_mod.extract_wav(src, dest, sample_rate=args.sr, channels=args.ch)
    _print_json({"audio": str(dest), "sample_rate": args.sr, "channels": args.ch})
    return 0


def cmd_transcribe(args) -> int:
    from lib import transcriber

    audio = Path(args.audio).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else audio.parent
    doc = transcriber.transcribe(
        audio, out_dir,
        language=args.lang, root=ROOT,
        provider=args.provider, model=args.model, max_cue_ms=args.max_cue,
        condition_on_previous_text=not args.no_condition_on_previous_text,
        hallucination_silence_threshold=args.hallucination_silence_threshold,
    )
    dest = out_dir / f"{args.lang}.json"
    from video_translation_house.util import atomic_write_json

    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(dest, doc)
    _print_json({"transcript": str(dest), "language": args.lang, "cues": len(doc.get("cues", []))})
    return 0


def cmd_translate(args) -> int:
    from lib import translator
    from video_translation_house.util import atomic_write_json, load_json

    src_doc = load_json(Path(args.src).expanduser().resolve())
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else Path(args.src).resolve().parent
    langs = _csv(args.to) or [src_doc.get("language")]
    docs = translator.translate_all(
        src_doc, src_lang=args.src_lang or src_doc.get("language"),
        caption_langs=langs, root=ROOT,
        provider=args.provider, parallel=args.parallel,
    )
    written = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    for lang, doc in docs.items():
        path = out_dir / f"{lang}.json"
        atomic_write_json(path, doc)
        written[lang] = str(path)
    _print_json({"captions": written})
    return 0


def cmd_dub(args) -> int:
    """Dub one language of one video: render the dub, then mux it onto the picture."""
    from lib import dubber, muxer
    from video_translation_house.util import atomic_write_json, load_json

    caption_doc = load_json(Path(args.captions).expanduser().resolve())
    language = args.lang.strip().lower()
    video = Path(args.input).expanduser().resolve() if args.input else None
    work = Path(args.out_dir).expanduser().resolve() if args.out_dir else (
        video.parent if video else Path(args.captions).resolve().parent
    )
    work.mkdir(parents=True, exist_ok=True)

    # Clone resolution: explicit --clone/--no-clone wins; else clone iff lang is a company
    # clone-language (read live). --no-clone forces piper.
    from video_translation_house.dubbing import clone_languages

    if args.no_clone:
        is_clone = False
    elif args.clone:
        is_clone = True
    else:
        is_clone = language in set(clone_languages(ROOT))

    source_audio = None
    if is_clone or any("keep-source-audio" in (c.get("flags") or []) for c in caption_doc.get("cues", [])):
        # Need the source audio; extract it from the video if not already a wav.
        if video is not None:
            from video_translation_house import media as media_mod

            source_audio = work / "source-audio.wav"
            if not source_audio.is_file():
                media_mod.extract_wav(video, source_audio,
                                      sample_rate=media_mod.ASR_SAMPLE_RATE,
                                      channels=media_mod.ASR_CHANNELS)

    dub_wav = work / f"dub.{language}.wav"
    dub_result = dubber.run_dub(
        ROOT, language=language, caption_doc=caption_doc, dub_wav=dub_wav,
        is_clone=is_clone, source_audio=source_audio, source_video=video,
        still_image=args.still_image, provider=args.provider, gender=args.gender,
    )
    if dub_result.get("freeze_plan") is not None:
        atomic_write_json(work / f"freeze-plan.{language}.json", dub_result["freeze_plan"])
    atomic_write_json(work / f"sync.{language}.json", dub_result["sync"])

    dest = Path(args.out).expanduser().resolve() if args.out else work / f"dubbed.{language}.mp4"
    mux_result = muxer.run_mux(
        ROOT, language=language, dub_wav=dub_wav, dest_mp4=dest,
        source_video=video, still_image=args.still_image,
        freeze_plan=dub_result.get("freeze_plan"), caption_doc=caption_doc,
        mode=("burned-in" if args.burn else "soft-subs"),
        playback_speed=args.speed, work_dir=work,
    )
    _print_json({"dub": dub_result["dub"], "video": mux_result["dubbed_video"],
                 "clone": is_clone, "retimed": mux_result["retimed"],
                 "still_image": mux_result["still_image"]})
    return 0


def cmd_mux(args) -> int:
    """Mux a pre-rendered audio wav onto a video (no dub render, no freeze plan)."""
    from lib import muxer
    from video_translation_house.util import load_json

    video = Path(args.input).expanduser().resolve()
    audio = Path(args.audio).expanduser().resolve()
    dest = Path(args.out).expanduser().resolve() if args.out else video.with_name(f"{video.stem}.dubbed.mp4")
    caption_doc = load_json(Path(args.captions).expanduser().resolve()) if args.captions else None
    result = muxer.run_mux(
        ROOT, language=args.lang or "und", dub_wav=audio, dest_mp4=dest,
        source_video=video, caption_doc=caption_doc, subs_vtt=args.subs,
        mode=("burned-in" if args.burn else ("no-subs" if not (args.subs or caption_doc) else "soft-subs")),
        playback_speed=args.speed,
    )
    _print_json(result)
    return 0


def cmd_size(args) -> int:
    from video_translation_house.size import compute_dimensions

    _print_json(compute_dimensions(args.axis, args.value, aspect=args.aspect))
    return 0


def cmd_speed(args) -> int:
    from video_translation_house.speed import respeed

    result = respeed(ROOT, args.factor, Path(args.input).expanduser().resolve(),
                     dest=Path(args.out).expanduser().resolve() if args.out else None)
    _print_json(result)
    return 0


def cmd_playlist(args) -> int:
    from lib import downloader

    entries = downloader.playlist_entries(downloader.normalize_url(args.url))
    doc = {"schema_version": "1.0",
           "videos": [{"id": e.get("id"), "url": e.get("url")} for e in entries]}
    if args.out:
        from video_translation_house.util import atomic_write_json

        path = Path(args.out).expanduser().resolve()
        atomic_write_json(path, doc)
        _print_json({"list": str(path), "count": len(entries)})
    else:
        _print_json(doc)
    return 0


def cmd_batch(args) -> int:
    from lib import batch

    # Assemble per-video overrides from the single-ref CLI flags. Only keys the operator
    # actually passed are included, so unset flags fall through to defaults.json.
    overrides: dict = {}
    if args.source_lang:
        overrides["sourceLang"] = args.source_lang.strip().lower()
    if args.target_langs is not None:
        overrides["targetLangs"] = _csv(args.target_langs)
    if args.dub_langs is not None:
        overrides["dubLangs"] = _csv(args.dub_langs)
    if args.cc is not None:
        overrides["closedCaptions"] = _csv(args.cc)
    if args.force:
        overrides["force"] = True

    if overrides and not args.ref:
        print("error: --source-lang/--target-langs/--dub-langs/--cc/--force apply only to a "
              "single id/url (not --list); put overrides in the list file", file=sys.stderr)
        return 2

    # Footgun guard: overriding --source-lang while leaving targetLangs at the config default
    # can silently ask to translate INTO the old default source (e.g. --source-lang ur while
    # defaults still list fa as a target → a ur→fa track nobody asked for). Warn, don't block.
    if args.source_lang and args.target_langs is None:
        from lib import config as _cfg

        default_targets = _cfg.load_defaults().get("targetLangs") or []
        src = args.source_lang.strip().lower()
        stale = [t for t in default_targets if str(t).strip().lower() not in ("", src)]
        if stale:
            print(f"warning: --source-lang {src} set but --target-langs not given; the config "
                  f"default targets {stale} still apply (you'll translate {src}→{stale}). "
                  f"Pass --target-langs to change this.", file=sys.stderr)

    try:
        summary = batch.run_batch(
            ROOT, ref=args.ref, list_path=args.list,
            out_root=args.out_root, parallel=args.parallel,
            overrides=overrides or None,
        )
    except RuntimeError as exc:
        # Preflight failures (e.g. fetch flag off while a download is needed) are operator
        # config errors, not crashes — print the actionable message cleanly, no stack trace.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_json({k: summary[k] for k in ("total", "succeeded", "failed")})
    return 0 if not summary["failed"] else 1


# --------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="yt-app", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="verb", required=True)

    sp = sub.add_parser("split-channels", help="split an A/V file's audio channels")
    sp.add_argument("input")
    sp.add_argument("--out-dir", dest="out_dir")
    sp.add_argument("--layout", choices=["mono", "stereo", "all"], default="all")
    sp.add_argument("--sr", type=int, default=48000)
    sp.set_defaults(func=cmd_split_channels)

    sp = sub.add_parser("probe", help="ffprobe summary of a media file")
    sp.add_argument("input")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("extract-audio", help="extract a WAV from a media file")
    sp.add_argument("input")
    sp.add_argument("--out")
    sp.add_argument("--sr", type=int, default=16000)
    sp.add_argument("--ch", type=int, default=1)
    sp.set_defaults(func=cmd_extract_audio)

    sp = sub.add_parser("transcribe", help="ASR a WAV into a caption doc")
    sp.add_argument("audio")
    sp.add_argument("--out-dir", dest="out_dir")
    sp.add_argument("--provider")
    sp.add_argument("--model")
    sp.add_argument("--lang", required=True, help="spoken (source) language code")
    sp.add_argument("--max-cue", dest="max_cue", type=int, default=None,
                    help="max caption cue duration in ms (default: company bar)")
    sp.add_argument("--no-condition-on-previous-text", action="store_true",
                    help="disable ASR context carry (anti-repetition-loop)")
    sp.add_argument("--hallucination-silence-threshold", type=float, default=None)
    sp.set_defaults(func=cmd_transcribe)

    sp = sub.add_parser("translate", help="translate a source caption doc into target langs")
    sp.add_argument("src", help="source caption/transcript JSON")
    sp.add_argument("--from", dest="src_lang", help="source language (default: doc.language)")
    sp.add_argument("--to", required=True, help="comma-separated target language codes")
    sp.add_argument("--out-dir", dest="out_dir")
    sp.add_argument("--provider")
    sp.add_argument("--parallel", type=int, default=4)
    sp.set_defaults(func=cmd_translate)

    sp = sub.add_parser("dub", help="render + mux a dub for one language of one video")
    sp.add_argument("input", nargs="?", help="source video (omit only for a still-image dub)")
    sp.add_argument("--captions", required=True, help="caption doc JSON for the language")
    sp.add_argument("--lang", required=True)
    sp.add_argument("--out", help="output mp4 (default: <workdir>/dubbed.<lang>.mp4)")
    sp.add_argument("--out-dir", dest="out_dir")
    sp.add_argument("--provider")
    sp.add_argument("--gender", choices=["male", "female"])
    clone_grp = sp.add_mutually_exclusive_group()
    clone_grp.add_argument("--clone", action="store_true", help="force XTTS voice-clone")
    clone_grp.add_argument("--no-clone", action="store_true", help="force piper (no clone)")
    sp.add_argument("--still-image", dest="still_image", help="fixed frame instead of source video")
    sp.add_argument("--burn", action="store_true", help="burn subtitles into the picture")
    sp.add_argument("--speed", type=float, default=1.0, help="uniform playback speed factor")
    sp.set_defaults(func=cmd_dub)

    sp = sub.add_parser("mux", help="mux a rendered audio wav onto a video")
    sp.add_argument("input", help="source video")
    sp.add_argument("audio", help="audio wav to lay over it")
    sp.add_argument("--out")
    sp.add_argument("--lang")
    sp.add_argument("--captions", help="caption doc JSON (soft-subs source)")
    sp.add_argument("--subs", help="pre-rendered .vtt subtitle file")
    sp.add_argument("--burn", action="store_true")
    sp.add_argument("--speed", type=float, default=1.0)
    sp.set_defaults(func=cmd_mux)

    sp = sub.add_parser("size", help="compute a 16:9 (or --aspect) frame from one axis")
    sp.add_argument("axis", choices=["w", "h"])
    sp.add_argument("value", type=int)
    sp.add_argument("--aspect", default="16:9")
    sp.set_defaults(func=cmd_size)

    sp = sub.add_parser("speed", help="uniformly re-time any video (source untouched)")
    sp.add_argument("factor", type=float)
    sp.add_argument("input")
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_speed)

    sp = sub.add_parser("playlist", help="enumerate a playlist into a batch-list JSON (flag-free)")
    sp.add_argument("url")
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_playlist)

    sp = sub.add_parser("batch", help="run one id/url or a --list through the full pipeline")
    sp.add_argument("ref", nargs="?", help="a bare video id or url (single-video mode)")
    sp.add_argument("--list", help="a project-list JSON ({videos:[...]})")
    sp.add_argument("--out-root", dest="out_root", help="output root dir")
    sp.add_argument("--parallel", type=int, default=None, help="pool width (default: config)")
    # Per-video overrides (single id/url mode only) — override defaults.json for this run.
    sp.add_argument("--source-lang", dest="source_lang",
                    help="spoken source language code (overrides config sourceLang)")
    sp.add_argument("--target-langs", dest="target_langs",
                    help="comma-separated caption/translate target langs")
    sp.add_argument("--dub-langs", dest="dub_langs",
                    help="comma-separated dub target langs")
    sp.add_argument("--cc", dest="cc",
                    help="comma-separated closed-caption langs")
    sp.add_argument("--force", action="store_true",
                    help="keep the declared source lang even if ASR detects a different one")
    sp.set_defaults(func=cmd_batch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
