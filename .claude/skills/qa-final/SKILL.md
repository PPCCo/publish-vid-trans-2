---
name: qa-final
description: >-
  Review dubbed videos before the final_qa human gate. Runs the deterministic final gate report
  (per-language stream presence + audio/video duration alignment), summarizes findings, and
  prepares an evidence-backed gate packet. Use when a project is at VIDEO_MUX or FINAL_QA_GATE.
allowed-tools: Bash, Read
argument-hint: <video-id>
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus1/us.anthropic.claude-sonnet-5"
effort: medium
---

# qa-final

## Goal
Give the human `final_qa` reviewer everything needed to approve or reject the muxed deliverables
in one pass: the deterministic mux findings per dub-enabled language (video stream present, audio
stream present, audio/picture duration aligned within tolerance), with evidence. `final_qa` is a
**single project-level** gate (not per-language).

## Preconditions
- Each dub-enabled track has `video/<iso>/dubbed.mp4` (`vid_cli.py package mux` has run).

## Procedure
1. Regenerate the aggregate report: `vid_cli.py package final-qa <id>`. Read the `decision` and
   every finding (grouped by language).
2. `Read reviews/final-gate-latest.json`. For each language review:
   - **mux-missing** (blocker): a dub-enabled track has no dubbed video — run `package mux`.
   - **mux-no-video / mux-no-audio** (blocker): the mux dropped a stream — re-mux; check the
     source video and the dub WAV exist and are non-empty.
   - **mux-av-misaligned** (blocker): audio ends materially far from the picture — the dub is
     longer/shorter than the video. Revisit the dub sync (back to `AUDIO_SYNC_ADJUST`), not the
     mux; a large `av_drift_ms` points at accumulated dub drift.
3. Spot-check: use `vid_cli.py` MCP `final_qa_preview` (or re-probe) to confirm the reported
   durations, and confirm the dubbed video's captions/dub are the ACTIVE (non-superseded)
   artifacts in `project status`.
4. Summarize a project-level recommendation (APPROVE / REVISE) with per-language evidence and the
   exact fix for each blocker.

## Outputs
- Refreshed `reviews/final-gate-latest.json` (decision read by the state machine).
- A written gate-packet summary for the human reviewer (in your response).

## Stop / Escalate — HUMAN GATE
This skill NEVER grants an approval or transitions the gate. `final_qa` is human-bound. Present
findings + recommendation and stop. On REVISE, the human directs a transition back to
`VIDEO_MUX` (or earlier); on APPROVE, the human runs the approval skill (bound to the
dubbed-video hashes) and authorizes `FINAL_QA_GATE -> PACKAGE`. Never set rights or publish.

## Completion contract
The `final` gate report reflects the current dubbed videos; every blocker has a concrete
recommended fix; a project-level APPROVE/REVISE recommendation with per-language evidence is
presented. No approval or transition performed by the agent.
