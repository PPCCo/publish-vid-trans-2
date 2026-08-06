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

## Why this is human-only (tighter than the QA gates)
"Downloadable via yt-dlp" is not "cleared for redistribution." Assessing copyright, platform
ToS, fair-use posture, and a real person's likeness/consent are legal and ethical judgments a
model must not make autonomously. Unlike the editorial QA gates — where CLAUDE.md rule 13 now
lets the agent execute the grant *after* an explicit, disclosed human confirmation — this is an
**outward-facing / legal** gate and keeps the tighter posture: `disable-model-invocation: true`
enforces that Claude can gather evidence and present options, but only a person runs
`vid_cli.py rights set` (via the `!` block below).

## What Claude MAY do (preparation only — decide-not-operate, CLAUDE.md rule 13)
- Read `source/metadata.json` and `catalog/videos.json` to summarize uploader, channel, license
  field, and any stated reuse terms into `rights/evidence/` for the human to review.
- Note whether YouTube-provided captions exist and the video's stated license, if any.
- **Present the rights decision as selectable options** (`AskUserQuestion`) — the four
  `rights_status` values (*self-authored* / *licensed* / *fair-use-claimed* / *do-not-distribute*)
  plus whether voice-clone consent is on record — each with its consequence, recommending the safe
  default (`do-not-distribute`) when evidence is thin. Log the human's pick as the decision of record.
- **Assemble the paste-ready `!` block below** with the chosen status/flags and `--reviewer` filled
  in and verified with `--help`. It may NOT run `rights set` itself.

## What only a HUMAN does
Run the prepared block (the `!` prefix executes it as you):
```
! vid_cli.py rights set <id> \
    --status <self-authored|licensed|fair-use-claimed|do-not-distribute> \
    --reviewer "<name>" \
    [--voice-clone-consent] [--evidence path1,path2] [--notes "<decision>"]
```
- `--voice-clone-consent` may be set **only** with recorded consent from the rights holder;
  default dubbing uses a neutral, non-cloned voice.
- Use `do-not-distribute` when in doubt — it is the safe default and does not stop the pipeline
  from producing review-only outputs.

## Completion contract
`rights/record.json` validates against `rights-record.schema.json`, names a human reviewer, and
carries a deliberate `rights_status`. A `RIGHTS_SET` event is recorded.
