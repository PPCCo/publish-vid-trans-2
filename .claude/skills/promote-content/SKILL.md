---
name: promote-content
description: >-
  Draft platform-specific promotional posts for a published video (distinct text per platform, no
  cross-post spam), pointing at the watch URL. Automatable platforms (X/Telegram/Discord) get
  API-ready messages; manual platforms become checklist items with a how-to guide. Covers
  PROMOTION_QUEUE and STOPS at the human promotion_review gate. NEVER posts.
allowed-tools: Bash, Read, Edit
argument-hint: <video-id>
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus1/us.anthropic.claude-sonnet-5"
effort: medium
---

# promote-content

## Goal
Produce `distribution/promotion-manifest.json`: one drafted post per configured platform, each
with **distinct** text (anti-spam guardrail) filled from that platform's template
(`{title}{url}{summary}{hashtags}`). Automatable+enabled platforms are `queued`; everything else
is a `checklist` item linked to its `docs/guides/*` how-to.

## Scope & boundaries (company rule 3)
- **No posting.** This skill only drafts and queues. Firing posts is a separate step behind the
  human `promotion_review` gate and the publish flags.
- **Distinct text per platform.** If two platforms would post identical text, the manifest flags
  it; differentiate the messages before publish.

## Procedure
1. Confirm state: `vid_cli.py project status <id>` (at/through `YOUTUBE_UPLOAD`). A recorded watch
   URL (from the upload manifest) is used as `{url}`; if none, the draft uses a placeholder.
2. **Queue drafts:** `vid_cli.py distribute promote queue <id>` (add `--advance` to attempt
   `PROMOTION_QUEUE -> PROMOTION_REVIEW`; the human gate holds it).
3. `Read` `distribution/promotion-manifest.json`. Improve each `message` for its platform's norms
   and length; ensure they differ. `Edit` the manifest text if needed, keeping it schema-valid,
   OR adjust `promotion.config.json` templates and re-queue. (Prefer re-queue so the CLI owns the
   write; only hand-tune the manifest when the CLI cannot express the change.)
4. **STOP at `promotion_review`.** Present the queued posts + checklist. A human approves (see
   `video-promote-approve`).

## Outputs
- `distribution/promotion-manifest.json` (registered `promotion`); `reviews/promotion-gate-latest.json`.

## Stop / Escalate — HUMAN GATE
`promotion_review` is human-bound. Never grant it; never post.

## Completion contract
The promotion manifest validates with one distinct post per platform, automatable platforms
`queued` and manual ones `checklist`; the run stopped at the promotion_review gate; nothing posted.
