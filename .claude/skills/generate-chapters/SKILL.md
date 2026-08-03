---
name: generate-chapters
description: >-
  Define titled chapter breakpoints for a language edition from its closed captions, via a
  worksheet round-trip (the CLI never calls an LLM). Claude groups caption cues into 3-12 titled
  chapters; the CLI validates and registers them. Chapters feed the YouTube description timecode
  block and X/thread promo. Use once captions exist (CAPTION_VALIDATION or later).
allowed-tools: Bash, Read, Write, Edit
argument-hint: <video-id> --language <iso>
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus1/us.anthropic.claude-sonnet-5"
effort: medium
---

# generate-chapters

## Goal
Produce `chapters/chapters.<lang>.json`: an ordered list of `{start_ms, title}` markers that
segment the video into meaningful chapters, rendered later into a YouTube description timecode
block (first line `0:00`) and available for promotion threads.

## Why a worksheet (company rule 1 & 8)
Choosing chapter boundaries and writing titles is an editorial/LLM judgment; the deterministic
CLI must not make it. So the CLI exports a worksheet, **you** fill it, and the CLI validates and
registers the result — exactly like the translation round-trip.

## Procedure
1. Confirm captions exist: `vid_cli.py project status <id>`. The track must be at/through
   `CAPTION_VALIDATION`.
2. **Export the worksheet:**
   `vid_cli.py distribute chapters export <id> --language <iso>`
   → writes `chapters/<lang>.chapters-worksheet.json` (the full cue list + heuristic
   `candidate_boundaries` at long pauses + an empty `chapters` list + instructions).
3. **`Read` the worksheet.** Group the cues into **3-12 chapters**. For each chapter write a
   `{start_ms, title}` row into the `chapters` list, obeying:
   - the **first** chapter MUST have `start_ms: 0`;
   - `start_ms` values strictly increasing, each aligned to a cue's `start_ms`, all `< duration_ms`;
   - titles concise (≈ ≤ 40 chars), descriptive, in the caption language.
   Refine the `candidate_boundaries` rather than starting blank. `Edit`/`Write` the worksheet.
4. **Import & validate:**
   `vid_cli.py distribute chapters import <id> --language <iso>`
   → validates boundaries, writes `chapters/chapters.<lang>.json`, registers `chapters@<lang>`.
   Fix any validation error (monotonic / first-at-0 / within-duration) and re-import.

## Outputs
- `chapters/chapters.<lang>.json` (registered `chapters@<lang>`).
- Events `CHAPTERS_WORKSHEET_EXPORTED`, `CHAPTERS_IMPORTED`.

## Boundaries
- No state transition, no approval, no publication. Chapters are an input to platform packaging.
- Editing an imported chapters file later re-registers it and invalidates any binding approval.

## Completion contract
`chapters/chapters.<lang>.json` validates against `chapters.schema.json` with 3-12 monotonic
chapters starting at 0. Nothing else mutated.
