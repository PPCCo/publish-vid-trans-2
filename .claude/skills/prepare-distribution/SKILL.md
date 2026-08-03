---
name: prepare-distribution
description: >-
  Build the per-platform upload packet for a review-ready project: templated title/description
  (with chapter timecodes), tags, resolved channel, and the exact final-video hash each upload
  binds to, plus a per-project how-to guide. Covers READY_FOR_REVIEW -> PLATFORM_PACKAGING and
  STOPS at the human release_authorization gate. NEVER uploads. Use when a project is at
  READY_FOR_REVIEW and a human wants to prepare distribution.
allowed-tools: Bash, Read
argument-hint: <video-id>
user-invocable: true
disable-model-invocation: false
---

# prepare-distribution

## Goal
Produce `distribution/platform-package.json`: one target per dub-enabled language, each carrying
upload metadata and bound to the exact active `dubbed-video@<lang>` hash, so a human can
authorize a specific release. Also render a per-project YouTube guide under
`distribution/guides/`.

## Scope & boundaries (company rules 2, 3, 4)
- **No publication, no autonomy past the gate.** This skill prepares a packet and stops at the
  human `release_authorization` gate. It never uploads.
- **Distribution is opt-in.** READY_FOR_REVIEW is terminal for the autonomous loop; a human (or
  this skill on explicit request) moves the project into `PLATFORM_PACKAGING` deliberately.
- **Rights precede distribution.** Packaging records `distributable`; if rights are not cleared
  the gate report FAILs and the release cannot be authorized.

## Procedure
1. Confirm the project is at `READY_FOR_REVIEW`: `vid_cli.py project status <id>`.
2. (Recommended) Ensure chapters exist per language (`generate-chapters`) so timecodes are
   embedded in the description.
3. Move into packaging deliberately, then build:
   - `vid_cli.py project transition <id> --to PLATFORM_PACKAGING --actor human` (human step), or
   - if already there: `vid_cli.py distribute package <id>`.
   Pass `--advance` to attempt `PLATFORM_PACKAGING -> RELEASE_AUTHORIZATION` (the human gate will
   hold the transition until approved).
4. `Read` `distribution/platform-package.json` and `reviews/platform-package-gate-latest.json`.
   Summarize each target (language, title, channel, bound video hash) and the decision.
5. **STOP at `release_authorization`.** Present the packet + recommendation. A human grants the
   approval bound to the exact final-video hash (see the `video-release-authorize` skill).

## Outputs
- `distribution/platform-package.json` (registered `platform-package`).
- `distribution/guides/youtube-<lang>.md` per target.
- `reviews/platform-package-gate-latest.json`.

## Stop / Escalate — HUMAN GATE
`release_authorization` is human-bound (`disable-model-invocation: true` skill). Never grant it,
never upload, never force the transition.

## Completion contract
The platform package validates and binds each target to a real active video hash; the run stopped
at the release_authorization gate; nothing published.
