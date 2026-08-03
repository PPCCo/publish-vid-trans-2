---
name: qa-audio-sync
description: >-
  Review dubbed audio sync before the per-language audio_qa human gate. Runs the deterministic
  audio-sync report, summarizes per-cue drift, over-cap stretches, and cumulative offset per
  language, and prepares an evidence-backed gate packet. Use when a project (or a track) is at
  AUDIO_SYNC_ADJUST or AUDIO_QA_GATE.
allowed-tools: Bash, Read
argument-hint: <video-id> [--language <iso>]
user-invocable: true
disable-model-invocation: false
---

# qa-audio-sync

## Goal
Give the human `audio_qa` reviewer everything needed to approve or reject each language's dub
in one pass: the deterministic sync findings (cumulative drift vs the ceiling, per-cue drift vs
tolerance, cues whose fit demanded a stretch beyond the cap), with cue-referenced evidence. The
`audio_qa` gate is **per-language** — one approval per dub-enabled track.

## Preconditions
- Each dub-enabled track has `audio/<iso>/dub.wav` and a block in `audio/sync-report.json`
  (`vid_cli.py dub run|import` has run).

## Procedure
1. Regenerate the aggregate report: `vid_cli.py dub qa <id>`. Read the `decision` and every
   finding (grouped by language).
2. `Read audio/sync-report.json`. For each language block, review:
   - **audio-cumulative-drift** (blocker): `max_abs_drift_ms` exceeds the cumulative ceiling —
     the dub loses sync unrecoverably. Direct a re-time/re-segment; this FAILs the gate.
   - **audio-stretch-over-cap** (major): a cue needed a tempo stretch past the cap; it was
     clamped, so it lags. Fix by tightening that cue's translation (shorter rendering) or
     splitting the source segment, then re-render.
   - **audio-drift-over-tolerance** (minor): cue drifts past per-cue tolerance — note for the
     reviewer; not blocking on its own.
3. Spot-check: `Read` a couple of flagged cues in `captions/captions.<iso>.json` and confirm the
   translation is genuinely too long for its slot (vs. an engine artifact).
4. Summarize a per-language recommendation (APPROVE / REVISE) with cue-referenced evidence and
   the exact fix for each blocker/major finding.

## Outputs
- Refreshed `reviews/audio-sync-gate-latest.json` (decision read by the state machine).
- A written per-language gate-packet summary for the human reviewer (in your response).

## Stop / Escalate — HUMAN GATE
This skill NEVER grants an approval or transitions the gate. `audio_qa` is human-bound and
per-language. Present findings + recommendation and stop. On REVISE, the human directs a
transition back to `AUDIO_SYNC_ADJUST` (or `DUBBING`); on APPROVE, the human runs the approval
skill (bound to that language's `dub-wav` hash) and then authorizes
`AUDIO_QA_GATE -> VIDEO_MUX`.

## Completion contract
The `audio-sync` gate report reflects the current dubs; every blocker/major finding has a
concrete recommended fix; a per-language APPROVE/REVISE recommendation with cue evidence is
presented. No approval or transition performed by the agent.
