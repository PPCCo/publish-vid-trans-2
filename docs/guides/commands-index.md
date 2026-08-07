project init
/new-video
catalog add-playlist

## project init <id> --url <url> --targets <t1,t2,...> [flags]

`project init` is the raw CLI verb; /new-video is the guided workflow built on top of it. The key differences:

- One shot, no interview — you must already know and pass every flag yourself: --audio, --dub-voice-gender, --no-clone, --mux-mode, --glossary,
--images, --speeds, etc.
- Just creates project.yaml/state.json at INGEST. It does not download media — you still have to run ingest run yourself afterward.
- Doesn't route playlists — if you hand it a playlist URL it'll happily try to init one "project" for the whole playlist URL, which is wrong.

## /new-video <url> [video-id] (the skill)

1. Detects if the URL is a playlist and routes to catalog add-playlist instead (metadata-only enumeration, no project init) if so.
2. For a single video, asks the operator two AskUserQuestion checklists: which languages to translate, and which subset of those to dub (pre-selected
from company defaults, source language excluded from dubbing).
3. Runs a short init interview — dub voice gender, voice cloning (clone vs. neutral opt-out), rights status, mux subtitle mode, glossary,
per-language still images, per-language playback speed — then assembles the correct project init command with all those flags for you.
4. Calls project init with the assembled flags, then (if VIDTRANS_FETCH_ENABLED=1) also runs ingest run so the project lands ready at LANGUAGE_ID,
not just INGEST.
5. Reports back the project id, translate/dub sets, and resulting state.

In short: project init is the mechanical primitive; /new-video is the human-facing onboarding flow that figures out the right project init (and
follow-up ingest run) call for you, and handles the playlist carve-out. For normal onboarding you should use /new-video; call project init directly
only if you already know every flag and want to skip the interview (e.g. scripting many similar projects).