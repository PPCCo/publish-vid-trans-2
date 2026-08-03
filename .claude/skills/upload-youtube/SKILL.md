---
name: upload-youtube
description: >-
  Upload an authorized video edition to YouTube through the sanctioned uploader. Inert by
  default — a dry-run returns the exact request without any network write. A real upload needs
  BOTH VIDTRANS_PUBLISH_ENABLED=1 and VIDTRANS_EXTERNAL_WRITES=enabled, credentials in the
  environment, and a prior release_authorization approval. Covers YOUTUBE_UPLOAD.
allowed-tools: Bash, Read
argument-hint: <video-id> --language <iso> [--live]
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus1/us.anthropic.claude-haiku-4-5-20251001-v1:0"
effort: low
---

# upload-youtube

## Goal
Upload the authorized `dubbed-video@<lang>` to its resolved channel, recording the attempt in
`distribution/upload-manifest.json`. **Default is a dry-run** that performs no network I/O and
returns the exact `videos.insert` request that would be sent.

## Scope & boundaries (company rule 3)
- **Two flags required for a live upload:** `VIDTRANS_PUBLISH_ENABLED=1` (module switch) AND
  `VIDTRANS_EXTERNAL_WRITES=enabled` (hook switch). Absent either, only the dry-run path runs.
- **Prior human authorization required.** The project must already be at `YOUTUBE_UPLOAD`, which
  is only reachable through the human `release_authorization` gate bound to the video hash. Rights
  are re-checked; a superseded/edited video hash aborts the upload.
- **Idempotent.** A completed upload of the same (channel, video hash) is skipped.
- Default to dry-run in all guidance. Only run `--live` when a human has explicitly asked and the
  flags/credentials are set.

## Procedure
1. Confirm state + authorization: `vid_cli.py project status <id>` (expect `YOUTUBE_UPLOAD`) and
   that a `release_authorization` approval exists (`vid_cli.py approval list <id>`).
2. **Dry-run first (always):**
   `vid_cli.py distribute youtube <id> --language <iso>`
   `Read` the returned `request_preview`; confirm title/description/privacy/channel are correct.
3. **Live upload (human-requested only):** with both flags exported and credentials present:
   `VIDTRANS_PUBLISH_ENABLED=1 VIDTRANS_EXTERNAL_WRITES=enabled vid_cli.py distribute youtube <id> --language <iso> --live [--advance]`
   `--advance` moves `YOUTUBE_UPLOAD -> PROMOTION_QUEUE` after a successful live upload.
4. `Read` `distribution/upload-manifest.json` and report the recorded `video_id`/`url` (or the
   dry-run preview).

## Outputs
- `distribution/upload-manifest.json` (registered per attempt); event `YOUTUBE_UPLOAD_RECORDED`.

## Completion contract
A dry-run preview (or, on explicit live request with flags set, a real upload record) exists; the
video hash matched the authorized release; nothing uploaded without both flags + authorization.
