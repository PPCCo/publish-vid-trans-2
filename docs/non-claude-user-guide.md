# yt-app — Standalone (non-Claude) A/V pipeline CLI

`yt-app/` is a self-contained command-line toolkit that runs the
**download → split → transcribe → translate → dub → mux** chain **without Claude**, without
the state machine, gates, approvals, or event log. It imports the framework's proven leaf
functions in place and thin-reimplements the two state-coupled orchestrators (dub, mux) as
pure functions over plain files. macOS only.

Use it when you want to batch-process videos yourself from a terminal and don't need the
review gates. If you *do* need the human-gated production pipeline, use the framework
(`vid_cli.py` / the Claude skills) instead.

---

> ## ⚠️ LOUD CALLOUTS — read before you dub
>
> 1. **Voice cloning is NOT consent-gated here.** The framework requires a recorded
>    `voice_clone_consent: true` before it will clone a speaker's voice (rule 5). **yt-app has
>    no rights record and no consent gate.** Cloning defaults **ON** for the company's
>    clone-languages (currently `en, zh, ar, es, ru, fr, pt`). If you do not have the right to
>    clone the speaker's voice, pass **`--no-clone`** (per-dub) or set
>    `"clone": {"enabled_by_default": false}` / `"voice_clone": {"<lang>": false}` in your
>    config. **You are responsible for having the rights.**
>
> 2. **`fa` (Persian) has no staged piper voice.** A `fa` dub will **fail loudly** at
>    `resolve_dub_voice` — this is expected, not a bug. `fa` and `ur` are piper-only (no XTTS
>    clone coverage); `ur` has a staged voice, `fa` does not yet. Forcing XTTS on either
>    (`per_language_provider={"fa":"xtts"}`) is a hard `LangSpecError` before any work starts.

---

## Install / run

Everything runs through **`yt-app/run.sh`**, which resolves the repo root from its own
location and execs the repo venv's Python on `cli.py`. Never call a bare `python3` — it lacks
the venv deps and is hook-blocked.

```bash
cd ~/Dev/my-repos/pub/publish-vid-trans
./yt-app/run.sh --help
./yt-app/run.sh <verb> [args]
```

`run.sh` self-configures the environment at import time (via `lib/env.py::bootstrap`), only
setting a var when it's unset **and** its target exists:

- `VIDTRANS_FETCH_ENABLED=1` — gate for media downloads (yt-dlp). Playlist enumeration is
  flag-free regardless.
- `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE=~/certs/aipe-certs.pem` — the Prisma Access
  TLS-inspection CA bundle (egress fails without it).
- `HF_HOME=<repo-parent>/.cache/huggingface` + `HF_HUB_OFFLINE=1` — HuggingFace is
  policy-blocked; models are staged offline into this cache.

An operator's explicit env always wins.

---

## Verbs

| Verb | What it does |
|---|---|
| `split-channels` | de-interleave / downmix an A/V file's audio channels |
| `probe` | ffprobe summary of a media file |
| `extract-audio` | extract a WAV from a media file |
| `transcribe` | ASR a WAV into a caption doc |
| `translate` | translate a source caption doc into target languages (parallel) |
| `dub` | render **and** mux a dub for one language of one video |
| `mux` | mux a pre-rendered audio WAV onto a video |
| `size` | compute a 16:9 (or `--aspect`) frame from one axis |
| `speed` | uniformly re-time any video (source untouched) |
| `playlist` | enumerate a playlist into a batch-list JSON (flag-free) |
| `batch` | run one id/url or a `--list` through the whole pipeline |

### `split-channels`
```bash
# de-interleave every channel of a multi-track recording into its own mono WAV
./yt-app/run.sh split-channels recording.mov --layout all --sr 48000
# downmix to a single mono file
./yt-app/run.sh split-channels stereo.wav --layout mono
```

### `probe`
```bash
./yt-app/run.sh probe clip.mp4
```

### `extract-audio`
```bash
# default 16kHz mono (ASR-ready)
./yt-app/run.sh extract-audio clip.mp4 --out clip.wav
```

### `transcribe`
```bash
# ASR a WAV; --lang is the spoken (source) language (no auto-detect)
./yt-app/run.sh transcribe clip.wav --lang fa --out-dir ./captions
# anti-hallucination flags for a repetitive/merged decode (see CLAUDE.md rule 10)
./yt-app/run.sh transcribe clip.wav --lang fa \
    --no-condition-on-previous-text --hallucination-silence-threshold 2.0 \
    --max-cue 7000
```
Long cues are auto-split to `--max-cue` ms (default: the company caption bar).

### `translate`
```bash
# translate a source caption doc into en, fa (verbatim), ar, es — cues in parallel
./yt-app/run.sh translate ./captions/fa.json --from fa --to en,fa,ar,es \
    --out-dir ./captions --parallel 4
```
The source language (`fa` here) yields **verbatim** captions (`target_text == source_text`);
it is never self-translated.

### `dub`
```bash
# clone the speaker's voice (default for clone-langs) — SEE THE CONSENT CALLOUT ABOVE
./yt-app/run.sh dub clip.mp4 --captions ./captions/en.json --lang en --out en.mp4

# force piper (no clone), burn subtitles in, run the finished track at 1.25x
./yt-app/run.sh dub clip.mp4 --captions ./captions/ur.json --lang ur \
    --no-clone --burn --speed 1.25 --out ur.mp4

# a still-image dub (no source video needed; no freeze plan — a static frame can't desync)
./yt-app/run.sh dub --captions ./captions/en.json --lang en \
    --still-image backdrop.jpg --out en.mp4
```
`dub` renders the audio (never time-stretched — the **picture** is re-timed to the audio via a
freeze/trim plan) and muxes it onto the picture in one step.

### `mux`
```bash
# lay a pre-rendered audio wav onto a video (no dub render, no freeze plan)
./yt-app/run.sh mux clip.mp4 dub.en.wav --captions ./captions/en.json --out en.mp4
./yt-app/run.sh mux clip.mp4 dub.en.wav --subs en.vtt --burn
```

### `size` / `speed`
```bash
./yt-app/run.sh size w 1920            # → 1920x1080 (even-rounded for H.264)
./yt-app/run.sh size h 583 --aspect 4:3
./yt-app/run.sh speed 1.25 clip.mp4    # → clip_1.25.mp4 beside the source (source untouched)
```

### `playlist`
```bash
# flag-free metadata enumeration → a batch-list JSON you can feed to `batch --list`
./yt-app/run.sh playlist "https://youtube.com/playlist?list=PLxxxx" \
    --out yt-app/projects/list-gen.json
```

### `batch`
```bash
# single video, using config/defaults.json verbatim
./yt-app/run.sh batch dQw4w9WgXcQ
# or a full URL
./yt-app/run.sh batch "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# a project list with per-video overrides
./yt-app/run.sh batch --list yt-app/projects/list-1.json --parallel 3 --out-root ./outputs
```

---

## Example flows

**1. Pull one channel from a multi-track recording, transcribe it:**
```bash
./yt-app/run.sh split-channels board-recording.mov --layout all
./yt-app/run.sh transcribe board-recording.ch0.wav --lang en
```

**2. Transcribe with a caption-length cap, then translate to four languages:**
```bash
./yt-app/run.sh transcribe speech.wav --lang fa --max-cue 6000 --out-dir ./cc
./yt-app/run.sh translate ./cc/fa.json --from fa --to en,fa,ar,es --out-dir ./cc --parallel 4
```

**3. Dub a video you already have audio for:**
```bash
./yt-app/run.sh mux speech.mp4 dub.en.wav --captions ./cc/en.json --out speech.en.mp4
```

**4. Playlist → list → batch:**
```bash
./yt-app/run.sh playlist "<playlist-url>" --out yt-app/projects/gen.json
./yt-app/run.sh batch --list yt-app/projects/gen.json --parallel 3
```

---

## Configuration

### `yt-app/config/defaults.json`
Batch defaults applied to every video unless a per-video `overrides` block replaces them. An
optional gitignored `defaults.local.json` is deep-merged over it.

```json
{ "schema_version":"1.0","sourceLang":"fa","targetLangs":["en","fa","ar","es"],
  "dubLangs":["en","ar"],"closedCaptions":["en","fa","ar","es"],
  "default_provider":null,
  "per_language_provider":{"en":"xtts","ar":"xtts","fa":"piper","ur":"piper"},
  "parallel_executions":3,"clone":{"enabled_by_default":true},
  "voice_clone":{"en":true,"zh":true,"ar":true,"fa":false,"ur":false,"fr":true,"es":true,"pt":true,"ru":true},
  "xtts_worker":{"clone_ref_trim_seconds":25,"idle_timeout_seconds":1800},
  "asr":{"provider":"mlx-whisper","model":"mlx-community/whisper-large-v3-turbo","max_cue_ms":7000},
  "mux_mode":"soft-subs","playback_speed":{},"images":{} }
```

### Project list (`--list`)
```json
{ "schema_version":"1.0","videos":[
  {"id":"abc123","url":"https://www.youtube.com/watch?v=abc123"},
  {"id":"def456","url":"...","overrides":{"targetLangs":["en","ar"],"dubLangs":["en"]}} ]}
```
`overrides` deep-merge over the defaults (dict-merge recursively; **lists and scalars replace
wholesale** — `targetLangs` is replaced, not concatenated).

### Language rules (validated up front, before any download)
- `sourceLang` in `targetLangs` → dropped from translation (verbatim captions only).
- A `dubLangs` / `closedCaptions` entry not in the produced language set → **ignored** with a
  note.
- `per_language_provider[lang]` forcing a non-piper engine on `fa`/`ur` → **hard error**.
- Clone-vs-piper precedence: (1) an explicit `piper` provider override wins; (2) explicit
  `voice_clone[lang]` (true/false); (3) otherwise clone iff `clone.enabled_by_default` **and**
  the language is in the company clone set.

---

## Resume

Each video keeps a sidecar `status.json` under `<out-root>/<video-id>/`. A stage is skipped
only if it's `done` **and** its recorded artifact exists non-empty. A crash mid-stage
(`running` at load) is treated as `pending` and re-runs. Interrupt a `batch` and re-run it —
completed stages (and completed per-language dub/mux tracks) are skipped. Partial artifacts
are never deleted; a re-run overwrites them.

## Concurrency

`batch` runs videos concurrently up to `parallel_executions` (default 3). **Dub and mux are
serialized to one at a time across the entire batch** via a shared semaphore — concurrent
ffmpeg dubs exhaust file descriptors and die (rc 232). Download / transcribe / translate
parallelize freely.

---

## Output layout

```
<out-root>/<video-id>/
  source/audio.wav          source/<video>.<ext>   source/info.json
  captions/<lang>.json      captions/<lang>.srt    captions/<lang>.vtt
  audio/dub.<lang>.wav      audio/freeze-plan.<lang>.json   audio/sync.<lang>.json
  video/dubbed.<lang>.mp4
  status.json
```

## Tests

```bash
./.venv/bin/python3 -m pytest yt-app/tests -q
```
Pure-logic only (langspec validation, freeze/trim algorithm, citation strip, status resume,
config merge, batch concurrency) — no ffmpeg, no engines, no network. They use a **synthetic**
company config fixture, never the gitignored real one.
