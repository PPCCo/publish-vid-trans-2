---
name: qa-caption-sync
description: >-
  Review rendered closed captions for readability and timing before advancing past
  CAPTION_VALIDATION. Runs the deterministic caption validator, summarizes reading-speed /
  duration / line-geometry findings per language, and recommends fixes. Use when a project
  is at CAPTION_TIMING or CAPTION_VALIDATION.
allowed-tools: Bash, Read
argument-hint: <video-id> [--language <iso>]
user-invocable: true
disable-model-invocation: false
---

# qa-caption-sync

## Goal
Confirm the rendered captions are comfortable to read and correctly timed for every active
language track: reading speed within budget, cues not too short/long, lines within the
script-class character limit and count. CJK is measured in characters; RTL/Latin/Cyrillic in
words. The `caption` gate report's decision is read by the state machine.

## Preconditions
- Canonical captions exist at `captions/captions.<iso>.json`, and SRT/VTT have been built
  (`vid_cli.py captions build <id> --language <iso>`).

## Procedure
1. Regenerate the aggregate report: `vid_cli.py captions validate <id>`. Read `decision` and
   every finding (grouped by language).
2. For each finding:
   - **reading-speed** (major): the cue shows too many words/characters for its duration.
     Fix by tightening the translation or, where timing allows, splitting is NOT an option
     (timing is fixed) — prefer a more concise rendering.
   - **duration** (minor): very short (<1s) or very long (>7s) cues — usually a source
     segmentation artifact; note for the reviewer.
   - **line-length / line-count** (minor): the cue wraps too wide or to >2 lines. Fix by
     setting an explicit `lines` array in `captions.<iso>.json`, then re-build.
3. `Read` the rendered `captions/captions.<iso>.srt` for a couple of flagged cues to confirm
   the wrapping reads naturally.
4. Summarize a per-language recommendation (READY / NEEDS-WORK) with cue-referenced evidence
   and the exact edit for each fix.

## Outputs
- Refreshed `reviews/caption-gate-latest.json` (decision read by the state machine).
- A written readability summary per language (in your response).

## Stop / Escalate
This skill does not grant approvals. `CAPTION_VALIDATION` advances on a PASS `caption` report
(deterministic) plus the human-authorized transition. If the decision is `CONDITIONAL_PASS`
or `FAIL`, list the specific cue fixes; after fixes, re-build and re-validate before the
human advances the state.

## Completion contract
The `caption` gate report reflects the current rendered captions; every readability finding
has a concrete recommended fix; a per-language READY/NEEDS-WORK summary is presented. No
state transition performed by the agent.
