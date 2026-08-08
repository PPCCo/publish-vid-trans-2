project init
/new-video
catalog add-playlist
cmd / nextcmd (print the equivalent terminal command)
size (compute WxH from one axis)
speed (uniformly re-time any video)

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

## `size {w|h|width|height} <n> [--aspect W:H]` (compute the full WxH from one axis)

Project-free, pure math, no I/O. Useful for sizing a still image to the canvas before `project set-image` (rule 15), or any
time you know one dimension and need the other at a given aspect ratio. Output is always even-rounded (required for H.264).

```bash
# from width, default aspect 16:9
vid_cli.py size w 1920
# {"width": 1920, "height": 1080, "aspect": "16:9", "label": "1920x1080", "given": {"axis": "w", "value": 1920}}

# "width" is accepted as a synonym for "w" (same for "height"/"h")
vid_cli.py size width 1920

# from height instead of width
vid_cli.py size h 583
# {"width": 1036, "height": 584, "aspect": "16:9", "label": "1036x584", ...}   <- note even-rounding

# non-default aspect ratio, e.g. 4:3 for an older-style still image
vid_cli.py size w 1042 --aspect 4:3
# {"width": 1042, "height": 782, "aspect": "4:3", "label": "1042x782", ...}

# square canvas, e.g. for a square avatar-style still image
vid_cli.py size w 1080 --aspect 1:1
# {"width": 1080, "height": 1080, "aspect": "1:1", "label": "1080x1080", ...}
```

There is no verb for a raw diagonal/area computation — `size` only ever takes one axis + an aspect ratio. If you have a
target image already sized, you don't need this verb at all; it's for when you're *choosing* dimensions before creating one.

## `speed <factor> <video> [--out <path>]` (uniformly re-time any video)

Project-free. Re-times **both** audio and video by the same factor so they stay in sync — the whole-video equivalent of
`project set-speed` / `project init --speeds`, but usable on any file, not just inside a project. The source file is never
modified or overwritten; refuses to run if `--out` would collide with the input path.

```bash
# 25% faster (and therefore shorter) — output defaults beside the source
vid_cli.py speed 1.25 raw/lecture.mp4
# -> raw/lecture_1.25.mp4

# 10% slower (and therefore longer)
vid_cli.py speed 0.9 raw/lecture.mp4
# -> raw/lecture_0.9.mp4

# whole-number factors drop the trailing ".0" from the filename tag
vid_cli.py speed 2 raw/lecture.mp4
# -> raw/lecture_2.mp4

# explicit output path instead of the default "<stem>_<factor><suffix>" naming
vid_cli.py speed 1.5 raw/lecture.mp4 --out /tmp/lecture-preview-fast.mp4

# works on a file this tool never produced — e.g. sizing a raw download BEFORE project init
vid_cli.py speed 1.2 ~/Downloads/interview-source.mp4
```

Refuses to overwrite the source (`--out` pointing at the same path as `path` errors rather than clobbering). To apply a
speed change as part of a project's normal mux output instead of a one-off file, use `project set-speed <id> --language
<iso> --factor <f>` (adjusts an existing project) or `project init --speeds "en=1.25,ur=1.25"` (set at onboarding) — see
`.claude/CLAUDE.md` rule 15.
