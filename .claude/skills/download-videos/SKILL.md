---
name: download-videos
description: >-
  Ingest one or more source videos: download via the sanctioned network module, extract a
  normalized WAV, probe metadata, merge into catalog/videos.json, register content-addressed
  source artifacts, and advance the project to LANGUAGE_ID. Use when a project is in INGEST.
allowed-tools: Bash, Read
argument-hint: <video-id> [more-video-ids...]
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus1/us.anthropic.claude-haiku-4-5-20251001-v1:0"
effort: low
---

# download-videos

## Goal
Turn a project's source URL into on-disk, cataloged, hash-registered source media, leaving the
project at `LANGUAGE_ID` with `source/metadata.json` and both source artifacts registered.

## Args
- `$1..$N` — one or more `video-id`s (each an already-initialized project whose `project.yaml`
  holds the source URL). Create projects first with `vid_cli.py project init`.

## Entry
This stage reaches the network. It is refused unless the operator has set
`VIDTRANS_FETCH_ENABLED=1` for the session — the CLI and the pre-tool hook both enforce this.
Confirm the flag is set before starting; if it is not, stop and tell the human to set it.

## Procedure
For each `video-id`:
1. Verify state: `vid_cli.py project status <id>` → `current_state` must be `INGEST`.
2. Run ingest: `vid_cli.py ingest run <id>`.
   - Downloads best video+audio + `--write-info-json` + available caption tracks.
   - Extracts mono 16 kHz `source/audio.wav`.
   - Probes streams, writes `source/metadata.json`, merges `catalog/videos.json`.
   - Registers `source-video` and `source-audio` artifacts.
   - Transitions `INGEST → LANGUAGE_ID`.
3. Report the returned `source_video.sha256`, duration, and any `metadata_language`.

Ingest is idempotent: a re-run reuses an existing download unless you pass `--force`.

## Outputs
- `projects/<id>/source/{<id>.mp4, audio.wav, metadata.json}`
- Updated `catalog/videos.json` (+ `catalog/events.ndjson` diff)
- Two registered artifacts; `INGEST_COMPLETED` event; state at `LANGUAGE_ID`

## Stop / Escalate
- Stop if `VIDTRANS_FETCH_ENABLED` is not set — do not attempt to work around it.
- Escalate if yt-dlp fails (geo-block, removed video, auth wall): report stderr, do not retry
  blindly, do not fetch from a non-allowlisted mirror.

## Completion contract
Every listed project is at `LANGUAGE_ID` with a valid `source/metadata.json`, a catalog entry
with `rights_status: unreviewed`, and two registered source artifacts. Report per-project hashes.
