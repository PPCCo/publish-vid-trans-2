---
name: video-release-authorize
description: >-
  Human-only gate. Authorize the external release of a specific packaged video edition by
  granting the release_authorization approval bound to the exact final-video SHA-256. Claude may
  summarize the packet but may NEVER grant this approval or upload. Required before any YouTube
  upload (RELEASE_AUTHORIZATION -> YOUTUBE_UPLOAD).
allowed-tools: Read
argument-hint: <video-id> --language <iso>
user-invocable: true
disable-model-invocation: true
model: "@bedrock-eus1/us.anthropic.claude-haiku-4-5-20251001-v1:0"
effort: low
---

# video-release-authorize  (human-only gate)

## Goal
Record a **human's** decision to publish a specific video edition externally, by granting the
`release_authorization` approval bound to that edition's exact `dubbed-video@<lang>` hash. This
opens `RELEASE_AUTHORIZATION -> YOUTUBE_UPLOAD`.

## Why this is human-only (tighter than the QA gates)
Publishing to a public platform is irreversible and outward-facing. Deciding that a particular
rendered video — this exact hash — may leave the building is a human judgment (`rights` are also
re-checked on this edge). Unlike the editorial QA gates — where CLAUDE.md rule 13 now lets the
agent execute the grant *after* an explicit, disclosed human confirmation — this **outward-facing**
gate keeps the tighter posture: `disable-model-invocation: true` enforces it, so Claude may present
the packet and options, but only a person grants the approval (via the `!` block below).

## Preconditions
- The project is at `RELEASE_AUTHORIZATION` with a valid `distribution/platform-package.json`.
- `rights_status` is distributable (self-authored / licensed / fair-use-claimed) — the edge
  carries a `rights` re-check that blocks otherwise.

## What Claude MAY do (preparation only — decide-not-operate, CLAUDE.md rule 13)
- `Read` `distribution/platform-package.json` and the `platform-package` gate report and summarize
  each target: language, title, channel, privacy, and the bound `video_sha256`.
- **Present the release decision as selectable options** (`AskUserQuestion`) per edition —
  *authorize release* / *hold* / *revise the package first* — each with its consequence (publication
  is irreversible and outward-facing). Log the human's pick as the decision of record.
- **Assemble the paste-ready `!` block below** with the exact `video_sha256` filled in from the
  manifest and flags verified with `--help` (`approval grant` → `--approver`; `project transition` →
  `--actor`). It may NOT run `approval grant` or the transition itself.

## What only a HUMAN does
For each edition to release, run the prepared block (the `!` prefix executes it as you); use **backslash
line-continuations** so a long command never wraps and splits mid-flag (CLAUDE.md rule 13):
```
! vid_cli.py approval grant <id> --gate release_authorization --approver "<name>" \
    --scope release --artifact <video_sha256-or-path> --notes "<decision>" \
  && vid_cli.py project transition <id> --to YOUTUBE_UPLOAD --actor human --reason "release authorized"
```

## Completion contract
A `release_authorization` approval exists bound to the exact final-video hash, rights are
distributable, and the project is at `YOUTUBE_UPLOAD`. No approval or transition performed by an
agent.
