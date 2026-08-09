from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .errors import VideoTranslationHouseError
from .util import parse_csv, repo_root


def _load_json_arg(value: str) -> Any:
    """Parse a CLI argument that is either inline JSON or ``@path`` to a JSON file."""
    text = value
    if value.startswith("@"):
        text = Path(value[1:]).expanduser().read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise VideoTranslationHouseError(f"invalid JSON in --selection: {exc}") from exc


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
    p_init.add_argument(
        "--selection",
        help="Source windows to process as inline JSON or @file. Object with a `windows` "
             "list ({start,end,[id,label,exact]}); omit for the whole video.",
    )
    p_init.add_argument("--no-join-clips", action="store_true",
                        help="Emit each selected window as a separate clip (default: join into one video)")
    p_init.add_argument("--no-snap-edges", action="store_true",
                        help="Cut selection windows at exact requested timecodes (default: snap to cue boundaries)")  # noqa: E501
    p_init.add_argument("--dub-voice-gender", choices=["male", "female"], default="male",
                        help="Dub voice gender for ALL languages (default male). Overridden to "
                             "female only by an explicit human decision (e.g. a female source voice).")
    p_init.add_argument("--voice-clone-requested", action="store_true",
                        help="Record the human's INTENT to clone the source speaker's voice. "
                             "Advisory only — actual cloning still requires recorded consent in the "
                             "rights record (rule 5); this never grants it.")
    p_init.add_argument("--no-clone", action="store_true",
                        help="Opt OUT of voice cloning for this project (neutral dub voice). By "
                             "default cloning is ON per company policy (rule-5 authorized override) "
                             "with consent auto-recorded at init; this restores the rule-5 neutral "
                             "default for one project.")
    p_init.add_argument("--mux-mode", choices=["soft-subs", "burned-in", "no-subs"],
                        default="soft-subs",
                        help="Default subtitle handling at `package mux`: soft-subs (toggleable "
                             "track, default), burned-in (painted into the picture), or no-subs "
                             "(clean video, captions only as sidecar files). A per-run `package mux "
                             "--mode` still overrides.")
    p_init.add_argument("--glossary",
                        help="Glossary id (glossary/<id>.json) to pin term renderings for this "
                             "project. Omit for none. See `glossary list`.")
    p_init.add_argument("--images",
                        help="Per-language still image shown for the whole runtime with the dub "
                             "over it (instead of the source video), as a lang=path comma map, e.g. "
                             "'en=/p/a.jpg,ur=/p/b.png'. Languages omitted keep the source video.")
    p_init.add_argument("--speeds",
                        help="Per-language deliberate playback speed (audio+video scaled together) "
                             "as a lang=factor comma map, e.g. 'en=1.25,ur=1.25'. Default 1.0 (no "
                             "change); languages omitted play at 1x.")
    p_init.add_argument("--source-language",
                        help="Pre-fill the confirmed source language (ISO 639-1, e.g. 'fa') at init "
                             "time, via the same `langid set` path normally run at LANGUAGE_ID. "
                             "Convenience only — a human can still revise it later at LANGUAGE_ID "
                             "(`detect-language`/`langid set`). Omit to defer to LANGUAGE_ID as before.")
    p_init.add_argument("--source-voice-gender", choices=["male", "female"],
                        help="Gender of the SOURCE speaker (human observation), recorded alongside "
                             "--source-language. Ignored if --source-language is omitted.")
    p_init.add_argument("--actor", default="human")
    psub.add_parser("list")
    for verb in ("status", "validate", "next", "plan"):
        pp = psub.add_parser(verb)
        pp.add_argument("project_id")
    p_addlang = psub.add_parser(
        "add-languages",
        help="Add translation/dub tracks to an existing project (keeps existing work)")
    p_addlang.add_argument("project_id")
    p_addlang.add_argument("--targets", required=True,
                           help="Comma-separated ISO 639-1 codes to add")
    p_addlang.add_argument("--audio",
                           help="Comma-separated subset of the added langs to dub (default: all added)")
    p_addlang.add_argument("--actor", default="human")
    p_endub = psub.add_parser(
        "enable-dub",
        help="Enable/disable dubbing on existing translate tracks (reuses their captions; no re-translate)")  # noqa: E501
    p_endub.add_argument("project_id")
    p_endub.add_argument("--targets", required=True,
                         help="Comma-separated ISO 639-1 codes of existing tracks to (un)dub")
    p_endub.add_argument("--actor", default="human")
    p_endub.add_argument("--disable", action="store_true",
                         help="Turn dubbing OFF for the named tracks (default: on)")
    p_endub.add_argument("--force", action="store_true",
                         help="With --disable, allow disabling even if a dub-wav artifact exists (rule 6)")
    p_setimg = psub.add_parser(
        "set-image",
        help="Set/clear the per-language still image shown over the dub (instead of source video)")
    p_setimg.add_argument("project_id")
    p_setimg.add_argument("--language", required=True, help="Target language ISO code")
    p_setimg.add_argument("--path", help="Image file (.jpg/.jpeg/.png/.webp/.bmp); omit with --clear")
    p_setimg.add_argument("--clear", action="store_true",
                          help="Remove the language's still image (revert to the source video)")
    p_setimg.add_argument("--actor", default="human")
    p_setspeed = psub.add_parser(
        "set-speed",
        help="Set the per-language uniform playback speed applied at mux (audio+video together)")
    p_setspeed.add_argument("project_id")
    p_setspeed.add_argument("--language", required=True, help="Target language ISO code")
    p_setspeed.add_argument("--factor", required=True, type=float,
                            help="Speed factor (>1 faster, <1 slower); 1.0 resets to default")
    p_setspeed.add_argument("--actor", default="human")
    p_redub = psub.add_parser(
        "redub",
        help="Scoped rewind so ONE dub track can be re-rendered (preserves other tracks' work/approvals)")  # noqa: E501
    p_redub.add_argument("project_id")
    p_redub.add_argument("--targets", required=True,
                         help="Comma-separated ISO 639-1 codes of dubbed tracks to re-render")
    p_redub.add_argument("--actor", default="agent")
    p_syncscope = psub.add_parser(
        "sync-scope",
        help="Reconcile two-axis review markers (skip_translation/auto_translate) on a pre-feature project")  # noqa: E501
    p_syncscope.add_argument("project_id")
    p_syncscope.add_argument("--actor", default="human")
    p_auto = psub.add_parser(
        "autopilot",
        help="Run deterministic pipeline steps back-to-back until a human gate/blocker (scripted route)")  # noqa: E501
    p_auto.add_argument("project_id")
    p_auto.add_argument("--mt", action="store_true",
                        help="Fill translation worksheets + English gloss via the MT engine (no Claude)")  # noqa: E501
    p_auto.add_argument("--until", help="Stop when this state is reached (default: first human gate)")
    p_auto.add_argument("--dry-run", action="store_true",
                        help="Print the ordered steps without executing or mutating state")
    p_auto.add_argument("--actor", default="agent")
    p_trans = psub.add_parser("transition")
    p_trans.add_argument("project_id")
    p_trans.add_argument("--to", required=True)
    p_trans.add_argument("--actor", default="human")
    p_trans.add_argument("--reason", default="")
    p_reset = psub.add_parser("reset", help="Rewind a project, wiping downstream work (keeps source+transcript by default)")  # noqa: E501
    p_reset.add_argument("project_id")
    p_reset.add_argument("--to", help="Resume state (default: TRANSCRIPTION when transcript kept)")
    p_reset.add_argument("--full", action="store_true",
                         help="Drop source AND transcript too (like --drop-source --drop-transcript)")
    p_reset.add_argument("--drop-source", action="store_true", help="Do not preserve source/ media")
    p_reset.add_argument("--drop-transcript", action="store_true",
                         help="Do not preserve the source ASR transcript")
    p_reset.add_argument("--actor", default="agent")
    p_delete = psub.add_parser("delete", help="Permanently remove a project directory + catalog entry")
    p_delete.add_argument("project_id")
    p_delete.add_argument("--keep-catalog", action="store_true",
                          help="Leave the catalog entry in place (default: remove it)")
    p_delete.add_argument("--actor", default="agent")

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

    gloss = sub.add_parser("glossary", help="Translation glossary operations")
    glsub = gloss.add_subparsers(dest="glossary_command", required=True)
    glsub.add_parser("list", help="List available glossaries (glossary/<id>.json) + term counts")

    ingest = sub.add_parser("ingest", help="Download + extract + catalog a project's source")
    isub = ingest.add_subparsers(dest="ingest_command", required=True)
    i_run = isub.add_parser("run")
    i_run.add_argument("project_id")
    i_run.add_argument("--actor", default="agent")
    i_run.add_argument("--force", action="store_true", help="Re-download even if source exists")
    i_run.add_argument("--no-subs", action="store_true", help="Skip fetching YouTube caption tracks")
    i_run.add_argument("--no-advance", action="store_true", help="Do not transition to LANGUAGE_ID")
    i_ensure = isub.add_parser(
        "ensure",
        help="Re-fetch the source video/WAV if deleted (media-restore; does not change state)")
    i_ensure.add_argument("project_id")
    i_ensure.add_argument("--actor", default="agent")
    i_ensure.add_argument("--no-subs", action="store_true", help="Skip fetching YouTube caption tracks")

    tr = sub.add_parser("transcript", help="Source-language transcription + QA")
    trsub = tr.add_subparsers(dest="transcript_command", required=True)
    t_run = trsub.add_parser("run", help="Transcribe source audio via the ASR adapter")
    t_run.add_argument("project_id")
    t_run.add_argument("--provider", help="ASR provider (default: first installed)")
    t_run.add_argument("--model", help="Engine model name (e.g. large-v3)")
    t_run.add_argument("--no-word-timestamps", action="store_true", help="Skip word-level timing")
    t_run.add_argument("--no-condition-on-previous-text", action="store_true",
                       help="Disable conditioning on prior text (main defense vs repetition loops)")
    t_run.add_argument("--hallucination-silence-threshold", type=float,
                       help="Skip likely hallucinations across silences longer than N seconds")
    t_run.add_argument("--temperature", type=float, help="Decode temperature (default 0 = deterministic)")
    t_run.add_argument("--no-retry", action="store_true",
                       help="Run one decode configuration only (disable the escalating auto-retry ladder)")
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
    t_geng = trsub.add_parser("english-export",
                              help="Export an English review-gloss worksheet from the source transcript")
    t_geng.add_argument("project_id")
    t_geng.add_argument("--actor", default="agent")
    t_gimp = trsub.add_parser("english-import",
                              help="Import a filled English-gloss worksheet -> transcript/english-gloss.json")
    t_gimp.add_argument("project_id")
    t_gimp.add_argument("--from", dest="from_path",
                        help="Worksheet path (default: transcript/english-gloss.worksheet.json)")
    t_gimp.add_argument("--actor", default="agent")
    t_grec = trsub.add_parser(
        "reconcile-verses",
        help="Merge english-verses-gloss.worksheet.json modified_translation overrides into "
             "the gloss worksheet (also run automatically as the first step of english-import)")
    t_grec.add_argument("project_id")
    t_grec.add_argument("--actor", default="agent")

    seg = sub.add_parser("segments", help="Resolve a source selection into cut/join segments")
    segsub = seg.add_subparsers(dest="segments_command", required=True)
    s_resolve = segsub.add_parser("resolve", help="Resolve selection -> segments/segments.json (SEGMENT_RESOLUTION)")  # noqa: E501
    s_resolve.add_argument("project_id")
    s_resolve.add_argument("--advance", action="store_true", help="Advance SEGMENT_RESOLUTION -> TRANSLATION")
    s_resolve.add_argument("--actor", default="agent")
    s_cut = segsub.add_parser("cut", help="Extract per-segment clip media + clip-local transcripts")
    s_cut.add_argument("project_id")
    s_cut.add_argument("--no-reencode", action="store_true",
                       help="Stream-copy clips instead of re-encoding (only safe on keyframe-aligned cuts)")  # noqa: E501
    s_cut.add_argument("--actor", default="agent")
    for verb in ("show", "list"):
        sv = segsub.add_parser(verb, help="Show the resolved segments manifest")
        sv.add_argument("project_id")

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
    tl_mfill = tlsub.add_parser(
        "machine-fill",
        help="Fill a worksheet's empty target_text via the MT engine (scripted/manual route)",
    )
    tl_mfill.add_argument("project_id")
    tl_mfill.add_argument("--language", required=True, help="Target language (ISO 639-1)")
    tl_mfill.add_argument("--provider", help="MT provider (default: tools config mt_baseline)")
    tl_mfill.add_argument("--model", help="Engine model/package name")
    tl_mfill.add_argument("--actor", default="agent")
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
    d_run.add_argument("--gender", choices=["male", "female"],
                       help="Dub voice gender to select from the company voices registry "
                            "(default: project dubbing.voice_gender, else company default male). "
                            "Ignored when --model is given (explicit model wins) or --clone.")
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
    pk_mux.add_argument("--mode", default=None, choices=["soft-subs", "burned-in", "no-subs"],
                        help="soft-subs: toggleable mov_text track (picture copied bit-for-bit); "
                             "burned-in: render VTT into the picture (re-encodes video, for "
                             "platforms that drop soft subs); no-subs: dub audio only. Omit to use "
                             "the project's mux_mode (set at init, default soft-subs).")
    pk_mux.add_argument("--advance", action="store_true", help="Advance top state once all dub tracks muxed")  # noqa: E501
    pk_mux.add_argument("--actor", default="agent")
    pk_final = pkgsub.add_parser("final-qa", help="Aggregate `final` gate report across dubbed videos")  # noqa: E501
    pk_final.add_argument("project_id")
    pk_final.add_argument("--actor", default="agent")
    pk_build = pkgsub.add_parser("build", help="Assemble packages/<lang>/ + package-manifest.json")
    pk_build.add_argument("project_id")
    pk_build.add_argument("--advance", action="store_true", help="Advance top state once all tracks packaged")  # noqa: E501
    pk_build.add_argument("--actor", default="agent")

    dist = sub.add_parser(
        "distribute",
        help="Phase 6: chapters, platform packaging, upload, promotion (OFF by default)",
    )
    distsub = dist.add_subparsers(dest="distribute_command", required=True)

    # distribute chapters export/import (worksheet round-trip; no LLM in the CLI)
    d_chap = distsub.add_parser("chapters", help="Titled breakpoints via worksheet export/import")
    chapsub = d_chap.add_subparsers(dest="chapters_command", required=True)
    dc_export = chapsub.add_parser("export", help="Export a chapter worksheet from captions.<lang>.json")
    dc_export.add_argument("project_id")
    dc_export.add_argument("--language", required=True)
    dc_export.add_argument("--actor", default="agent")
    dc_import = chapsub.add_parser("import", help="Import a filled chapter worksheet -> chapters.<lang>.json")
    dc_import.add_argument("project_id")
    dc_import.add_argument("--language", required=True)
    dc_import.add_argument("--from", dest="from_path",
                           help="Worksheet path (default: chapters/<lang>.chapters-worksheet.json)")
    dc_import.add_argument("--actor", default="agent")

    # distribute package -> PLATFORM_PACKAGING
    d_pkg = distsub.add_parser("package", help="Build per-platform upload metadata (PLATFORM_PACKAGING)")
    d_pkg.add_argument("project_id")
    d_pkg.add_argument("--advance", action="store_true",
                       help="Advance PLATFORM_PACKAGING -> RELEASE_AUTHORIZATION (human gate will hold)")
    d_pkg.add_argument("--actor", default="agent")

    # distribute youtube -> upload (dry-run default; needs prior release_authorization)
    d_yt = distsub.add_parser("youtube", help="Upload a language's video to YouTube (dry-run default)")
    d_yt.add_argument("project_id")
    d_yt.add_argument("--language", required=True)
    d_yt.add_argument("--live", action="store_true",
                      help="Perform the real upload (default: dry-run, no network write)")
    d_yt.add_argument("--advance", action="store_true",
                      help="On a live upload, advance YOUTUBE_UPLOAD -> PROMOTION_QUEUE")
    d_yt.add_argument("--actor", default="agent")

    # distribute promote queue/publish
    d_promo = distsub.add_parser("promote", help="Draft (queue) then publish promotional posts")
    promosub = d_promo.add_subparsers(dest="promote_command", required=True)
    dp_queue = promosub.add_parser("queue", help="Draft per-platform posts (PROMOTION_QUEUE)")
    dp_queue.add_argument("project_id")
    dp_queue.add_argument("--advance", action="store_true",
                          help="Advance PROMOTION_QUEUE -> PROMOTION_REVIEW (human gate will hold)")
    dp_queue.add_argument("--actor", default="agent")
    dp_pub = promosub.add_parser("publish",
                                 help="Fire approved posts (dry-run default; needs promotion_review)")
    dp_pub.add_argument("project_id")
    dp_pub.add_argument("--live", action="store_true",
                        help="Perform real posts (default: dry-run, no network write)")
    dp_pub.add_argument("--advance", action="store_true",
                        help="On a live publish, advance through PROMOTION_PUBLISHED -> MONITORING")
    dp_pub.add_argument("--actor", default="agent")

    # read-only manifest views
    for view in ("package-show", "upload-show", "promotion-show"):
        dv = distsub.add_parser(view, help="Show a distribution manifest (read-only)")
        dv.add_argument("project_id")

    cat = sub.add_parser("catalog", help="Repo-global source-video index")
    csub = cat.add_subparsers(dest="catalog_command", required=True)
    csub.add_parser("list")
    c_show = csub.add_parser("show")
    c_show.add_argument("video_id")
    c_addpl = csub.add_parser(
        "add-playlist",
        help="Enumerate a playlist (metadata only, flag-free) and index each video under it")
    c_addpl.add_argument("url")
    c_addpl.add_argument("--actor", default="agent")
    csub.add_parser("playlists", help="List indexed playlists")
    c_pl = csub.add_parser("playlist", help="Show one playlist's videos (enriched)")
    c_pl.add_argument("playlist_id")

    bud = sub.add_parser("budget", help="Vendor (billed) TTS spend ceiling + ledger (read-only)")
    budsub = bud.add_subparsers(dest="budget_command", required=True)
    budsub.add_parser("status", help="Ceiling, running spend, and remaining headroom")

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
    l_set.add_argument("--source-voice-gender", choices=["male", "female"],
                       help="Gender of the speaker's voice in the SOURCE video (human observation; "
                            "default male). A female source is the cue to ask whether dubs should be "
                            "female too — it does not itself change the dub voice.")
    l_set.add_argument("--actor", default="human")

    ev = sub.add_parser("event")
    evsub = ev.add_subparsers(dest="event_command", required=True)
    e_tail = evsub.add_parser("tail")
    e_tail.add_argument("project_id")
    e_tail.add_argument("-n", type=int, default=20)

    sz = sub.add_parser("size", help="Compute full WxH from one axis + aspect (default 16:9)")
    sz.add_argument("axis", choices=["w", "h", "width", "height"],
                    help="Which axis the value is: w(idth) or h(eight)")
    sz.add_argument("value", type=int, help="Pixel size of that axis")
    sz.add_argument("--aspect", default="16:9", help="Target aspect ratio W:H (default 16:9)")

    sp = sub.add_parser("speed", help="Uniformly re-time ANY video by a factor (audio+video together)")
    sp.add_argument("factor", type=float, help="Speed factor (>1 faster/shorter, <1 slower/longer)")
    sp.add_argument("path", help="Path to a video file (source is never modified)")
    sp.add_argument("--out", help="Output path (default: <stem>_<factor><suffix> beside the source)")

    cg = sub.add_parser(
        "cmd",
        help="Print the raw external command (+ vid_cli.py alternative) to onboard/ingest a "
             "video or playlist")
    cg.add_argument("video_id_or_url",
                    help="A video URL/bare-id, a playlist URL/id, or an existing catalog video_id")
    cg.add_argument("--for-claude", dest="for_claude", action="store_true",
                    help="Prefix runnable lines with `!` (default: bare terminal form)")

    nc = sub.add_parser(
        "nextcmd",
        help="Print the raw external command (+ vid_cli.py alternative) for a project's current "
             "next step")
    nc.add_argument("project_id")
    nc.add_argument("--for-claude", dest="for_claude", action="store_true",
                    help="Prefix runnable lines with `!` (default: bare terminal form)")

    dele = sub.add_parser(
        "delete",
        help="Permanently remove a project directory + catalog entry (top-level alias for "
             "`project delete`)")
    dele.add_argument("project_id")
    dele.add_argument("--keep-catalog", action="store_true",
                      help="Leave the catalog entry in place (default: remove it)")
    dele.add_argument("--actor", default="agent")

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
    if cmd == "size":
        from . import size as size_mod
        return size_mod.compute_dimensions(args.axis, args.value, aspect=args.aspect)
    if cmd == "speed":
        from . import speed as speed_mod
        return speed_mod.respeed(root, args.factor, args.path, dest=args.out)
    if cmd == "cmd":
        from . import cmdgen
        return cmdgen.cmd_for_video(root, args.video_id_or_url, for_claude=args.for_claude)
    if cmd == "nextcmd":
        from . import cmdgen
        return cmdgen.nextcmd_for_project(root, args.project_id, for_claude=args.for_claude)
    if cmd == "delete":
        return project.delete_project(
            root, args.project_id,
            purge_catalog=not args.keep_catalog, actor=args.actor,
        )
    if cmd == "project":
        pc = args.project_command
        if pc == "init":
            selection = _load_json_arg(args.selection) if args.selection else None
            result = project.init_project(
                root, args.project_id, url=args.url,
                target_languages=parse_csv(args.targets),
                audio_languages=parse_csv(args.audio) if args.audio else None,
                channel=args.channel, title=args.title, actor=args.actor,
                selection=selection,
                join_clips=not args.no_join_clips,
                snap_edges=not args.no_snap_edges,
                dub_voice_gender=args.dub_voice_gender,
                voice_clone_requested=args.voice_clone_requested,
                voice_clone=False if args.no_clone else None,
                mux_mode=args.mux_mode,
                glossary_id=args.glossary,
                images=project.parse_lang_map(args.images, kind="images") if args.images else None,
                playback_speed=(
                    {k: float(v) for k, v in
                     project.parse_lang_map(args.speeds, kind="speeds").items()}
                    if args.speeds else None),
            )
            # Optional convenience pre-fill: run the exact same `langid set` path normally
            # invoked at LANGUAGE_ID, right after the project directory exists. A human can
            # still revise it later at LANGUAGE_ID (rule-5-style prefill, not a gate removal).
            if args.source_language:
                from . import langid as langid_mod
                from .paths import ProjectPaths
                from .util import load_json

                langid_mod.set_language(
                    root, args.project_id, args.source_language,
                    source="manual", source_voice_gender=args.source_voice_gender,
                    actor=args.actor,
                )
                # Re-read state: init_project's returned snapshot predates the langid write.
                result["state"] = load_json(ProjectPaths(root, args.project_id).state)
            return result
        if pc == "set-image":
            return project.set_image(
                root, args.project_id, language=args.language,
                path=args.path, clear=args.clear, actor=args.actor,
            )
        if pc == "set-speed":
            return project.set_speed(
                root, args.project_id, language=args.language,
                factor=args.factor, actor=args.actor,
            )
        if pc == "add-languages":
            return project.add_languages(
                root, args.project_id,
                target_languages=parse_csv(args.targets),
                audio_languages=parse_csv(args.audio) if args.audio else None,
                actor=args.actor,
            )
        if pc == "enable-dub":
            return project.enable_dub(
                root, args.project_id,
                target_languages=parse_csv(args.targets),
                actor=args.actor,
                disable=args.disable,
                force=args.force,
            )
        if pc == "redub":
            return project.redub_track(
                root, args.project_id,
                target_languages=parse_csv(args.targets),
                actor=args.actor,
            )
        if pc == "sync-scope":
            return project.sync_scope(root, args.project_id, actor=args.actor)
        if pc == "autopilot":
            from . import autopilot as autopilot_mod
            return autopilot_mod.autopilot(
                root, args.project_id,
                mt=args.mt, until=args.until, dry_run=args.dry_run, actor=args.actor,
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
        if pc == "reset":
            keep_source = not (args.drop_source or args.full)
            keep_transcript = not (args.drop_transcript or args.full)
            return project.reset_project(
                root, args.project_id,
                keep_source=keep_source, keep_transcript=keep_transcript,
                to_state=args.to, actor=args.actor,
            )
        if pc == "delete":
            return project.delete_project(
                root, args.project_id,
                purge_catalog=not args.keep_catalog, actor=args.actor,
            )
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
    if cmd == "glossary":
        from . import glossary as glossary_mod
        if args.glossary_command == "list":
            return {"glossaries": glossary_mod.list_glossaries(root)}
    if cmd == "ingest":
        from . import ingest as ingest_mod
        if args.ingest_command == "run":
            return ingest_mod.ingest_project(
                root, args.project_id, actor=args.actor, force=args.force,
                write_subs=not args.no_subs, advance=not args.no_advance,
            )
        if args.ingest_command == "ensure":
            return ingest_mod.ensure_source_present(
                root, args.project_id, actor=args.actor, write_subs=not args.no_subs,
            )
    if cmd == "transcript":
        from . import transcript as transcript_mod
        tc = args.transcript_command
        if tc == "run":
            return transcript_mod.run_transcription(
                root, args.project_id, provider=args.provider, model=args.model,
                word_timestamps=not args.no_word_timestamps,
                condition_on_previous_text=not args.no_condition_on_previous_text,
                hallucination_silence_threshold=args.hallucination_silence_threshold,
                temperature=args.temperature,
                retry=not args.no_retry,
                actor=args.actor, advance=args.advance,
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
        if tc == "english-export":
            from . import english_gloss
            return english_gloss.export_gloss_worksheet(root, args.project_id, actor=args.actor)
        if tc == "reconcile-verses":
            from . import verses_gloss
            return verses_gloss.reconcile_verses_into_gloss(
                root, args.project_id, actor=args.actor,
            )
        if tc == "english-import":
            from . import english_gloss, verses_gloss
            # Always reconcile human verse overrides FIRST, then import the (possibly updated)
            # worksheet into the canonical gloss doc.
            reconciled = verses_gloss.reconcile_verses_into_gloss(
                root, args.project_id, actor=args.actor,
            )
            result = english_gloss.import_gloss_worksheet(
                root, args.project_id, from_path=args.from_path, actor=args.actor,
            )
            result["verses_reconciled"] = reconciled
            return result
    if cmd == "segments":
        from . import segments as segments_mod
        sc = args.segments_command
        if sc == "resolve":
            return segments_mod.run_resolve(
                root, args.project_id, actor=args.actor, advance=args.advance,
            )
        if sc == "cut":
            return segments_mod.run_cut(
                root, args.project_id, actor=args.actor, reencode=not args.no_reencode,
            )
        if sc in ("show", "list"):
            return segments_mod.load_segments(root, args.project_id)
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
        if tlc == "machine-fill":
            return translate_mod.machine_fill_worksheet(
                root, args.project_id, args.language, provider=args.provider,
                model=args.model, actor=args.actor,
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
                model=args.model, voice=args.voice, gender=args.gender, clone=args.clone,
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
                root, args.project_id, args.language, mode=args.mode,
                actor=args.actor, advance=args.advance,
            )
        if pc == "final-qa":
            return packaging_mod.run_final_qa(root, args.project_id, actor=args.actor)
        if pc == "build":
            return packaging_mod.run_package(
                root, args.project_id, actor=args.actor, advance=args.advance,
            )
    if cmd == "distribute":
        from . import distribution as distribution_mod
        dc = args.distribute_command
        if dc == "chapters":
            from . import chapters as chapters_mod
            cc = args.chapters_command
            if cc == "export":
                return chapters_mod.export_chapter_worksheet(
                    root, args.project_id, args.language, actor=args.actor)
            if cc == "import":
                return chapters_mod.import_chapter_worksheet(
                    root, args.project_id, args.language, from_path=args.from_path,
                    actor=args.actor)
        if dc == "package":
            return distribution_mod.run_platform_packaging(
                root, args.project_id, actor=args.actor, advance=args.advance)
        if dc == "youtube":
            return distribution_mod.run_youtube_upload(
                root, args.project_id, args.language, dry_run=not args.live,
                actor=args.actor, advance=args.advance)
        if dc == "promote":
            prc = args.promote_command
            if prc == "queue":
                return distribution_mod.run_promotion_queue(
                    root, args.project_id, actor=args.actor, advance=args.advance)
            if prc == "publish":
                return distribution_mod.run_promotion_publish(
                    root, args.project_id, dry_run=not args.live,
                    actor=args.actor, advance=args.advance)
        if dc == "package-show":
            return distribution_mod.load_platform_package(root, args.project_id)
        if dc == "upload-show":
            paths = ProjectPaths(root, args.project_id).require()
            return distribution_mod._load_upload_manifest(paths, args.project_id)
        if dc == "promotion-show":
            return distribution_mod.load_promotion_manifest(root, args.project_id)
    if cmd == "catalog":
        from . import catalog as catalog_mod
        if args.catalog_command == "list":
            return {"videos": [catalog_mod.enrich_entry(root, e)
                               for e in catalog_mod.list_entries(root)]}
        if args.catalog_command == "show":
            entry = catalog_mod.get_entry(root, args.video_id)
            if entry is None:
                raise VideoTranslationHouseError(f"No catalog entry: {args.video_id}")
            return catalog_mod.enrich_entry(root, entry)
        if args.catalog_command == "add-playlist":
            return catalog_mod.add_playlist(root, args.url, actor=args.actor)
        if args.catalog_command == "playlists":
            return {"playlists": catalog_mod.list_playlists(root)}
        if args.catalog_command == "playlist":
            pl = catalog_mod.get_playlist(root, args.playlist_id)
            if pl is None:
                raise VideoTranslationHouseError(f"No playlist: {args.playlist_id}")
            # list_entries() is sorted by video_id (catalog.upsert_entry's master-list
            # order) — reorder by pl["video_ids"] so the playlist's own sequence (yt-dlp's
            # enumeration order) is what's returned, not an alphabetized one.
            by_id = {e["video_id"]: e for e in catalog_mod.list_entries(root)
                     if e.get("playlist_id") == args.playlist_id}
            videos = [catalog_mod.enrich_entry(root, by_id[vid])
                      for vid in pl.get("video_ids", []) if vid in by_id]
            summary: dict[str, int] = {}
            for v in videos:
                summary[v["status"]] = summary.get(v["status"], 0) + 1
            return {**pl, "video_count": len(videos), "status_summary": summary, "videos": videos}
    if cmd == "budget":
        from . import budget as budget_mod
        if args.budget_command == "status":
            return budget_mod.status(root)
    if cmd == "langid":
        from . import langid as langid_mod
        if args.langid_command == "detect":
            paths = ProjectPaths(root, args.project_id).require()
            wav = paths.source_dir / "audio.wav"
            return langid_mod.detect_from_audio(root, wav)
        if args.langid_command == "set":
            return langid_mod.set_language(
                root, args.project_id, args.language, source=args.source,
                confidence=args.confidence, dialect=args.dialect,
                source_voice_gender=args.source_voice_gender, actor=args.actor,
            )
    if cmd == "event":
        if args.event_command == "tail":
            paths = ProjectPaths(root, args.project_id).require()
            return {"events": read_events(paths.events, tail=args.n)}
    raise VideoTranslationHouseError(f"Unhandled command: {cmd}")


def _subcommand_label(args: argparse.Namespace) -> str:
    """A compact 'group sub' label for logs/heartbeat, e.g. 'dub run', 'project autopilot'.

    Each command group stores its subcommand under a distinct dest (``project_command``,
    ``dub_command``, …); pick whichever is present so the label is meaningful without a lookup
    table. Falls back to the bare top-level command."""
    cmd = getattr(args, "command", None) or "?"
    for attr in vars(args):
        if attr.endswith("_command"):
            sub = getattr(args, attr)
            if sub:
                return f"{cmd} {sub}"
    return cmd


def main(argv: list[str] | None = None) -> int:
    from . import obs

    parser = build_parser()
    args = parser.parse_args(argv)
    label = _subcommand_label(args)
    project_id = getattr(args, "project_id", None)
    heartbeat: obs.Heartbeat | None = None
    started = time.monotonic()
    try:
        root = Path(args.root).expanduser().resolve() if args.root else repo_root()
        # Observability (pure instrumentation — never changes state/output). The logger prunes
        # logs older than 24h and rotates by size; the heartbeat prints liveness to stderr while a
        # long command runs. Both suppress their own errors; stdout stays pure JSON.
        obs.get_logger(root)
        obs.log_info("cli start", kind="start", cmd=label, project_id=project_id,
                     argv=(argv if argv is not None else sys.argv[1:]), pid=os.getpid())
        if obs.heartbeat_enabled():
            heartbeat = obs.Heartbeat(label, project_id, interval=obs.heartbeat_interval()).start()
            obs.set_current(heartbeat)

        result = dispatch(args, root)
        # `cmd`/`nextcmd` return a paste-ready terminal block (a plain string), not a dict —
        # print it raw so newlines/quotes survive for copy-paste (their whole purpose).
        if args.command in ("cmd", "nextcmd"):
            print(result)
            obs.log_info("cli done", kind="end", cmd=label, project_id=project_id, rc=0,
                         elapsed_s=round(time.monotonic() - started, 3))
            return 0
        indent = None if args.compact else 2
        print(json.dumps(result, indent=indent, ensure_ascii=False, default=str))
        rc = 1 if isinstance(result, dict) and (
            result.get("valid") is False or result.get("status") == "fail") else 0
        obs.log_info("cli done", kind="end", cmd=label, project_id=project_id, rc=rc,
                     elapsed_s=round(time.monotonic() - started, 3))
        return rc
    except (VideoTranslationHouseError, FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        obs.log_error("cli error", kind="end", cmd=label, project_id=project_id, rc=2,
                      error_type=type(exc).__name__, error=str(exc),
                      elapsed_s=round(time.monotonic() - started, 3))
        print(json.dumps({"status": "error", "error": str(exc), "error_type": type(exc).__name__}, indent=2),
              file=sys.stderr)
        return 2
    finally:
        obs.set_current(None)
        if heartbeat is not None:
            heartbeat.stop()


if __name__ == "__main__":
    raise SystemExit(main())
