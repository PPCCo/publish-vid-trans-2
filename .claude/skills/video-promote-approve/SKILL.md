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
---

# video-promote-approve  (human-only gate)

## Goal
Record a **human's** approval of the drafted promotional posts, granting `promotion_review` so
the automatable posts may be fired (`PROMOTION_REVIEW -> PROMOTION_PUBLISHED`).

## Why this is human-only
Promotional posts are outward-facing publications tied to the org's accounts. A human must read
the exact text going to each platform before it is sent. `disable-model-invocation: true`
enforces that Claude cannot approve its own drafts.

## Preconditions
- The project is at `PROMOTION_REVIEW` with a valid `distribution/promotion-manifest.json`.

## What Claude MAY do (preparation only)
- `Read` `distribution/promotion-manifest.json` and summarize each queued post's platform and
  text, flag any duplicate-text warnings, and list the manual checklist items.
- It may NOT run `approval grant` and may NOT publish.

## What only a HUMAN does
Grant the approval and authorize the transition:
```
vid_cli.py approval grant <id> \
  --gate promotion_review \
  --approver "<name>" \
  --artifact <promotion-manifest-hash-or-path> \
  --scope promotion

vid_cli.py project transition <id> --to PROMOTION_PUBLISHED --actor human --reason "posts approved"
```
Posting then runs via `distribute promote publish` — **dry by default**; a live post needs
`VIDTRANS_PUBLISH_ENABLED=1` + `VIDTRANS_EXTERNAL_WRITES=enabled` and per-platform credentials.

## Completion contract
A `promotion_review` approval exists and the project is at `PROMOTION_PUBLISHED`. No approval,
transition, or post performed by an agent.
