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

## Stop / Escalate — HUMAN GATE (decide-not-operate, CLAUDE.md rule 13)
`final_qa` is human-bound: a grant records the **human's** decision, executed by you only after an
explicit confirmation. Never set rights or publish. Run it as a conversation:
1. **Surface + present options** (`AskUserQuestion`) — REVISE with the concrete fix per blocker —
   **plus an explicit `Approve & advance` option**, each with its consequence.
2. **On *approve*, disclose then confirm** (the recorded decision): echo the **gate** (`final_qa`),
   the **active** dubbed-video **SHA-256(s)** on disk (verify against the file(s)), the **relative
   path(s)** of the video(s), and any **concerns** worth a look, each briefly explained. Get one
   explicit confirmation; log it into `--notes` / a `reviews/` note.
3. **Then execute it yourself** (no `!` needed), flags verified with `--help`:
   ```bash
   vid approval grant <id> --gate final_qa --approver "<human>" --scope video \
       --artifact sha256:<active> --notes "<decision + disclosed concerns>"
   vid project transition <id> --to PACKAGE --actor human
   ```
   On REVISE, run the human-directed transition back to `VIDEO_MUX` (or earlier). Never grant/transition
   without the explicit confirmation (rule 2); never approve to unblock your own work.

## Completion contract
The `final` gate report reflects the current dubbed videos; every blocker has a concrete recommended
fix; a project-level APPROVE/REVISE recommendation with per-language evidence is presented; any
grant/transition performed by the agent was the execution of an explicit, disclosed human confirmation.
