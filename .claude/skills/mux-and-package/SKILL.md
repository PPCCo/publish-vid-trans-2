---
name: mux-and-package
description: >-
  Mux each dub-enabled language's dub over the source video, then assemble upload-ready
  deliverable packages (dubbed video + captions + README/manifest with checksums). Covers
  VIDEO_MUX -> PACKAGE. Stops at the human final_qa gate and at the rights-blocked
  PACKAGE -> READY_FOR_REVIEW edge; NEVER publishes. Use when a project is at AUDIO_QA_GATE
  (approved) or beyond.
allowed-tools: Bash, Read
argument-hint: <video-id> [--language <iso>]
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus1/us.anthropic.claude-haiku-4-5-20251001-v1:0"
effort: low
---

# mux-and-package

## Goal
Turn the approved dubs into per-language deliverables: `video/<lang>/dubbed.mp4` (source picture
+ that language's dub, optional soft-subs) and `packages/<lang>/` (dubbed video, captions
SRT/VTT, README with a rights banner + checksums), indexed by `packages/package-manifest.json`.
The pipeline **ends at READY_FOR_REVIEW** — nothing is uploaded or published.

## Scope & boundaries (company rules 2, 3, 4)
- **No publication.** This skill only produces packages under `packages/`. It never uploads to
  any platform and never writes `outputs/`. External distribution is a separate, human-authorized
  step (Phase 6) and out of scope here.
- **Rights precede distribution.** `PACKAGE -> READY_FOR_REVIEW` is blocked until a human sets a
  distributable `rights_status`. Until then, every package README carries a **NOT CLEARED FOR
  DISTRIBUTION** banner. Do not set rights yourself — that is a human-only action.
- Mux runs as an **ffmpeg subprocess**; the CLI never imports an ML library. The source picture
  is copied bit-for-bit (no re-encode); only the dub is encoded (AAC) — **except** in freeze-frame
  mode (below), where the picture is re-encoded to absorb the freezes.

## Freeze-frame mode (when `freeze_frame_enabled` is on)
If the dub was produced with `quality_bars.audio.freeze_frame_enabled: true` (opt-in via
`company.local.json`), `dub run` wrote a per-language freeze plan (`audio/freeze-plan.<lang>.json`)
recording how the picture is re-timed to the audio: running-gap **holds**, plus — for languages in
`quality_bars.audio.freeze_trim_languages` (Model A) — picture **trims** (dropped source regions).
When an **active** freeze plan exists for a language, `package mux` **rebuilds the picture on the
post-adjust timeline** (source slices interleaved with frozen-frame inserts and with trimmed regions
dropped, re-encoded H.264/AAC crf18 — not `-c:v copy`) and re-times **both** the embedded soft-subs
and, at `package build`, the standalone `captions.<lang>.vtt/.srt` deliverables onto that timeline
so they line up with the dubbed audio.
The **canonical approved caption doc is never edited** (rules 6/12) — retimed subs are derived
mux/package outputs. No new command or flag: mux/build detect the active plan automatically.
Turning the bar back off cleanly reverts future muxes to the plain `-c:v copy` path (no artifact
deleted). See CLAUDE.md rule 14 / OPERATING-GUIDE §9.

## Preconditions
- The project is at (or through) `AUDIO_QA_GATE` with the per-language `audio_qa` approvals
  granted, i.e. the human has authorized `AUDIO_QA_GATE -> VIDEO_MUX`.
- Each dub-enabled track has an active `dub-wav@<lang>` artifact and rendered captions
  (`captions.<lang>.srt` + `.vtt`; run `captions build` if missing).

## Procedure
1. Confirm readiness: `vid_cli.py project status <id>` and `vid_cli.py project plan <id>`
   (`autonomy_action`). Identify dub-enabled tracks (`language_tracks[*].dub_enabled`).
2. **Mux** each dub-enabled language: `vid_cli.py package mux <id> --language <iso>`
   (add `--no-subs` to omit the soft-subtitle track). Pass `--advance` on the last track to move
   the top state `AUDIO_QA_GATE -> VIDEO_MUX` once every dub track is muxed.
3. **Final QA:** `vid_cli.py package final-qa <id>`. Read the `decision` and findings
   (stream presence, A/V alignment). On FAIL, re-mux or fix the offending dub; do not proceed.
4. **STOP at the `final_qa` human gate** (see below). Only after a human approves and authorizes
   `FINAL_QA_GATE -> PACKAGE`:
5. **Package:** `vid_cli.py package build <id> --advance`. This writes `packages/<lang>/` and
   `packages/package-manifest.json`. `Read` the manifest and confirm `distributable` +
   `rights_status`.
6. **STOP at the rights gate.** `PACKAGE -> READY_FOR_REVIEW` will be blocked while rights are
   `unreviewed`/`do-not-distribute`. Report that a human must run `rights set` (and the
   rights-check skill) to open it. Do not attempt the transition yourself if blocked.

## Outputs
- `video/<lang>/dubbed.mp4` per dub-enabled track (registered `dubbed-video@<lang>`).
- `packages/<lang>/` + `packages/package-manifest.json` (registered `package@<lang>`,
  `package-manifest`).
- `reviews/final-gate-latest.json` (decision read by the state machine).

## Stop / Escalate — HUMAN GATES
- `final_qa` is human-bound (single project-level approval). Present the final-QA findings and a
  recommendation, then stop. The human grants the approval (bound to the dubbed-video hashes) and
  authorizes `FINAL_QA_GATE -> PACKAGE`.
- The rights gate on `PACKAGE -> READY_FOR_REVIEW` is human-bound. Never set rights or force the
  transition.

## Completion contract
Dubbed videos exist and pass final QA; packages and the manifest are built with correct rights
banners; the run stopped at the `final_qa` gate and (if rights not cleared) at the rights gate.
No approval, rights change, or gate transition performed by the agent. Nothing published.
