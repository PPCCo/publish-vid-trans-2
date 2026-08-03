from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import VideoTranslationHouseError
from .util import parse_csv, repo_root


def build_parser() -> argparse.ArgumentParser:
    # Global options live on a parent parser so they are accepted both before AND after
    # the subcommand (argparse otherwise rejects `... framework validate --compact`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", help="Repository root (defaults to auto-detected)")
    common.add_argument("--compact", action="store_true", help="Emit compact single-line JSON")

    parser = argparse.ArgumentParser(
        prog="vid_cli", description="publish-vid-trans deterministic CLI", parents=[common]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Environment, media tool, and engine checks")

    fw = sub.add_parser("framework", help="Framework-level operations")
    fwsub = fw.add_subparsers(dest="framework_command", required=True)
    fwsub.add_parser("validate", help="Validate state machine + schemas")

    proj = sub.add_parser("project", help="Project lifecycle operations")
    psub = proj.add_subparsers(dest="project_command", required=True)
    p_init = psub.add_parser("init")
    p_init.add_argument("project_id")
    p_init.add_argument("--url", required=True)
    p_init.add_argument("--targets", required=True, help="Comma-separated ISO 639-1 codes")
    p_init.add_argument("--audio", help="Comma-separated languages to dub (subset of targets)")
    p_init.add_argument("--channel")
    p_init.add_argument("--title")
    p_init.add_argument("--actor", default="human")
    psub.add_parser("list")
    for verb in ("status", "validate", "next", "plan"):
        pp = psub.add_parser(verb)
        pp.add_argument("project_id")
    p_trans = psub.add_parser("transition")
    p_trans.add_argument("project_id")
    p_trans.add_argument("--to", required=True)
    p_trans.add_argument("--actor", default="human")
    p_trans.add_argument("--reason", default="")

    art = sub.add_parser("artifact")
    asub = art.add_subparsers(dest="artifact_command", required=True)
    a_list = asub.add_parser("list")
    a_list.add_argument("project_id")
    a_reg = asub.add_parser("register")
    a_reg.add_argument("project_id")
    a_reg.add_argument("--path", required=True)
    a_reg.add_argument("--type", required=True)
    a_reg.add_argument("--stage", required=True)
    a_reg.add_argument("--lang")
    a_reg.add_argument("--source-ids", help="Comma-separated source artifact ids")
    a_reg.add_argument("--actor", default="agent")

    appr = sub.add_parser("approval")
    apsub = appr.add_subparsers(dest="approval_command", required=True)
    ap_list = apsub.add_parser("list")
    ap_list.add_argument("project_id")
    ap_grant = apsub.add_parser("grant")
    ap_grant.add_argument("project_id")
    ap_grant.add_argument("--gate", required=True)
    ap_grant.add_argument("--approver", required=True)
    ap_grant.add_argument("--artifact", required=True, help="path-or-hash (repeatable, comma-separated)")
    ap_grant.add_argument("--scope", required=True)
    ap_grant.add_argument("--lang")
    ap_grant.add_argument("--reject", action="store_true")
    ap_grant.add_argument("--notes")

    rights = sub.add_parser("rights")
    rsub = rights.add_subparsers(dest="rights_command", required=True)
    r_check = rsub.add_parser("check")
    r_check.add_argument("project_id")
    r_set = rsub.add_parser("set")
    r_set.add_argument("project_id")
    r_set.add_argument("--status", required=True)
    r_set.add_argument("--reviewer", required=True)
    r_set.add_argument("--voice-clone-consent", action="store_true")
    r_set.add_argument("--evidence", help="Comma-separated evidence paths")
    r_set.add_argument("--notes")

    ingest = sub.add_parser("ingest", help="Download + extract + catalog a project's source")
    isub = ingest.add_subparsers(dest="ingest_command", required=True)
    i_run = isub.add_parser("run")
    i_run.add_argument("project_id")
    i_run.add_argument("--actor", default="agent")
    i_run.add_argument("--force", action="store_true", help="Re-download even if source exists")
    i_run.add_argument("--no-subs", action="store_true", help="Skip fetching YouTube caption tracks")
    i_run.add_argument("--no-advance", action="store_true", help="Do not transition to LANGUAGE_ID")

    tr = sub.add_parser("transcript", help="Source-language transcription + QA")
    trsub = tr.add_subparsers(dest="transcript_command", required=True)
    t_run = trsub.add_parser("run", help="Transcribe source audio via the ASR adapter")
    t_run.add_argument("project_id")
    t_run.add_argument("--provider", help="ASR provider (default: first installed)")
    t_run.add_argument("--model", help="Engine model name (e.g. large-v3)")
    t_run.add_argument("--no-word-timestamps", action="store_true", help="Skip word-level timing")
    t_run.add_argument("--advance", action="store_true", help="Advance to TRANSCRIPT_QA_GATE")
    t_run.add_argument("--actor", default="agent")
    t_import = trsub.add_parser("import", help="Import an engine-shaped JSON transcript (no engine needed)")
    t_import.add_argument("project_id")
    t_import.add_argument("--from", dest="from_json", required=True, help="Whisper-shaped JSON path")
    t_import.add_argument("--provider", default="manual")
    t_import.add_argument("--model")
    t_import.add_argument("--advance", action="store_true")
    t_import.add_argument("--actor", default="human")
    t_qa = trsub.add_parser("qa", help="Run deterministic QA + write the gate report")
    t_qa.add_argument("project_id")
    t_qa.add_argument("--language")
    t_qa.add_argument("--actor", default="agent")

    tl = sub.add_parser("translate", help="Per-language translation worksheet + captions.<lang>.json")
    tlsub = tl.add_subparsers(dest="translate_command", required=True)
    tl_export = tlsub.add_parser("export", help="Export a translation worksheet from the transcript")
    tl_export.add_argument("project_id")
    tl_export.add_argument("--language", required=True, help="Target language (ISO 639-1)")
    tl_export.add_argument("--actor", default="agent")
    tl_import = tlsub.add_parser("import", help="Import a filled worksheet -> canonical captions")
    tl_import.add_argument("project_id")
    tl_import.add_argument("--language", required=True)
    tl_import.add_argument("--from", dest="from_path", help="Worksheet path (default: captions/<lang>.worksheet.json)")  # noqa: E501
    tl_import.add_argument("--advance", action="store_true", help="Advance top state once all tracks done")
    tl_import.add_argument("--actor", default="agent")
    tl_qa = tlsub.add_parser("qa", help="Aggregate translation-qa + glossary gate reports")
    tl_qa.add_argument("project_id")
    tl_qa.add_argument("--actor", default="agent")

    cap = sub.add_parser("captions", help="Render + validate closed captions")
    capsub = cap.add_subparsers(dest="captions_command", required=True)
    cap_build = capsub.add_parser("build", help="Render deterministic SRT/VTT from captions.<lang>.json")
    cap_build.add_argument("project_id")
    cap_build.add_argument("--language", required=True)
    cap_build.add_argument("--format", dest="formats", help="Comma-separated formats (default: srt,vtt)")
    cap_build.add_argument("--actor", default="agent")
    cap_val = capsub.add_parser("validate", help="Readability validation -> the caption gate report")
    cap_val.add_argument("project_id")
    cap_val.add_argument("--actor", default="agent")

    dub = sub.add_parser("dub", help="Per-language dubbing + sync (audio/<lang>/dub.wav)")
    dubsub = dub.add_subparsers(dest="dub_command", required=True)
    d_run = dubsub.add_parser("run", help="Synthesize a dub from captions via the TTS adapter")
    d_run.add_argument("project_id")
    d_run.add_argument("--language", required=True, help="Target language (ISO 639-1)")
    d_run.add_argument("--provider", help="TTS provider (default: per-language config / first installed)")  # noqa: E501
    d_run.add_argument("--model", help="Engine voice/model name")
    d_run.add_argument("--voice", help="Named neutral voice for the engine")
    d_run.add_argument("--clone", action="store_true", help="Clone source speaker (needs recorded consent)")  # noqa: E501
    d_run.add_argument("--advance", action="store_true", help="Advance top state once all dub tracks done")  # noqa: E501
    d_run.add_argument("--actor", default="agent")
    d_import = dubsub.add_parser("import", help="Import a pre-rendered dub WAV (no engine needed)")
    d_import.add_argument("project_id")
    d_import.add_argument("--language", required=True)
    d_import.add_argument("--from", dest="from_path", required=True, help="Rendered dub WAV path")
    d_import.add_argument("--advance", action="store_true")
    d_import.add_argument("--actor", default="agent")
    d_qa = dubsub.add_parser("qa", help="Aggregate audio-sync gate report across dub tracks")
    d_qa.add_argument("project_id")
    d_qa.add_argument("--actor", default="agent")

    pkg = sub.add_parser("package", help="Mux dubbed video + assemble deliverable packages")
    pkgsub = pkg.add_subparsers(dest="package_command", required=True)
    pk_mux = pkgsub.add_parser("mux", help="Mux source video + a language's dub -> video/<lang>/dubbed.mp4")  # noqa: E501
    pk_mux.add_argument("project_id")
    pk_mux.add_argument("--language", required=True, help="Dub-enabled target language (ISO 639-1)")
    pk_mux.add_argument("--no-subs", dest="with_subs", action="store_false",
                        help="Do not embed the VTT as a soft-subtitle track")
    pk_mux.add_argument("--advance", action="store_true", help="Advance top state once all dub tracks muxed")  # noqa: E501
    pk_mux.add_argument("--actor", default="agent")
    pk_final = pkgsub.add_parser("final-qa", help="Aggregate `final` gate report across dubbed videos")  # noqa: E501
    pk_final.add_argument("project_id")
    pk_final.add_argument("--actor", default="agent")
    pk_build = pkgsub.add_parser("build", help="Assemble packages/<lang>/ + package-manifest.json")
    pk_build.add_argument("project_id")
    pk_build.add_argument("--advance", action="store_true", help="Advance top state once all tracks packaged")  # noqa: E501
    pk_build.add_argument("--actor", default="agent")

    cat = sub.add_parser("catalog", help="Repo-global source-video index")
    csub = cat.add_subparsers(dest="catalog_command", required=True)
    csub.add_parser("list")
    c_show = csub.add_parser("show")
    c_show.add_argument("video_id")

    lid = sub.add_parser("langid", help="Source-language identification (human-overridable)")
    lsub = lid.add_subparsers(dest="langid_command", required=True)
    l_detect = lsub.add_parser("detect")
    l_detect.add_argument("project_id")
    l_set = lsub.add_parser("set")
    l_set.add_argument("project_id")
    l_set.add_argument("--language", required=True, help="ISO 639-1 code")
    l_set.add_argument("--source", default="manual", choices=["manual", "auto-detect", "youtube-metadata"])
    l_set.add_argument("--confidence", type=float)
    l_set.add_argument("--dialect")
    l_set.add_argument("--actor", default="human")

    ev = sub.add_parser("event")
    evsub = ev.add_subparsers(dest="event_command", required=True)
    e_tail = evsub.add_parser("tail")
    e_tail.add_argument("project_id")
    e_tail.add_argument("-n", type=int, default=20)

    return parser


def dispatch(args: argparse.Namespace, root: Path) -> Any:
    from . import approvals, artifacts, project, rights, state
    from .doctor import run_doctor
    from .events import read_events
    from .paths import ProjectPaths
    from .util import load_json as load_json_file
    from .validation import validate_framework

    cmd = args.command
    if cmd == "doctor":
        return run_doctor(root)
    if cmd == "framework":
        if args.framework_command == "validate":
            return validate_framework(root)
    if cmd == "project":
        pc = args.project_command
        if pc == "init":
            return project.init_project(
                root, args.project_id, url=args.url,
                target_languages=parse_csv(args.targets),
                audio_languages=parse_csv(args.audio) if args.audio else None,
                channel=args.channel, title=args.title, actor=args.actor,
            )
        if pc == "list":
            return project.list_projects(root)
        if pc == "status":
            return project.project_status(root, args.project_id)
        if pc == "validate":
            return project.validate_project(root, args.project_id)
        if pc == "next":
            return state.next_actions(root, args.project_id)
        if pc == "plan":
            return state.plan(root, args.project_id)
        if pc == "transition":
            return state.transition(root, args.project_id, args.to, args.actor, args.reason)
    if cmd == "artifact":
        if args.artifact_command == "list":
            return {"artifacts": artifacts.list_artifacts(root, args.project_id)}
        if args.artifact_command == "register":
            src = None
            if args.source_ids:
                src = parse_csv(args.source_ids)
            return artifacts.register_artifact(
                root, args.project_id, args.path, args.type, args.stage, args.actor,
                language=args.lang, source_artifact_ids=src,
            )
    if cmd == "approval":
        if args.approval_command == "list":
            return {"approvals": approvals.list_approvals(root, args.project_id)}
        if args.approval_command == "grant":
            hashes = [artifacts.resolve_hash(root, args.project_id, x) for x in parse_csv(args.artifact)]
            return approvals.grant_approval(
                root, args.project_id, args.gate, args.approver, hashes, args.scope,
                language=args.lang, decision="REJECTED" if args.reject else "APPROVED",
                notes=args.notes,
            )
    if cmd == "rights":
        if args.rights_command == "check":
            return rights.check_rights(root, args.project_id)
        if args.rights_command == "set":
            return rights.set_rights(
                root, args.project_id, status=args.status, reviewer=args.reviewer,
                voice_clone_consent=args.voice_clone_consent,
                evidence_paths=parse_csv(args.evidence) if args.evidence else None,
                notes=args.notes,
            )
    if cmd == "ingest":
        from . import ingest as ingest_mod
        if args.ingest_command == "run":
            return ingest_mod.ingest_project(
                root, args.project_id, actor=args.actor, force=args.force,
                write_subs=not args.no_subs, advance=not args.no_advance,
            )
    if cmd == "transcript":
        from . import transcript as transcript_mod
        tc = args.transcript_command
        if tc == "run":
            return transcript_mod.run_transcription(
                root, args.project_id, provider=args.provider, model=args.model,
                word_timestamps=not args.no_word_timestamps, actor=args.actor, advance=args.advance,
            )
        if tc == "import":
            from .engines.asr import ASRResult, _normalize_whisper_json
            raw = load_json_file(args.from_json)
            cues, has_words = _normalize_whisper_json(raw)
            paths = ProjectPaths(root, args.project_id).require()
            language = load_json_file(str(paths.state)).get("source_language")
            if not language:
                raise VideoTranslationHouseError("source_language not set; run `langid set` first")
            result = ASRResult(
                provider=args.provider, model=args.model, language=language, cues=cues,
                has_word_timing=has_words, duration_seconds=raw.get("duration"),
                word_alignment="native-whisper" if has_words else None,
            )
            doc = transcript_mod.build_transcript_doc(args.project_id, language, result, actor=args.actor)
            written = transcript_mod.write_transcript(root, args.project_id, doc, actor=args.actor)
            advanced_to = "TRANSCRIPTION"
            if args.advance:
                state.transition(root, args.project_id, "TRANSCRIPT_QA_GATE", args.actor,
                                 reason="transcript imported")
                advanced_to = "TRANSCRIPT_QA_GATE"
            return {"project_id": args.project_id, "language": language, **written,
                    "advanced_to": advanced_to}
        if tc == "qa":
            from . import transcript_qa
            return transcript_qa.run_transcript_qa(
                root, args.project_id, language=args.language, actor=args.actor,
            )
    if cmd == "translate":
        from . import translate as translate_mod
        tlc = args.translate_command
        if tlc == "export":
            return translate_mod.export_worksheet(
                root, args.project_id, args.language, actor=args.actor,
            )
        if tlc == "import":
            return translate_mod.import_worksheet(
                root, args.project_id, args.language, from_path=args.from_path,
                actor=args.actor, advance=args.advance,
            )
        if tlc == "qa":
            return translate_mod.run_translation_qa(root, args.project_id, actor=args.actor)
    if cmd == "captions":
        from . import translate as translate_mod
        cpc = args.captions_command
        if cpc == "build":
            return translate_mod.build_captions(
                root, args.project_id, args.language,
                formats=parse_csv(args.formats) or None, actor=args.actor,
            )
        if cpc == "validate":
            return translate_mod.run_caption_validation(root, args.project_id, actor=args.actor)
    if cmd == "dub":
        from . import dubbing as dubbing_mod
        dc = args.dub_command
        if dc == "run":
            return dubbing_mod.run_dub(
                root, args.project_id, args.language, provider=args.provider,
                model=args.model, voice=args.voice, clone=args.clone,
                actor=args.actor, advance=args.advance,
            )
        if dc == "import":
            return dubbing_mod.import_dub(
                root, args.project_id, args.language, from_path=args.from_path,
                actor=args.actor, advance=args.advance,
            )
        if dc == "qa":
            return dubbing_mod.run_audio_qa(root, args.project_id, actor=args.actor)
    if cmd == "package":
        from . import packaging as packaging_mod
        pc = args.package_command
        if pc == "mux":
            return packaging_mod.run_mux(
                root, args.project_id, args.language, with_subs=args.with_subs,
                actor=args.actor, advance=args.advance,
            )
        if pc == "final-qa":
            return packaging_mod.run_final_qa(root, args.project_id, actor=args.actor)
        if pc == "build":
            return packaging_mod.run_package(
                root, args.project_id, actor=args.actor, advance=args.advance,
            )
    if cmd == "catalog":
        from . import catalog as catalog_mod
        if args.catalog_command == "list":
            return {"videos": catalog_mod.list_entries(root)}
        if args.catalog_command == "show":
            entry = catalog_mod.get_entry(root, args.video_id)
            if entry is None:
                raise VideoTranslationHouseError(f"No catalog entry: {args.video_id}")
            return entry
    if cmd == "langid":
        from . import langid as langid_mod
        if args.langid_command == "detect":
            paths = ProjectPaths(root, args.project_id).require()
            wav = paths.source_dir / "audio.wav"
            return langid_mod.detect_from_audio(root, wav)
        if args.langid_command == "set":
            return langid_mod.set_language(
                root, args.project_id, args.language, source=args.source,
                confidence=args.confidence, dialect=args.dialect, actor=args.actor,
            )
    if cmd == "event":
        if args.event_command == "tail":
            paths = ProjectPaths(root, args.project_id).require()
            return {"events": read_events(paths.events, tail=args.n)}
    raise VideoTranslationHouseError(f"Unhandled command: {cmd}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        root = Path(args.root).expanduser().resolve() if args.root else repo_root()
        result = dispatch(args, root)
        indent = None if args.compact else 2
        print(json.dumps(result, indent=indent, ensure_ascii=False, default=str))
        if isinstance(result, dict) and (result.get("valid") is False or result.get("status") == "fail"):
            return 1
        return 0
    except (VideoTranslationHouseError, FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc), "error_type": type(exc).__name__}, indent=2),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
