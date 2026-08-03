---
name: detect-language
description: >-
  Confirm or set the source language of a project's video. Runs a cheap automated detection
  pass when an ASR adapter is installed; otherwise presents the metadata hint and asks the
  human to confirm. Use when a project is at LANGUAGE_ID before transcription.
allowed-tools: Bash, Read
argument-hint: <video-id> [--language <iso>]
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus1/us.anthropic.claude-haiku-4-5-20251001-v1:0"
effort: medium
---

# detect-language

## Goal
Record a confident `source_language` on the project so transcription runs with the right model.
Language ID is intentionally **human-overridable and non-blocking**: Persian/Arabic/Urdu are
genuinely confusable at the LID level (shared script, Quranic Arabic embedded in Persian/Urdu
religious speech), so a human confirmation is expected, not a failure mode.

## Args
- `$1` — `video-id` (project at `LANGUAGE_ID`).
- `--language <iso>` — optional explicit override (e.g. `fa`, `ar`, `ur`, `en`).

## Procedure
1. Read the metadata hint: `vid_cli.py project status <id>` and
   `Read projects/<id>/source/metadata.json` → note `detected_language` / `language_source`.
2. Attempt automated detection: `vid_cli.py langid detect <id>`.
   - If `available: false` (no verified ASR engine yet), fall back to the metadata hint and to
     human judgment. Do NOT block the pipeline waiting for an engine.
3. Decide the language:
   - If a `--language` override was given, use it.
   - Else if automated detection returned a confident result, use it.
   - Else propose the metadata hint (or your best judgment from the title/channel) and confirm
     with the human before setting.
4. Record it: `vid_cli.py langid set <id> --language <iso> --source <manual|auto-detect|youtube-metadata> [--confidence <0-1>] [--dialect <tag>]`.
   Capture dialect when known (MSA vs Gulf/Egyptian/Levantine Arabic; Iranian Persian vs Dari) —
   it routes dialectal segments to higher-quality translation later.

## Outputs
- `state.json.source_language` set; `source/metadata.json` language fields updated;
  catalog entry language reflected; `SOURCE_LANGUAGE_SET` event.

## Stop / Escalate
- If the audio plausibly mixes languages (code-switching, embedded recitation), set the primary
  language and record a `--dialect`/note; flag the mixing for the transcription QA gate.

## Completion contract
`source_language` is a known ISO 639-1 code recorded with its source and (when relevant) dialect.
The project remains at `LANGUAGE_ID`, ready for a human/agent to advance to `TRANSCRIPTION`.
