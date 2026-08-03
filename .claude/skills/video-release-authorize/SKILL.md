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
---

# video-release-authorize  (human-only gate)

## Goal
Record a **human's** decision to publish a specific video edition externally, by granting the
`release_authorization` approval bound to that edition's exact `dubbed-video@<lang>` hash. This
opens `RELEASE_AUTHORIZATION -> YOUTUBE_UPLOAD`.

## Why this is human-only
Publishing to a public platform is irreversible and outward-facing. Deciding that a particular
rendered video — this exact hash — may leave the building is a human judgment (`rights` are also
re-checked on this edge). `disable-model-invocation: true` enforces it: Claude may present the
packet, but only a person grants the approval.

## Preconditions
- The project is at `RELEASE_AUTHORIZATION` with a valid `distribution/platform-package.json`.
- `rights_status` is distributable (self-authored / licensed / fair-use-claimed) — the edge
  carries a `rights` re-check that blocks otherwise.

## What Claude MAY do (preparation only)
- `Read` `distribution/platform-package.json` and the `platform-package` gate report and summarize
  each target: language, title, channel, privacy, and the bound `video_sha256`.
- Recommend which editions to authorize. It may NOT run `approval grant`.

## What only a HUMAN does
For each edition to release, grant the approval bound to that edition's video hash:
```
vid_cli.py approval grant <id> \
  --gate release_authorization \
  --approver "<name>" \
  --artifact <video_sha256-or-path> \
  --scope release
```
Then authorize the transition:
```
vid_cli.py project transition <id> --to YOUTUBE_UPLOAD --actor human --reason "release authorized"
```

## Completion contract
A `release_authorization` approval exists bound to the exact final-video hash, rights are
distributable, and the project is at `YOUTUBE_UPLOAD`. No approval or transition performed by an
agent.
