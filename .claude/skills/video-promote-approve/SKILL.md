---
name: video-promote-approve
description: >-
  Human-only gate. Approve the queued promotional posts for a project by granting the
  promotion_review approval, opening PROMOTION_REVIEW -> PROMOTION_PUBLISHED. Claude may summarize
  the queued posts but may NEVER grant this approval or post. After approval, posting still
  requires the publish flags and runs dry by default.
allowed-tools: Read
argument-hint: <video-id>
user-invocable: true
disable-model-invocation: true
model: "@bedrock-eus1/us.anthropic.claude-haiku-4-5-20251001-v1:0"
effort: low
---

# video-promote-approve  (human-only gate)

## Goal
Record a **human's** approval of the drafted promotional posts, granting `promotion_review` so
the automatable posts may be fired (`PROMOTION_REVIEW -> PROMOTION_PUBLISHED`).

## Why this is human-only (tighter than the QA gates)
Promotional posts are outward-facing publications tied to the org's accounts. A human must read
the exact text going to each platform before it is sent. Unlike the editorial QA gates — where
CLAUDE.md rule 13 now lets the agent execute the grant *after* an explicit, disclosed human
confirmation — this **outward-facing** gate keeps the tighter posture: `disable-model-invocation:
true` enforces that Claude cannot approve its own drafts; a person runs the `!` block below.

## Preconditions
- The project is at `PROMOTION_REVIEW` with a valid `distribution/promotion-manifest.json`.

## What Claude MAY do (preparation only — decide-not-operate, CLAUDE.md rule 13)
- `Read` `distribution/promotion-manifest.json` and summarize each queued post's platform and
  text, flag any duplicate-text warnings, and list the manual checklist items.
- **Present the approval decision as selectable options** (`AskUserQuestion`) — *approve the queued
  posts* / *hold* / *revise a post first* — each with its consequence (posts go to org accounts).
  Log the human's pick as the decision of record.
- **Assemble the paste-ready `!` block below** with the manifest hash filled in from the manifest and
  flags verified with `--help`. It may NOT run `approval grant` and may NOT publish.

## What only a HUMAN does
Run the prepared block (the `!` prefix executes it as you); use **backslash line-continuations** so a
long command never wraps and splits mid-flag (CLAUDE.md rule 13):
```
! vid_cli.py approval grant <id> --gate promotion_review --approver "<name>" \
    --scope promotion --artifact <promotion-manifest-hash-or-path> --notes "<decision>" \
  && vid_cli.py project transition <id> --to PROMOTION_PUBLISHED --actor human --reason "posts approved"
```
Posting then runs via `distribute promote publish` — **dry by default**; a live post needs
`VIDTRANS_PUBLISH_ENABLED=1` + `VIDTRANS_EXTERNAL_WRITES=enabled` and per-platform credentials.

## Completion contract
A `promotion_review` approval exists and the project is at `PROMOTION_PUBLISHED`. No approval,
transition, or post performed by an agent.
