---
name: qa-transcript
description: >-
  Second-pass quality review of a source-language transcript before the transcript_qa
  human gate. Runs the deterministic QA report, then adds a careful human/agent listen-back
  on the flagged cues, preparing an evidence-backed gate packet. Use when a project is at
  TRANSCRIPT_QA_GATE.
allowed-tools: Bash, Read
argument-hint: <video-id> [--language <iso>]
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus1/us.anthropic.claude-sonnet-5"
effort: high
---

# qa-transcript

## Goal
Give the human gate reviewer everything they need to approve or reject the transcript in
one pass: the deterministic QA findings, plus a focused second-pass review of the cues the
machine flagged (low confidence, silence/music, timing anomalies, sensitive terms).

## Preconditions
- Project state is `TRANSCRIPT_QA_GATE`.
- A transcript exists at `transcript/source.<lang>.json`.

## Procedure
1. Regenerate the deterministic report (idempotent): `vid_cli.py transcript qa <id>`.
   Read `decision`, `metrics`, and every `finding`.
2. `Read transcript/source.<lang>.json` and `Read source/metadata.json` (duration, dialect).
3. For each flagged cue, do a second-pass check:
   - **timing** findings (blocker/minor): confirm the cue's start/end against the audio;
     these gate a FAIL if the timeline is corrupt.
   - **confidence** findings: judge whether the text is plausibly correct for the
     dialect; note likely mistranscriptions with the corrected text as evidence.
   - **non-speech / silence**: confirm whether real speech was dropped vs. music/applause.
   - **sensitive-religious / sensitive-political**: verify the transliteration/term is
     rendered accurately; flag anything whose *translation* will need editorial care.
4. Summarize a recommendation (APPROVE / REVISE) with a short, cue-referenced rationale.
   If REVISE, list the specific fixes (re-transcribe with a larger model, correct N cues,
   re-slice a silence gap).

## Outputs
- Refreshed `reviews/transcript-qa-gate-latest.json` (decision read by the state machine).
- A written gate-packet summary for the human reviewer (in your response, not a file the
  agent commits as an approval).

## Stop / Escalate — HUMAN GATE
This skill NEVER grants the approval or transitions the gate. `transcript_qa` is
human-bound. Present findings + recommendation and stop. On REVISE, the human directs a
transition back to `TRANSCRIPTION`; on APPROVE, the human runs the approval skill and then
authorizes `TRANSCRIPT_QA_GATE -> TRANSLATION`.

## Completion contract
The gate report reflects the current transcript, every machine-flagged cue has a
second-pass note, and a clear APPROVE/REVISE recommendation with evidence is presented for
the human. No approval or transition performed by the agent.
