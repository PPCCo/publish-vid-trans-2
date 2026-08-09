# Transcribing, Translating & Dubbing — Workflow

The end-to-end path from a source video to upload-ready packages, with the **states**, the
**commands**, the **human gates**, and the **review → edit → import** loops that trip people up.

> Every command is `vid_cli.py …`; run it as
> `.venv/bin/python3 .claude/scripts/vid_cli.py …` (never bare `python3`).
> `<id>` is the project id (e.g. `yt-MFuUIoF5PSc`). Lost? `vid_cli.py project plan <id>`
> prints the deterministic next step; its `autonomy_action` is authoritative:
> `PROCEED` (run it) · `STOP_AT_GATE` (a human must decide) · `BLOCKED` · `TERMINAL`.

---

## The pipeline at a glance

```
INGEST → LANGUAGE_ID → TRANSCRIPTION → [TRANSCRIPT_QA_GATE] → SEGMENT_RESOLUTION
  → TRANSLATION → [TRANSLATION_QA_GATE] → CAPTION_TIMING → CAPTION_VALIDATION
  → DUBBING → AUDIO_SYNC_ADJUST → [AUDIO_QA_GATE] → VIDEO_MUX → [FINAL_QA_GATE]
  → PACKAGE → READY_FOR_REVIEW ∎
```

`[BRACKETED]` = **human gate**: the pipeline stops until a person decides. Everything else the
agent/script advances on its own. The pipeline **ends at `READY_FOR_REVIEW`** — nothing uploads
by default.

**Two golden rules that explain the whole UX:**
1. **The CLI owns all state.** You never hand-edit `state.json`/`manifest.json`/approvals.
   Artifacts are content-addressed (SHA-256); editing an approved file auto-invalidates its
   approval.
2. **Gates are human decisions.** The agent prepares the packet and *recommends*; a human
   approves. You decide in plain language — the agent runs the plumbing (except three
   outward-facing gates, which stay human-run).

**Per-language after TRANSLATION.** From `TRANSLATION` on, each target language advances on its
own track. Scope of human review (rule 7):
- **source language** → `skip_translation` (verbatim captions, no gate)
- **English (`en`)** → human-reviewed, **stops at `translation_qa`**
- **every other target** (`fr`, `es`, `zh`, …) → `auto_translate`, deterministic QA only, **no gate**

---

## 1. Ingest & language

| Step | Command | Notes |
|---|---|---|
| Onboard (one video) | `project init …` + `ingest run <id>` | or the `/new-video` skill. Media download needs `VIDTRANS_FETCH_ENABLED=1` (`source .env.local`). |
| Set source language | `langid set <id> --language <iso> [--source-voice-gender male\|female]` | **Human decision** — ASR auto-detect is unavailable. Can be pre-filled at `project init --source-language`. |

---

## 2. Transcription  →  the transcript gate

```bash
vid_cli.py transcript run <id> [--provider mlx-whisper]      # ASR → transcript/source.<lang>.json
vid_cli.py transcript qa  <id>                               # deterministic QA report
vid_cli.py project transition <id> --to TRANSCRIPT_QA_GATE   # (or `transcript run --advance`)
```

**Hallucinating transcript?** Re-run with anti-repetition flags:
`transcript run --no-condition-on-previous-text [--hallucination-silence-threshold 2]`.
To regenerate you must be back at `TRANSCRIPTION`:
`project reset <id> --drop-transcript --actor human` → `project transition <id> --to TRANSCRIPTION --actor agent` → re-run.

### The English review-gloss (produced *at* the gate — rule 11)

At `TRANSCRIPT_QA_GATE` an English gloss of the source is always produced so the human can
validate meaning. This is the **review → edit → import loop** people ask about:

```bash
vid_cli.py transcript english-export <id>     # → transcript/english-gloss.worksheet.json  (FILL-IN form)
#   … fill target_text for every cue (the english-context-check skill does this) …
vid_cli.py transcript english-import <id>     # → transcript/english-gloss.json  (CANONICAL, registered)
vid_cli.py transcript qa <id>                 # re-fold gloss context findings into the QA report
```

**Overriding verse renderings (Quran/tafsir sources).** For a Quranic source, a companion review
aid may exist at `transcript/english-verses-gloss.worksheet.json` listing **only** the verse-bearing
cues, each with its `(Quran s:a)` citation(s), the source text, my English `translated_text`, and an
empty `modified_translation`. Fill `modified_translation` **only** for renderings you want to change
(leave `""` to accept mine). Then:

```bash
vid_cli.py transcript reconcile-verses <id>   # merge your modified_translation → target_text in the gloss worksheet
vid_cli.py transcript english-import  <id>    # runs reconcile-verses FIRST automatically, then rebuilds the gloss
```

`reconcile-verses` copies each non-empty `modified_translation` into the matching cue's
`target_text` in `english-gloss.worksheet.json` (by cue id) and mirrors it back into the verses
file's `translated_text` so both stay in sync. It is **always the first action of
`english-import`**, so you can skip the standalone call and just run `english-import` — but the
standalone verb lets you apply + inspect your edits before importing. No-op if the verses file is
absent or has no overrides. All verse citations render **inline** after each quoted verse in the
gloss `target_text` (English + numeric `(Quran s:a)`, no Arabic script — rule 16).

**worksheet vs. canonical — this is the key distinction:**

| `…worksheet.json` | `…gloss.json` (or any `captions.<iso>.json`) |
|---|---|
| Transient **fill-in form** — you edit this | **Canonical, registered** artifact — the CLI builds it |
| Overwritten by each `export`; disposable | Content-addressed (SHA-256), versioned |
| The thing you *edit* | The thing gates *bind to* |

> **To change a gloss/translation: edit the _worksheet_, then re-`import`. Never hand-edit the
> canonical `*.json`** — that desyncs its registered hash (rule 6). `import` rebuilds the
> canonical doc as a new version and validates it.

The gloss is a **review aid only** — it validates the *source transcript's* meaning. It is **not**
the shipped English caption (that's produced independently at TRANSLATION). The AI never edits the
source transcript; source problems become `context-mismatch` findings for the human.

**⏸ GATE — `transcript_qa`:** approve (bind the transcript hash) or **revise** (rewind to
`TRANSCRIPTION`, fix the source, re-run). See [Clearing a gate](#clearing-a-gate).

---

## 3. Segment resolution (auto)

`SEGMENT_RESOLUTION` resolves any clip selection into cut/join segments — usually a no-op
(whole video). The agent advances it.

---

## 4. Translation → captions → the translation gate

Runs **per language**. Same worksheet→import loop as the gloss.

```bash
vid_cli.py translate export <id> --language <iso>   # → captions/<iso>.worksheet.json  (empty target_text slots)
#   … fill target_text per cue …
vid_cli.py translate import <id> --language <iso> [--advance]   # → captions/captions.<iso>.json (canonical) + auto-splits long cues
vid_cli.py translate qa <id>                        # aggregate translation-qa + glossary reports (all tracks)
```

- **≥2 tracks to fill → the agent fans out in parallel** (one agent per language, rule 16).
  You don't drive this by hand; the `create-closed-captions` skill does.
- **Timing is the contract:** never change `id`/`start_ms`/`end_ms`/`source_text`; never
  merge/split cues. Long cues auto-split at import (rule 12).
- **Fix a translation:** edit `captions/<iso>.worksheet.json`, re-`translate import`. (Same
  worksheet→canonical rule as above.)

**⏸ GATE — `translation_qa`:** **only `en` stops here.** `auto_translate` targets pass on
deterministic QA alone; the source track is verbatim.

Then render + validate the caption files (deterministic, byte-stable):
```bash
vid_cli.py captions build    <id> --language <iso> --format srt,vtt
vid_cli.py captions validate <id>                  # reading-speed / line-length / duration report
```

---

## 5. Dubbing → audio sync → the audio gate

**One language at a time — never run two `dub run`s (or a dub + a mux) concurrently** (ffmpeg
contention). A non-clone dub needs **no** network env.

```bash
vid_cli.py dub run --language <iso> <id>     # → audio/<iso>/dub.wav  (per language; repeat sequentially)
vid_cli.py dub qa <id>                        # aggregate over ALL dubbed tracks (NO --language)
```

- **Constant audio speed (rule 14):** dubbed audio is never time-stretched; the **picture** is
  re-timed (freeze/trim) to the audio. `dub run` writes a per-language freeze plan; still-image
  languages get none.
- **Keep the original voice for a window:** a cue flagged `flags:["keep-source-audio"]` splices
  the **source audio** for that span into the dub in every language (recited/untranslatable
  passages) instead of TTS. The flag is added at the **worksheet** stage (it does not survive
  `transcript import`).
- **Silent-span QA:** a dead-air run > `silent_span_max_ms` (default 7000ms; keep-source windows
  exempt) is a blocker → `dub qa` FAILs.
- **Re-render one track after it's approved:** `project redub <id> --targets <iso>` (rewinds only
  that track to dubbing), then re-`dub run` + `dub qa`.

**⏸ GATE — `audio_qa`:** **per language** — each dubbed track needs its own approval.

---

## 6. Mux → final gate → package

```bash
vid_cli.py package mux      <id> [--language <iso>]  # rebuild picture on the dub timeline; re-time subs → video/<lang>/dubbed.mp4
vid_cli.py package final-qa <id>                     # aggregate `final` gate report across dubbed videos
vid_cli.py package build    <id>                     # assemble packages/<lang>/ + package-manifest.json
```

**⏸ GATE — `final_qa`:** one **project-level** approval (not per-language).
Then `PACKAGE → READY_FOR_REVIEW` — **blocked while `rights_status` is `unreviewed` /
`do-not-distribute`** (rule 4). Rights are recorded at init by default, or set at the human-only
`rights-check` gate.

**∎ Done at `READY_FOR_REVIEW`.** Distribution (chapters/upload/promotion) is a separate,
off-by-default phase.

---

## Clearing a gate

You **decide in plain language; the agent does the plumbing** (rule 13):

1. Agent **surfaces** the gate and summarizes what's under review.
2. Agent presents options — the fixes **and** an *Approve & advance* option.
3. On approve, the agent **discloses**: the gate, the exact **active SHA-256** it will bind, the
   file path(s), and any concerns — and gets **one explicit confirmation**.
4. Agent runs `approval grant --approver "<you>" --artifact <hash> --notes "…"` then the gated
   `project transition <id> --to <next> --actor human`.

**Three outward-facing gates stay human-run** (you type the final `!` command): `rights-check`,
`video-release-authorize`, `video-promote-approve`.

**Revise instead of approve** = rewind and fix. Common rewinds (all preserve history):

| Verb | Effect |
|---|---|
| `project reset <id> [--drop-transcript\|--full] --actor human` | Wipe downstream, rewind (default keeps source + transcript → `TRANSCRIPTION`). |
| `project redub <id> --targets <iso>` | Rewind **only** the named dub track(s) to dubbing. |
| `project add-languages <id> --targets <codes>` | Add tracks to an in-progress project (no reset). |
| `project enable-dub <id> --targets <codes>` | Turn on dubbing for an already-translated language. |

---

## Cheat sheet

```bash
vid_cli.py project status <id>        # full state
vid_cli.py project plan   <id>        # deterministic next step + autonomy_action
vid_cli.py nextcmd        <id>        # print (never run) the copy-paste command for the next step
vid_cli.py doctor                     # engines / tools / staged voices
```

**Golden reminders:** run `.venv/bin/python3 …vid_cli.py` (never bare `python3`) · edit
**worksheets**, `import` to regenerate the **canonical** artifact (never hand-edit the canonical
`*.json`) · dub **one language at a time** · gates are human decisions.
