---
name: rights-check
description: >-
  Record the human-reviewed rights posture for a source video (license/ToS/fair-use stance and
  voice-cloning consent). This is a HUMAN-ONLY decision — Claude may prepare an evidence packet
  but may never set the rights status. Required before any output can be packaged for distribution.
allowed-tools: Read
argument-hint: <video-id>
user-invocable: true
disable-model-invocation: true
model: "@bedrock-eus1/us.anthropic.claude-haiku-4-5-20251001-v1:0"
effort: low
---

# rights-check  (human-only gate)

## Goal
Produce a `rights/record.json` reflecting a **human's** judgment about whether a translated /
dubbed derivative of this source video may be distributed, and whether the source speaker's
voice may be cloned. The pipeline runs to `READY_FOR_REVIEW` regardless, but
`PACKAGE → READY_FOR_REVIEW` is blocked while `rights_status ∈ {unreviewed, do-not-distribute}`.

## Why this is human-only
"Downloadable via yt-dlp" is not "cleared for redistribution." Assessing copyright, platform
ToS, fair-use posture, and a real person's likeness/consent are legal and ethical judgments a
model must not make autonomously. `disable-model-invocation: true` enforces that: Claude can
gather evidence, but only a person runs `vid_cli.py rights set`.

## What Claude MAY do (preparation only)
- Read `source/metadata.json` and `catalog/videos.json` to summarize uploader, channel, license
  field, and any stated reuse terms into `rights/evidence/` for the human to review.
- Note whether YouTube-provided captions exist and the video's stated license, if any.
- Draft, but not submit, the recommended `rights_status`.

## What only a HUMAN does
Set the record:
```
vid_cli.py rights set <id> \
  --status <self-authored|licensed|fair-use-claimed|do-not-distribute> \
  --reviewer "<name>" \
  [--voice-clone-consent] [--evidence path1,path2] [--notes "..."]
```
- `--voice-clone-consent` may be set **only** with recorded consent from the rights holder;
  default dubbing uses a neutral, non-cloned voice.
- Use `do-not-distribute` when in doubt — it is the safe default and does not stop the pipeline
  from producing review-only outputs.

## Completion contract
`rights/record.json` validates against `rights-record.schema.json`, names a human reviewer, and
carries a deliberate `rights_status`. A `RIGHTS_SET` event is recorded.
