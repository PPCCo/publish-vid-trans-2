project init
/new-video
catalog add-playlist
cmd / nextcmd (print the equivalent terminal command)

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

## `cmd <video-id-or-url>` / `nextcmd <project-id>` (print the equivalent terminal command)

These two verbs don't *do* anything — they **print** a copy-paste-ready terminal block so you can run the underlying tool yourself in a plain shell
instead of through the CLI. They are read-only views over the same state `catalog show`'s `next_command` reads; they never download, execute, or
mutate anything.

- `cmd <video-id-or-url>` — the onboarding/ingest equivalent for a video URL, a bare 11-char id, a playlist URL/id, or an existing catalog video_id.
- `nextcmd <project-id>` — the raw-external equivalent of a project's *current* next step (the `next_command` analog).

When a step maps to both a real external tool and a vid_cli.py verb, both are printed as labelled options, each with its own `cd` and the exact env
preamble that flavor needs:

- Option A (raw external) — e.g. `yt-dlp …`, preceded only by `export VIDTRANS_FETCH_ENABLED=1` (a brew/PATH yt-dlp needs no CA bundle).
- Option B (via vid_cli.py) — e.g. `ingest run <id>`, preceded by `source .env.local` (the Python net layer needs the fetch flag + proxy CA bundle).
- Playlist enumeration is the flag-free carve-out (rule 3) — no env preamble on either option.
- A pure state mutation (no external tool) falls back to just the vid_cli.py verb ("CLI-only state op").

At a human gate (STOP_AT_GATE), nextcmd prints no runnable command — only the explanation and the `approval grant … && project transition …` line as
reference, because gates are decided in conversation (disclose → confirm → execute), never by pasting that line (rule 13).

Output defaults to bare terminal form. Add `--for-claude` to re-add the `!` prefix on runnable lines for pasting into Claude — never on a gate
reference line. Because the printer is built from the same argv builders the real downloader runs and the same verb strings catalog emits, the printed
command can never drift from what actually executes.

Use these when you want the raw command (to run elsewhere, to inspect exactly what would run, or to audit the env a step needs); use the pipeline
skills or the plain vid_cli.py verbs when you just want the step done.
