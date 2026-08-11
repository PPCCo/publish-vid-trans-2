---
description: Generate the external terminal commands to kick off translate/dub for one playlist video with the uniform Javadi-playlist settings, then explain the handoff loop. A playlist URL instead indexes that playlist into the catalog.
argument-hint: [--still-image] <video-id> | <playlist-url>   (bare catalog id e.g. yt-MFuUIoF5PSc, or a youtube.com/playlist?list=… URL; add --still-image to use per-language still images)
model: "@bedrock-eus2/us.anthropic.claude-opus-4-8"
effort: medium
---

# /kickoff — one-command onboarding for the Javadi playlist

Onboard **one** video from playlist `PLDvVOFNMIIG3snBzVLV65D9YHbmkN8s2Z` with the uniform
settings below, and hand the operator the exact **external** terminal commands to run (plus the
handoff loop back into this framework). This command is a **read-only command generator** — it
prints commands for the operator to run in a plain macOS terminal; it does **not** run the
pipeline or mutate state itself.

## Argument routing (do this BEFORE anything else)

**First, parse the optional `--still-image` flag.** `$ARGUMENTS` may contain a `--still-image`
flag in any position (e.g. `/kickoff --still-image yt-ZCw8i1crHUU` or
`/kickoff yt-ZCw8i1crHUU --still-image`). Strip it out and record whether it was present:

- **`--still-image` present** → **still-image mode ON.** The `--images` option is included in
  `project init` (STEP 1) and the "Preflight the still images" step (STEP 1's preflight) runs.
- **`--still-image` absent (default)** → **still-image mode OFF.** The `--images` option is
  **removed entirely** from `project init`, and the "Preflight the still images" step is
  **skipped**. The languages fall back to the source video (rule 15's default).

After stripping the flag, the remaining token is the **positional argument** (a bare video-id or a
playlist URL). Route on that:

`$ARGUMENTS` (after stripping `--still-image`) is either a **bare catalog video-id** (e.g.
`yt-MFuUIoF5PSc`) or a **playlist URL** (contains `playlist?list=` or `/playlist`, or is a bare
`list=…`). Decide which:

- **Playlist URL** → **playlist-index mode** (below). Do NOT run the per-video onboarding steps.
  (`--still-image` is irrelevant here — playlist-index mode never inits a project.)
- **Bare video-id** → the per-video onboarding procedure (In-progress detection + STEPS 1–5 below).

### Playlist-index mode

When `$ARGUMENTS` is a playlist URL, ensure the playlist is cataloged, then step back — this mode
only indexes the playlist (flag-free, metadata-only enumeration, rule 3); it does **not** init or
ingest any video.

1. **Extract the playlist id** — the `list=` value from the URL (e.g.
   `https://www.youtube.com/playlist?list=PLnvPbB1dlVjUzEekQoDimvCW28iiIwxWJ` →
   `PLnvPbB1dlVjUzEekQoDimvCW28iiIwxWJ`).
2. **Check the catalog** — read `catalog/playlists.json` and see whether a `playlists[]` entry
   already has that `playlist_id`.
   - **Already present** → say so, then surface it: run
     `.venv/bin/python3 .claude/scripts/vid_cli.py catalog playlist <playlist-id>` and show its
     videos + each entry's derived `next_command`. Do **not** re-add. Stop.
   - **Not present** → add it. This is the one sanctioned mutation this command performs, and it is
     the flag-free metadata-only enumeration (no media download, rule 3), so run it directly (not
     as an `!`-block handoff):

     ```
     .venv/bin/python3 .claude/scripts/vid_cli.py catalog add-playlist "$ARGUMENTS"
     ```

     Then confirm the new entry with
     `.venv/bin/python3 .claude/scripts/vid_cli.py catalog playlist <playlist-id>` and show the
     indexed videos.
3. **Explain the next step** — to kick off any single video from the playlist, re-run
   `/kickoff <video-id>` with a bare catalog id from the list just shown. Stop here.

---

**In-progress detection (do this FIRST for a bare video-id).** These SETTINGS only apply to a **fresh** video. If the
video is already onboarded, its own configured settings are authoritative — do not re-apply the
SETTINGS or re-init. Before anything else, check whether `projects/<ID>/` exists (e.g.
`ls -d projects/<ID>` or `.venv/bin/python3 .claude/scripts/vid_cli.py project status <ID>` — a non-error
means it's in progress). **If it exists, defer to `/continue`:** invoke the `continue` skill for
`<ID>` (resume mode) and do NOT proceed with the onboarding steps below. Only when
`projects/<ID>/` does **not** exist do you run the fresh-onboarding procedure.

---

## SETTINGS (edit here to change for all future kickoffs)

| Setting | Value | Notes |
|---|---|---|
| `source_language` | `fa` | Pre-filled at `project init --source-language fa` directly (the whole playlist is Persian). Still revisable later at LANGUAGE_ID via `detect-language`/`langid set` if a particular video turns out to differ. |
| `targets` (soft-subs) | `en,ur,ar,zh,fr,es,pt,ru` | Translation set. |
| `audio` (dub) | `en,ur,ar,zh,fr,es,pt,ru` | All targets dubbed. |
| `dub voice gender` | `male` | Company default; emitted explicitly. |
| voice cloning | `true` | **Company default — consent auto-recorded at `project init`** (rule-5 override). No separate `rights set`/`--voice-clone-requested` needed. |
| rights status | `self-authored` | **Company default — auto-recorded at `project init`.** No separate command needed. |
| mux subtitle mode | `soft-subs` | Company default; emitted explicitly. |
| glossary | none | Omit `--glossary`. |
| playback speed | `1.0` for all 8 languages (normal speed) | `--speeds en=1.0,…,ru=1.0`. |
| still image | **Only when `--still-image` flag is passed** → `images/<video-id>-<lang>.jpg` for all 8 languages (`--images en=images/<id>-en.jpg,…`; repo-relative paths, init validates each exists and stores it absolute). **Without the flag → omitted entirely** (languages use the source video, rule 15 default). |

`LANGS` = `en ur ar zh fr es pt ru` (order fixed; used for `--speeds`, and — **only in still-image mode** — `--images` and the image preflight).

---

## Procedure — for `$ARGUMENTS` (a bare video id like `yt-MFuUIoF5PSc`)

Let `ID = $ARGUMENTS`. Derive the YouTube URL by stripping the `yt-` prefix:
`ID = yt-MFuUIoF5PSc` → `URL = https://www.youtube.com/watch?v=MFuUIoF5PSc`.

**First run the in-progress detection above.** If `projects/<ID>/` already exists, hand off to the
`continue` skill and stop — the steps below are for fresh videos only.

Otherwise (fresh video), do the following in ONE response (all read-only; do not run
init/ingest/pipeline yourself):

### 1. Preflight the still images (read-only) — **still-image mode ONLY**
**Skip this entire step when `--still-image` was not passed** (no `--images` in STEP 1 → nothing to
preflight). When `--still-image` **is** passed:

Run `ls images/<ID>-<lang>.jpg` for each of the 8 `LANGS` (a single `ls images/<ID>-*.jpg` is
fine). Print a ✅/⚠️ list of which per-language image files exist. If **any** are missing, warn
clearly:

> ⚠️ `project init` (STEP 1) will **fail** with `still image not found` until every listed image
> exists under `images/`. Supply the missing `images/<ID>-<lang>.jpg` files first, or drop the
> `--still-image` flag to fall back to the source video for now.

Emit the STEP 1 command regardless (emit-as-is + warn).

### 2. Emit STEP 1 — external onboarding (project init)
Bare-terminal block, ready to paste. Substitute `ID` and `URL`, and expand `LANGS` into the
`--speeds` (and, in still-image mode, `--images`) maps.

**Still-image mode ON (`--still-image` passed)** — include the `--images` map:

```
cd /Users/qaiser.abbas/Dev/my-repos/pub/publish-vid-trans
source .env.local
.venv/bin/python3 .claude/scripts/vid_cli.py project init <ID> \
  --url <URL> \
  --targets en,ur,ar,zh,fr,es,pt,ru \
  --audio en,ur,ar,zh,fr,es,pt,ru \
  --source-language fa \
  --dub-voice-gender male \
  --mux-mode soft-subs \
  --speeds en=1.0,ur=1.0,ar=1.0,zh=1.0,fr=1.0,es=1.0,pt=1.0,ru=1.0 \
  --images en=images/<ID>-en.jpg,ur=images/<ID>-ur.jpg,ar=images/<ID>-ar.jpg,zh=images/<ID>-zh.jpg,fr=images/<ID>-fr.jpg,es=images/<ID>-es.jpg,pt=images/<ID>-pt.jpg,ru=images/<ID>-ru.jpg
```

**Still-image mode OFF (no `--still-image`, the default)** — **omit `--images` entirely** (the
languages fall back to the source video):

```
cd /Users/qaiser.abbas/Dev/my-repos/pub/publish-vid-trans
source .env.local
.venv/bin/python3 .claude/scripts/vid_cli.py project init <ID> \
  --url <URL> \
  --targets en,ur,ar,zh,fr,es,pt,ru \
  --audio en,ur,ar,zh,fr,es,pt,ru \
  --source-language fa \
  --dub-voice-gender male \
  --mux-mode soft-subs \
  --speeds en=1.0,ur=1.0,ar=1.0,zh=1.0,fr=1.0,es=1.0,pt=1.0,ru=1.0
```

Emit **only** the block matching the current mode.

Note under it: this also auto-records **voice_clone_consent: true** and
**rights_status: self-authored** (company defaults), and pre-fills **source_language: fa** via
the same `langid set` path normally run at LANGUAGE_ID — no separate rights or langid command is
needed. `fa` is still revisable at LANGUAGE_ID if a particular playlist video turns out not to be
Persian.

### 3. Emit STEP 2 — external media download (ingest)
Generate the `ingest run` line from the sanctioned printer so the download command can't drift
from what the tool runs: run `.venv/bin/python3 .claude/scripts/vid_cli.py cmd <ID>` and take **only the
`ingest run <ID>` line** from its Option B block (ignore Option B's `project init` — it uses a
different stored target set incl. `fa`; STEP 1 above is the authoritative init with our settings).
The STEP 2 block is:

```
cd /Users/qaiser.abbas/Dev/my-repos/pub/publish-vid-trans
source .env.local          # sets VIDTRANS_FETCH_ENABLED=1 + proxy CA bundle (required for media download)
.venv/bin/python3 .claude/scripts/vid_cli.py ingest run <ID>
```

(If the operator prefers, `cmd`'s Option B already chains `project init … && ingest run …` in one
line — but STEP 1 here carries the extra settings, so run STEP 1 then STEP 2.)

### 4. Emit STEP 3 — external scripted pipeline (token-thrifty route)
```
cd /Users/qaiser.abbas/Dev/my-repos/pub/publish-vid-trans
.claude/scripts/run_pipeline.sh <ID> --mt
```

Note under it: this runs the whole deterministic transcribe→translate→dub chain **outside Claude**
(zero tokens) and **halts at the first human gate**. Since `source_language` is pre-filled as
`fa` from STEP 1, `run_pipeline.sh` passes through LANGUAGE_ID without stopping for input — the
first real stop should be further downstream. If the operator wants to correct the source
language for a particular video, that's still done at LANGUAGE_ID via `detect-language`/
`langid set` before this step. It is idempotent: safe to re-run; it resumes from wherever
`state.json` sits.

### 5. Explain the handoff loop
Present this plainly (this is how the framework takes over and hands back):

1. Run **STEP 1 → STEP 2 → STEP 3** externally, in order.
2. When `run_pipeline.sh` halts at a gate, come back here and say:
   **"the manual process for `<ID>` is done"**.
3. Claude then runs `/process-manual <ID> done` (resume/QA mode): it performs the editorial QA at
   each pending gate and walks you through approval per rule 13. (This is the only place this
   route spends Claude tokens.)
4. If clearing a gate needs another **external** command (e.g. a later `dub run` / `package mux`),
   Claude hands you that exact terminal block and steps back. Repeat 2–4 until the project reaches
   `READY_FOR_REVIEW`.

---

## Guardrails
- Never run `project init`, `ingest run`, `run_pipeline.sh`, or any gate transition yourself from
  this command — only **print** the commands for the operator (rule 1 state ownership is via the
  operator's external runs; gates stay human-bound, rules 2/13).
- The **one exception** is playlist-index mode's `catalog add-playlist`: it is the flag-free,
  metadata-only enumeration (no media download, no state gate, rule 3), so run it directly. Every
  **media** download and per-video init still stays a printed handoff.
- Emit bare-terminal form (no `!` prefix) — these are meant to be pasted into a normal macOS
  terminal, not run inside Claude.
