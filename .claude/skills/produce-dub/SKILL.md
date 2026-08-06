---
name: produce-dub
description: >-
  Produce a dubbed audio track for each dub-enabled language of a project: synthesize per-cue
  TTS from captions.<lang>.json, tempo-fit to the caption timing, loudness-normalize, and
  write the sync report (DUBBING -> AUDIO_SYNC_ADJUST). Runs the TTS adapter or imports a
  pre-rendered dub, then stops at the per-language audio_qa human gate. Use when a project is
  at CAPTION_VALIDATION (or a track is ready to dub).
allowed-tools: Bash, Read
argument-hint: <video-id> [--language <iso>] [--provider <tts>] [--voice <name>]
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus1/us.anthropic.claude-haiku-4-5-20251001-v1:0"
effort: low
---

# produce-dub

## Goal
Turn each dub-enabled language's canonical `captions/captions.<lang>.json` into a single timed
dub track `audio/<lang>/dub.wav`, and produce `audio/sync-report.json` measuring how well the
synthesized audio fits the caption timing (the downstream contract). Caption-only tracks
(`dub_enabled: false`) are skipped — they go straight to PACKAGE.

## Voice & consent (company rule 5)
The **default voice is neutral**. Cloning the source speaker's voice is only permitted when a
human has recorded consent: `vid_cli.py rights check <id>` must show `voice_clone_consent:
true`. `dub run --clone` refuses otherwise — do not attempt to work around it. Setting consent
is a human-only action (`rights set --voice-clone-consent`); never grant it yourself.

## Model / engine note
TTS runs as a **subprocess** through the adapter — the CLI never imports an ML library. The
per-language engine default lives in `tools.default.json` (`tts.per_language`: en→kokoro,
fa/ar/ur→piper, others→xtts). No engine is required to exercise the pipeline: if none is
installed, dub out-of-band and use `dub import`.

## Preconditions
- The track's `stage` is `CAPTION_VALIDATION` and its `captions/captions.<iso>.json` exists.
- The track is `dub_enabled` (`vid_cli.py project status <id>` → `language_tracks`).

## Incremental dubbing ("add es dubbing for <id>")
A dub reuses the already-produced `captions/captions.<iso>.json` — a **non-clone dub has no
source-video dependency**, so you can turn dubbing on for a translate-only language at any point:
- Track already exists (translated, just not dubbed): `vid_cli.py project enable-dub <id>
  --targets <iso>` flips `dub_enabled` (idempotent), then dub as below.
- No track yet: `vid_cli.py project add-languages <id> --targets <iso>` first (creates the
  translate track), then `enable-dub`.
- If the project has already advanced past `AUDIO_QA_GATE`, rewind with
  `vid_cli.py project reset <id> --to CAPTION_VALIDATION` (keeps source+transcript+captions)
  rather than forcing the dub outside its allowed states.
- `--clone` still needs recorded consent AND the source video/WAV. If the source was deleted to
  save space, `dub run --clone` (and `package mux`) transparently re-fetch it via
  `ingest ensure` — which is flag-gated, so `VIDTRANS_FETCH_ENABLED=1` must be set (else clean
  `FetchDisabled`). A neutral-voice dub never needs the source.

## Procedure (once per dub-enabled language)
1. Confirm readiness: `vid_cli.py project status <id>` (track at CAPTION_VALIDATION, dub_enabled).
2. Check for a TTS engine: `vid_cli.py doctor` → look for `engine:kokoro` / `engine:piper` /
   `engine:TTS` = installed, and `config:tts-provider`.
3. Synthesize:
   - Engine present: `vid_cli.py dub run <id> --language <iso> [--provider piper] [--voice <name>]`.
     Per cue the adapter synthesizes, the CLI tempo-fits into the cue slot up to the configured
     stretch cap (default 1.3×). Beyond the cap it does **not** force an unnatural stretch — it
     clamps, flags the cue, and lets drift accrue (surfaced in the sync report).
   - **Freeze-frame mode** (`quality_bars.audio.freeze_frame_enabled: true`, opt-in via
     `company.local.json` — no CLI flag): `dub run` instead fits audio only to the gentle
     `freeze_stretch_cap` (default 1.15×) and writes a per-language freeze plan
     (`audio/freeze-plan.<iso>.json`, registered, traces to the dub-wav). The plan re-times the
     picture on a **running gap** (the dub is laid back-to-back, so a long cue propagates), holding
     the frame at each cue boundary where the audio is later than the picture. **Two modes** per
     `quality_bars.audio.freeze_trim_languages`: languages *not* in the list are **hold-only**
     (Model B — an honest one-sided residual may remain; cover it with a raised
     `per_cue_drift_tolerance_ms`); languages *in* the list additionally **trim** the picture
     (Model A — residual 0 by construction, dropping some source frames). Over-slot cues drop out of
     the over-cap list *by construction*, so the track reaches PASS honestly and the picture is
     re-timed to the audio at `package mux`. Use this when the neutral voice runs slower than the
     source instead of cranking `max_time_stretch`. Scope limit: freeze-planning is the real-TTS
     `dub run` path only — `dub import` produces no freeze plan (`freeze_plan_skipped_reason`).
     See CLAUDE.md rule 14 / OPERATING-GUIDE §9.
   - No engine installed: render out-of-band, then
     `vid_cli.py dub import <id> --language <iso> --from <dub.wav>`. Do NOT hand-edit the WAV
     or `sync-report.json`.
4. Read the sync report: `vid_cli.py dub qa <id>` (aggregate) or inspect `audio/sync-report.json`.
   Note per-cue drift, `cues_over_stretch_cap`, and `cumulative_offset_ms`.
5. If cues exceed the stretch cap or the cumulative ceiling, tighten those cues' translations
   (shorter rendering) back in `captions.<iso>.json`, re-import/re-translate, re-render, and
   re-run `dub qa`. Then STOP at the human gate.

## Outputs
- `audio/<iso>/dub.wav` (registered `dub-wav@<iso>`, EBU R128 −16 LUFS).
- `audio/sync-report.json` (registered `sync-report`; per-language cue drift/stretch blocks).
- `reviews/audio-sync-gate-latest.json` (aggregate gate report, decision read by the state machine).
- Events: `DUB_RENDERED` / `DUB_IMPORTED`, `AUDIO_SYNC_REPORTED`.

## Stop / Escalate — HUMAN GATE (per language)
`AUDIO_QA_GATE -> VIDEO_MUX` is human-bound and **per-language**: each dub-enabled track needs
its own `audio_qa` approval bound to that language's `dub-wav` hash. You may prepare the packet
and recommend, but never grant the approval or transition the gate. Use `/qa-audio-sync` to
build the reviewer packet.

## Completion contract
Every dub-enabled track has a registered `audio/<iso>/dub.wav` and a sync block in
`audio/sync-report.json`; the aggregate `audio-sync` gate report carries an explicit decision;
each dub track rests at its human `audio_qa` gate. Voice cloning was used only with recorded
consent. No gate transition has been attempted by the agent.
