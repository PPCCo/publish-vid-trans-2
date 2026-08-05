# publish-vid-trans

A repository-native, file-based **video-translation production company** for Claude Code.
It turns a Persian / Arabic / Urdu (or other source-language) video speech into an English
audio dub plus multi-language closed captions — autonomously, under human-bound approval
gates — and stops at an upload-ready package. It never publishes anywhere on its own.

Modeled on the operating pattern of [`publish-book`](https://github.com/PPCCo/publish-book):
deterministic Python CLI owns all state, content-addressed artifacts, human approvals bound
to exact file hashes, and a data-driven lifecycle state machine. See `ANALYSIS.md` for the
full design rationale and every deliberate deviation from `publish-vid-trans-plan.md`.

## Table of contents

- [Why this exists](#why-this-exists)
- [Non-negotiable rules](#non-negotiable-rules)
- [Quick start](#quick-start)
- [How a project moves through the pipeline](#how-a-project-moves-through-the-pipeline)
- [Selecting and clipping the source (`selection` / `join_clips`)](#selecting-and-clipping-the-source-selection--join_clips)
- [Repository layout](#repository-layout)
- [The CLI](#the-cli-vid_clipy)
- [Skills (the `/`-invocable workflow)](#skills-the--invocable-workflow)
- [Engines: ASR / MT / TTS](#engines-asr--mt--tts)
- [Rights and voice-cloning consent](#rights-and-voice-cloning-consent)
- [The vendor spend ceiling (cost guard)](#the-vendor-spend-ceiling-cost-guard)
- [Mux modes: soft-subs, burned-in, no-subs](#mux-modes-soft-subs-burned-in-no-subs)
- [Distribution and promotion (Phase 6, opt-in)](#distribution-and-promotion-phase-6-opt-in)
- [Chapters / titled breakpoints](#chapters--titled-breakpoints)
- [MCP server (read-only)](#mcp-server-read-only)
- [Hooks and permission posture](#hooks-and-permission-posture)
- [Worked example: end to end with no ML engines installed](#worked-example-end-to-end-with-no-ml-engines-installed)
- [Edge cases and caveats](#edge-cases-and-caveats)
- [Testing](#testing)
- [Installation](#installation)
- [What's out of scope](#whats-out-of-scope)

## Why this exists

Independent researchers, archivists, and diaspora communities often need to make a Persian,
Arabic, or Urdu speech legible in English — an accurate dub plus captions in several target
languages — without hand-rolling ffmpeg pipelines or trusting an agent to silently overwrite
approval records. This framework gives Claude Code a deterministic backend (the CLI) it
must call for every mutation, so an agent can prepare, recommend, and draft, but a human
always makes the calls that matter: rights, consent, and gate approval.

## Non-negotiable rules

These are enforced by `.claude/CLAUDE.md`, the state machine, and the pre-tool hook — not
just documentation:

1. **The CLI owns all state.** No one hand-writes `state.json`, `manifest.json`, approvals,
   or events. Every mutation goes through `vid_cli.py`.
2. **Gates are human-bound.** An agent may recommend a transition and prepare a gate packet.
   It can never grant an approval, set rights status, or force a transition through a human
   gate — those skills are marked `disable-model-invocation: true`.
3. **No external publication by default.** The autonomous pipeline stops at
   `READY_FOR_REVIEW` and produces upload-ready packages. The optional Phase 6 extension can
   upload/promote, but only after a human passes a `RELEASE_AUTHORIZATION` gate **and** both
   publish flags are set — `VIDTRANS_PUBLISH_ENABLED=1` (module switch) and
   `VIDTRANS_EXTERNAL_WRITES=enabled` (hook switch); with either unset every path is a
   no-network dry-run. Ingest downloads (`yt-dlp`) and any vendor API call happen only
   through the sanctioned network module, and only when `VIDTRANS_FETCH_ENABLED=1`.
   See [Distribution and promotion](#distribution-and-promotion-phase-6-opt-in).
4. **Rights precede distribution.** A project reaches `READY_FOR_REVIEW` regardless of
   rights status, but nothing under `packages/` is safe to hand off until a human sets
   `rights_status` to a distributable value. `PACKAGE → READY_FOR_REVIEW` is blocked while
   `unreviewed` or `do-not-distribute`.
5. **Voice cloning requires recorded consent.** Default dubbing uses a neutral voice.
   Cloning the source speaker's voice requires `voice_clone_consent: true` in the rights
   record.
6. **Artifacts are content-addressed.** Every produced file is registered with the CLI;
   approvals bind to its exact SHA-256. Editing an approved artifact supersedes the hash and
   auto-invalidates the approval bound to it.
7. **No vendor spend without a human-raised ceiling.** Billed TTS providers (ElevenLabs,
   Azure, Google) are refused until a human raises `budget.vendor_spend_ceiling_usd` above
   $0.00. Local engines are never spend-guarded.
8. **The CLI never imports an ML library or calls an LLM.** ASR/MT/TTS run as opt-in
   subprocesses; Claude does the translation itself, at the agent layer, not inside the CLI.

## Quick start

```bash
# one-time setup
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,mcp]'

# ingest needs yt-dlp; install it into the SAME venv (the CLI finds venv-installed tools
# even when the venv is not "activated" — see OPERATING-GUIDE.md).
.venv/bin/pip install yt-dlp

# see what's installed / missing (ffmpeg, ffprobe, ASR/TTS engines, yt-dlp)
.venv/bin/python3 .claude/scripts/vid_cli.py doctor

# create a project (source URL + which languages you want captions/audio for)
.venv/bin/python3 .claude/scripts/vid_cli.py project init yt-abc12345678 \
  --url "https://youtube.com/watch?v=abc12345678" \
  --targets en,ar,fa,ur --audio en

# check where it stands and what to do next
.venv/bin/python3 .claude/scripts/vid_cli.py project status yt-abc12345678
.venv/bin/python3 .claude/scripts/vid_cli.py project plan yt-abc12345678
```

Inside Claude Code, `/vid-status <video-id>` runs both `status` and `plan` and summarizes
them. From there, invoke the skill matching the project's current stage (see
[Skills](#skills-the--invocable-workflow)) — each one drives the project forward and stops
cleanly at the next human gate.

## How a project moves through the pipeline

```
INGEST → LANGUAGE_ID → TRANSCRIPTION → [TRANSCRIPT_QA_GATE] → SEGMENT_RESOLUTION →
TRANSLATION → [TRANSLATION_QA_GATE] → CAPTION_TIMING → CAPTION_VALIDATION →
  ├─(dub-enabled langs)→ DUBBING → AUDIO_SYNC_ADJUST → [AUDIO_QA_GATE] → VIDEO_MUX →
  │                       [FINAL_QA_GATE] → PACKAGE ─┐
  └─(caption-only langs)──────────────────────────────┴→ PACKAGE
                                                          │
                                                    [rights gate]
                                                          ↓
                                                  READY_FOR_REVIEW   ← autonomous stop
```

The autonomous pipeline **stops** at `READY_FOR_REVIEW` (`plan()` reports `TERMINAL`). The
opt-in Phase 6 extension continues from there, but only under human hands:

```
READY_FOR_REVIEW → PLATFORM_PACKAGING → [RELEASE_AUTHORIZATION] → YOUTUBE_UPLOAD →
PROMOTION_QUEUE → [PROMOTION_REVIEW] → PROMOTION_PUBLISHED → MONITORING (terminal)
```

`[RELEASE_AUTHORIZATION]` and `[PROMOTION_REVIEW]` are **human-only** gates (bound to the
exact final-video / promotion-manifest hash), and a `rights` re-check guards the
`RELEASE_AUTHORIZATION → YOUTUBE_UPLOAD` edge — publication is refused under non-distributable
rights even this late. See [Distribution and promotion](#distribution-and-promotion-phase-6-opt-in).

`[bracketed]` states are **human gates**: a deterministic report must read `PASS` (or
`CONDITIONAL_PASS`, which still needs the human okay) **and** a human approval must be bound
to the exact artifact hash currently on disk. Both conditions are required — a clean report
alone never opens a gate.

- Everything through `TRANSCRIPT_QA_GATE` is **project-level** (one shared source-language
  transcript).
- `SEGMENT_RESOLUTION` is a project-level step between the transcript gate and translation
  that turns an optional `selection` (which source time-windows to process) into a canonical,
  hashed segment list. With no selection it resolves instantly to a single whole-video segment
  and the pipeline behaves exactly as before. See
  [Selecting and clipping the source](#selecting-and-clipping-the-source-selection--join_clips).
- From `TRANSLATION` onward, each target language runs its own **track**
  (`state.language_tracks.<lang>`), in parallel, at its own pace. `translation_qa` and
  `audio_qa` approvals are per-language; `transcript_qa` and `final_qa` are project-level.
- A language is **caption-only** unless it's listed in `--audio` at `project init`. Its
  track skips `DUBBING…VIDEO_MUX` entirely and goes straight from `CAPTION_VALIDATION` to
  `PACKAGE`. A project can freely mix dub-enabled and caption-only tracks (e.g. dub `en`,
  caption-only `ar`/`fa`/`ur`).
- The project's top-level `current_state` advances past a per-language milestone only once
  *every active track* (dub-enabled tracks, for the audio/mux milestones) has reached it —
  a "slowest track" quorum, not a race.
- `PACKAGE → READY_FOR_REVIEW` additionally requires `rights_status` to be a distributable
  value (`self-authored` / `licensed` / `fair-use-claimed`), set by a human via
  `rights set`. The pipeline still runs all the way to `READY_FOR_REVIEW` under
  `unreviewed` rights — rights gate *distribution*, not *production* — it just refuses this
  last edge until a human clears it.
- Operational states outside the plan's original 12: `PAUSED`, `CANCELLED`, `ERROR` — every
  in-flight state can transition to any of these three, and `CANCELLED` is terminal.

## Selecting and clipping the source (`selection` / `join_clips`)

By default a project processes the **whole** source video. You can instead restrict
processing to specific source time-windows, and choose whether the selected parts ship as
**separate clips** or are **joined into one tightened video** — useful for lifting the
substantive passages out of a long lecture and shipping just those, either as chapters or as
one cut.

Two independent knobs, both set at `project init` and recorded in `project.yaml`:

- **`selection`** — which source time-windows to process. `null`/absent (the default) means
  the whole video. A `selection` is an object with an ordered `windows` list; **output order
  is list order**, so listing windows out of source order re-sequences them.
- **`join_clips`** — what to do with the selected windows. `true` (the default) concatenates
  them, in listed order, into **one** output video. `false` emits **each window as its own
  deliverable clip**. Ignored when `selection` is `null`.

### The three shapes

```bash
CLI=".venv/bin/python3 .claude/scripts/vid_cli.py"

# (1) Whole video — the default. No selection flags at all.
$CLI project init yt-abc12345678 --url "https://youtube.com/watch?v=abc12345678" \
  --targets en,ar --audio en

# (2) Three windows, shipped as three SEPARATE dubbed clips.
$CLI project init yt-abc12345678 --url "https://youtube.com/watch?v=abc12345678" \
  --targets en --audio en --no-join-clips \
  --selection '{"windows": [
    {"start": "00:08:07", "end": "00:12:40", "label": "intro"},
    {"start": "00:41:00", "end": "00:48:15", "label": "core argument"},
    {"start": "01:55:00", "end": "02:03:30", "label": "closing"}
  ]}'

# (3) Two windows JOINED into one video, in listed order (join_clips defaults to true).
$CLI project init yt-abc12345678 --url "https://youtube.com/watch?v=abc12345678" \
  --targets en --audio en \
  --selection '{"windows": [
    {"start": "00:41:00", "end": "00:48:15", "label": "core argument"},
    {"start": "01:55:00", "end": "02:03:30", "label": "closing"}
  ]}'
```

`--selection` accepts inline JSON or `@path/to/selection.json`. The related flags:

- `--no-join-clips` — emit one deliverable per window instead of joining.
- `--no-snap-edges` — cut at the exact requested timecodes instead of snapping edges to the
  nearest cue/silence boundary (see below).

### The `selection` shape

```jsonc
{
  "windows": [
    {
      "start": "00:41:00",     // HH:MM:SS(.mmm), MM:SS, or a (fractional) number of seconds
      "end":   "00:48:15",     // same formats; a bare number is SECONDS, not milliseconds
      "id":    "core",         // optional, human-stable; auto-assigned when absent
      "label": "core argument",// optional human note
      "exact": true            // optional per-window override: cut verbatim, ignore snapping
    }
  ],
  "snap_edges": true,           // project default; --no-snap-edges sets this false
  "snap_search_window_ms": 2000 // max distance (ms) an edge may move to reach a snap boundary
}
```

Timecodes may be given as `"HH:MM:SS.mmm"`, `"MM:SS"`, or a plain number — but a bare number
is interpreted as **seconds** (`90` = 90 s), so use a string when you mean a wall-clock time.
Both string and numeric forms validate against the schema and parse identically.

### Edge snapping

Because a hard cut in the middle of a word is jarring, each window edge is **snapped** by
default to the nearest cue/silence boundary in the full-source transcript, within
`snap_search_window_ms` (default 2000 ms). The adjustment is reported for the human. To cut
at the exact requested timecodes instead, either set `--no-snap-edges` for the whole project
or `"exact": true` on an individual window (per-window `exact` overrides the project default).
If no boundary is found within the search radius, the edge is left at the requested value and
flagged as unsnapped.

### Where it happens in the pipeline: `SEGMENT_RESOLUTION`

The selection is resolved at the `SEGMENT_RESOLUTION` state, which sits between the transcript
gate and translation. Crucially, **the middle of the pipeline is untouched**: translation,
captioning, and dubbing all run against the whole-source timeline as before. The actual
cutting and joining happens only at **assemble time** (PACKAGE) — the finished video is sliced
per window (frame-accurate, `libx264 -crf 18` re-encode) and, if `join_clips` is true,
concatenated; captions are re-offset to match. This "assemble-time cut/join" keeps the
translation/QA/dub stages simple and lets the selection stay editable late.

The verbs (an agent may run these; they mutate no gates):

```bash
$CLI segments resolve yt-abc12345678 --advance   # selection -> hashed segments.json; -> TRANSLATION
$CLI segments show    yt-abc12345678             # inspect the resolved windows + snap adjustments
$CLI segments list    yt-abc12345678             # same, alias
$CLI segments cut      yt-abc12345678            # (optional) extract per-segment clip media early
```

`segments resolve --advance` writes the canonical `segments.json` and advances
`SEGMENT_RESOLUTION → TRANSLATION`. For a whole-video project this resolves to a single
`[0, duration]` segment flagged `whole_video`, and packaging takes its original fast path
(`-c:v copy`, byte-identical to before).

### Caption-only + selection

A caption-only language (one not in `--audio`) with a selection ships a cut/joined video of
the selected windows carrying the **original source audio** plus soft captions — no dub.
(Because a cut requires a re-encode, the video is not stream-copied in this case.)

### Selection is not frozen — it's hash-bound

Resolving a selection stamps a `selection_hash` (a `util.hash_json` over the resolved segment
list) into the provenance of every downstream artifact. Editing the windows and re-running
`segments resolve` produces a new hash, which re-registers those artifacts under new SHA-256s
and **auto-invalidates any approvals bound to the old ones** — the same content-addressing
mechanism as [rule 6](#non-negotiable-rules). So you can revise the selection late; the gates
that depended on the old cut simply reopen.

### What the deliverables look like

- **Whole video** (`selection: null`): `packages/<lang>/` exactly as documented in the
  [worked example](#worked-example-end-to-end-with-no-ml-engines-installed).
- **`join_clips: false`, N windows**: one clip per window, e.g.
  `packages/<lang>/clip-0/`, `clip-1/`, … each with its own video + clip-local captions, plus
  a `package-manifest.json` listing every clip.
- **`join_clips: true`, N windows**: a single `packages/<lang>/joined.mp4` whose duration is
  the sum of the windows, with one continuous caption track re-timed across the joined
  timeline.

## Repository layout

```
publish-vid-trans/
├── .claude/
│   ├── CLAUDE.md              # company policy (this file's non-negotiables, verbatim)
│   ├── commands/               # /vid-status and other slash commands
│   ├── skills/                 # one skill per pipeline stage (see below)
│   ├── standards/              # durable cross-cutting contracts (artifact/approval/rights/naming)
│   ├── agents/, workflows/     # intentionally empty — see ANALYSIS.md §B9
│   ├── hooks/                  # pre-tool policy (deny/ask) + post-tool audit
│   ├── schemas/                # JSON Schema for every file type below
│   ├── scripts/
│   │   ├── vid_cli.py          # 16-line entrypoint
│   │   └── video_translation_house/   # the actual package: state, artifacts, engines, ...
│   ├── mcp/                     # read-only MCP server
│   └── config/
│       ├── company.default.json    # quality bars, budget ceiling, human-gate approver counts
│       ├── company.local.json      # human overrides (gitignored)
│       ├── tools.default.json      # engine/provider config, per-language routing, fonts
│       └── tools.local.json        # local overrides (gitignored)
├── catalog/
│   └── videos.json             # repo-global index of every ingested source video
├── projects/<video-id>/
│   ├── project.yaml / state.json / events.ndjson
│   ├── source/                 # downloaded video + extracted WAV + metadata
│   ├── transcripts/            # source-language transcript + qa-report.json
│   ├── segments/               # segments.json (resolved selection; a single segment when whole-video) + clips/
│   ├── captions/               # worksheets, captions.<lang>.json, .srt/.vtt
│   ├── chapters/               # (Phase 6) chapter worksheets + chapters.<lang>.json
│   ├── audio/<lang>/           # dub.wav per dub-enabled language + sync-report.json
│   ├── video/<lang>/           # dubbed.mp4 per dub-enabled language
│   ├── packages/<lang>/        # final deliverables: video + captions + README + checksums
│   ├── distribution/           # (Phase 6) platform-package + upload/promotion manifests; guides/ + renders/ gitignored
│   ├── artifacts/manifest.json # every registered artifact, content-addressed
│   ├── rights/record.json      # human-set rights + voice-clone-consent record
│   └── reviews/                # gate reports, keyed by report type
├── budget/ledger.json          # repo-global vendor-spend ledger (only exists once a vendor call runs)
├── ANALYSIS.md                 # design rationale + every deviation from the plan, with why
└── tests/                       # pytest suite mirroring the module layout
```

## The CLI (`vid_cli.py`)

Every command is `vid_cli.py [--root <path>] [--compact] <noun> <verb> [args]`, prints JSON
to stdout, and exits 2 on error (message on stderr). `--root` auto-detects by walking up to
find `.claude/CLAUDE.md` if omitted.

| Noun | Verbs |
|---|---|
| `doctor` | (no verb) — probe Python deps, ffmpeg/ffprobe, every optional engine, `yt-dlp` |
| `framework` | `validate` — self-check the state machine + schemas |
| `project` | `init`, `list`, `status`, `validate`, `next`, `plan`, `transition` |
| `artifact` | `list`, `register` |
| `approval` | `list`, `grant` *(human-only in practice — see below)* |
| `rights` | `check`, `set` *(`set` is human-only)* |
| `ingest` | `run` — download, extract WAV, probe, catalog, register, advance |
| `transcript` | `run` (ASR), `import` (no-engine path), `qa` |
| `segments` | `resolve` (selection → `segments.json`, `SEGMENT_RESOLUTION → TRANSLATION`), `cut`, `show`, `list` |
| `translate` | `export`, `import`, `qa` |
| `captions` | `build` (SRT/VTT), `validate` |
| `dub` | `run` (TTS), `import` (no-engine path), `qa` |
| `package` | `mux`, `final-qa`, `build` |
| `distribute` *(Phase 6, opt-in)* | `chapters export`/`import`, `package`, `youtube` *(dry-run default)*, `promote queue`/`publish` *(dry-run default)*, `package-show`/`upload-show`/`promotion-show` |
| `catalog` | `list`, `show` |
| `budget` | `status` — read-only ceiling/spend/headroom |
| `langid` | `detect`, `set` |
| `event` | `tail` |

`approval grant` and `rights set` exist as CLI verbs because the CLI is the only code path
allowed to write those files — but *invoking* them is restricted to humans by the skill
layer (`disable-model-invocation: true` on `rights-check`) and by policy (rule 2 above). An
agent should never run `approval grant` or `rights set` on a human's behalf.

`project plan <id>` is the single most useful command for "what do I do next": it returns
`autonomy_action` — `PROCEED` (keep going), `STOP_AT_GATE` (a human approval is needed),
`BLOCKED` (something else is unmet, e.g. rights), or `TERMINAL` (nothing left to do) — plus
`recommended_target` and the concrete `blockers` for every candidate transition. Treat it as
authoritative.

## Skills (the `/`-invocable workflow)

Each skill is scoped to one or two adjacent pipeline stages, stops cleanly at the next human
gate, and never bypasses the CLI:

| Skill | Stage | Notes |
|---|---|---|
| `download-videos` | `INGEST → LANGUAGE_ID` | sanctioned network module only, `VIDTRANS_FETCH_ENABLED=1` required |
| `detect-language` | `LANGUAGE_ID` | cheap auto-detect if an ASR adapter is installed; else asks a human |
| `rights-check` | any time, before `PACKAGE` | **human-only** (`disable-model-invocation: true`) |
| `create-closed-captions` | `TRANSCRIPTION`, `TRANSLATION` | runs ASR or imports a transcript, then the per-language translation worksheet + caption render |
| `qa-transcript` | `TRANSCRIPT_QA_GATE` | second-pass listen-back on flagged cues, prepares the gate packet |
| `qa-translation` | `TRANSLATION_QA_GATE` | editorial review of flagged + glossary-relevant cues |
| `qa-caption-sync` | `CAPTION_TIMING`, `CAPTION_VALIDATION` | reading-speed / duration / line-geometry review |
| `produce-dub` | `CAPTION_VALIDATION → AUDIO_SYNC_ADJUST` | runs TTS or imports a pre-rendered dub WAV |
| `qa-audio-sync` | `AUDIO_SYNC_ADJUST`, `AUDIO_QA_GATE` | per-cue drift / stretch-cap / cumulative-offset review |
| `mux-and-package` | `VIDEO_MUX → PACKAGE` | mux + assemble packages; stops at `final_qa` and the rights-blocked edge |
| `qa-final` | `VIDEO_MUX`, `FINAL_QA_GATE` | stream-presence + A/V-alignment review |
| `generate-chapters` *(Phase 6)* | after `CAPTION_VALIDATION` | export a chapter worksheet, fill titles/breakpoints, import → `chapters.<lang>.json` |
| `prepare-distribution` *(Phase 6)* | `READY_FOR_REVIEW → PLATFORM_PACKAGING` | build the per-platform packet + per-project guide; **stops at** `release_authorization`. Never uploads |
| `video-release-authorize` *(Phase 6)* | `RELEASE_AUTHORIZATION` | **human-only** (`disable-model-invocation: true`) — grant `release_authorization` bound to the final-video hash |
| `upload-youtube` *(Phase 6)* | `YOUTUBE_UPLOAD` | dry-run by default; a real upload needs both publish flags + prior authorization |
| `promote-content` *(Phase 6)* | `PROMOTION_QUEUE` | draft one distinct post per platform; **stops at** `promotion_review` |
| `video-promote-approve` *(Phase 6)* | `PROMOTION_REVIEW` | **human-only** — grant `promotion_review` for the queued posts |

Every QA skill's job is identical in shape: run the deterministic checker, summarize the
findings in plain language, and hand the human an evidence-backed gate packet — never grant
the approval itself.

## Engines: ASR / MT / TTS

All ML runs **outside** the CLI process, as a subprocess, and is entirely opt-in:

- **ASR** (`engines/asr.py`): `mlx-whisper` (Apple Silicon default), `faster-whisper`
  fallback. No engine installed → `EngineUnavailableError`; use `transcript import` with a
  Whisper-shaped JSON instead.
- **Translation**: done by Claude itself at the agent/skill layer (export a worksheet,
  translate, import it back) — never a local MT model, never an API call from inside the
  CLI.
- **TTS** (`engines/tts.py`): local (`kokoro`, `piper`, `xtts`, `chatterbox`) and vendor
  (`elevenlabs`, `azure`, `google`) providers, resolved per-language via
  `tools.default.json`'s `tts.per_language` map, falling back to whatever's on `PATH`. No
  engine installed → `EngineUnavailableError`; use `dub import` with a pre-rendered WAV
  instead. Vendor providers additionally go through the [spend ceiling](#the-vendor-spend-ceiling-cost-guard).

Run `vid_cli.py doctor` to see exactly what's installed vs. missing on this machine — every
check is marked `required` or `optional-missing`, never a hard failure for an optional
engine.

## Rights and voice-cloning consent

Every project gets a `rights/record.json`, starting at `rights_status: "unreviewed"`. Only a
human can change it (`vid_cli.py rights set` or, better, the `rights-check` skill, which
gathers evidence but stops short of setting the status). Valid distributable values:
`self-authored`, `licensed`, `fair-use-claimed`. `unreviewed` and `do-not-distribute` block
the final `PACKAGE → READY_FOR_REVIEW` edge — but do **not** block anything upstream, so the
full pipeline can run and produce review-only output before a human ever looks at rights.

```bash
.venv/bin/python3 .claude/scripts/vid_cli.py rights set yt-abc12345678 \
  --status self-authored --reviewer "Jane Doe" \
  --voice-clone-consent \
  --evidence rights/evidence/license.txt \
  --notes "Speaker is the channel owner; verified via email."
```

`--voice-clone-consent` may be set only with the rights holder's recorded consent. Without
it, `dub run --clone` is refused and dubbing falls back to the neutral default voice — this
is enforced in `dubbing.py`, not left to an agent's judgment.

## The vendor spend ceiling (cost guard)

Billed TTS providers (`elevenlabs`, `azure`, `google`) are refused by default:
`company.default.json`'s `budget.vendor_spend_ceiling_usd` starts at **`0.0`**. A human
raises it per-repo in `company.local.json`:

```json
{ "budget": { "vendor_spend_ceiling_usd": 5.00 } }
```

Every vendor TTS call then checks its estimated cost (`budget.vendor_call_estimated_cost_usd`,
also human-configurable) against the running total in `budget/ledger.json` before it runs;
exceeding the ceiling raises `BudgetExceededError` and nothing is spent. Local engines
(`kokoro`/`piper`/`xtts`/`chatterbox`) never touch this at all. Check current standing
read-only with:

```bash
.venv/bin/python3 .claude/scripts/vid_cli.py budget status
# {"vendor_spend_ceiling_usd": 5.0, "spent_usd": 0.3, "remaining_usd": 4.7, "vendor_providers": ["azure","elevenlabs","google"]}
```

## Mux modes: soft-subs, burned-in, no-subs

`package mux` supports three output shapes per dub-enabled language:

- **`soft-subs`** (default) — video stream copied bit-for-bit (`-c:v copy`), captions
  embedded as a toggleable `mov_text` track. Fast, lossless, but some platforms/players
  don't render soft subtitle tracks reliably.
- **`burned-in`** — captions rendered as pixels via ffmpeg's `subtitles` (libass) filter.
  Requires a full video re-encode (`libx264`) since a burn can't be combined with stream
  copy. Survives screenshots and platforms that drop soft tracks. Font family is chosen
  automatically from `tools.default.json`'s `fonts` map based on the language's script
  (CJK / Arabic / Cyrillic) so non-Latin scripts don't fall back to missing-glyph boxes.
- **`no-subs`** — dub audio only, no caption track at all.

```bash
.venv/bin/python3 .claude/scripts/vid_cli.py package mux yt-abc12345678 --language en --mode burned-in
```

`burned-in` requires your ffmpeg build to have libass support (`ffmpeg -filters | grep subtitles`);
without it, the mux fails with a clear `MuxError` rather than silently falling back.

## Distribution and promotion (Phase 6, opt-in)

The core framework ends at `READY_FOR_REVIEW`. **Phase 6** is an opt-in extension that takes
a review-ready project through upload and cross-platform promotion — but it is **off by
default and human-bound at every gate**. Nothing leaves the machine unless a human clears
rights, authorizes the release against the exact final-video hash, and both publish flags are
set. With the flags unset, every uploader and poster runs as a **no-network dry-run** that
returns the exact request it *would* send.

### The two publish flags (defense in depth — both required for a live write)

| Flag | Layer | Default | Effect when set |
|---|---|---|---|
| `VIDTRANS_PUBLISH_ENABLED=1` | module (`net/publish.py`) | off | lets the sanctioned uploader perform a real API call |
| `VIDTRANS_EXTERNAL_WRITES=enabled` | hook (`pre_tool_policy.py`) | off | lets bash-level API verbs (`videos.insert`, `yt-dlp`, …) through the pre-tool deny |

A live upload needs **both** — same posture as ingest needing `VIDTRANS_FETCH_ENABLED=1` at
both the hook and module layers. Credentials are read from environment variables named per a
channel's `credentials_ref` and documented (no secrets) in **`.env.example`**:
YouTube OAuth (`YT_DEFAULT_CLIENT_ID`/`_CLIENT_SECRET`/`_REFRESH_TOKEN`), X/Twitter
(`X_API_KEY`/`_API_SECRET`/`_ACCESS_TOKEN`/`_ACCESS_SECRET`), Telegram
(`TELEGRAM_BOT_TOKEN`/`_CHANNEL`), Discord (`DISCORD_WEBHOOK_URL`). The uploader SDKs are an
optional install: `pip install -e '.[publish]'`; a missing SDK degrades to `PublishDisabled`
rather than crashing.

### The one sanctioned write boundary

`net/publish.py` is the **only** module that performs an external write. Every function calls
`require_publish_enabled()` first, resolves credentials from the environment, checks the
target host against a publish allowlist, and accepts `dry_run=True` (the tested, default
path). Automated platforms: **YouTube** (Data API v3 resumable `videos.insert` +
`captions.insert`), **X/Twitter** (API v2), **Telegram** (Bot API), **Discord** (webhook).
Every other platform is a **manual how-to guide** (see below), never an automated post.

### The flow

```bash
CLI=".venv/bin/python3 .claude/scripts/vid_cli.py"
VID=yt-abc12345678

# from READY_FOR_REVIEW — an agent may prepare; a human authorizes.
$CLI project transition $VID --to PLATFORM_PACKAGING --actor human
$CLI distribute chapters export $VID --language en    # (optional) titled breakpoints
# ... fill chapters/en.chapters-worksheet.json ...
$CLI distribute chapters import $VID --language en
$CLI distribute package $VID --advance                # builds the packet; STOPS at the human gate

# HUMAN grants release_authorization, bound to the exact final-video sha256:
$CLI approval grant $VID --gate release_authorization --approver "Jane" \
  --artifact <dubbed-video-sha256> --scope video
$CLI project transition $VID --to YOUTUBE_UPLOAD --actor human

$CLI distribute youtube $VID --language en            # dry-run by default; --live needs both flags
$CLI project transition $VID --to PROMOTION_QUEUE --actor agent
$CLI distribute promote queue $VID --advance          # one DISTINCT post per platform; STOPS at gate

# HUMAN grants promotion_review, then publish (dry-run by default; --live needs both flags):
$CLI approval grant $VID --gate promotion_review --approver "Jane" \
  --artifact <promotion-manifest-sha256> --scope promotion
$CLI distribute promote publish $VID --advance
```

`distribute package` writes `projects/<id>/distribution/platform-package.json` (the exact
video hash each target binds to, templated title/description with chapter timecodes appended,
resolved channel) plus a rendered per-project how-to guide under
`projects/<id>/distribution/guides/`. `distribute youtube`/`promote publish` record every
attempt in `upload-manifest.json`/`promotion-manifest.json` — in dry-run those records are
`prepared`/`queued`, never `posted`.

### Channels and promotion config

- `.claude/config/channels.config.json` — per-language YouTube channels (`channelId`,
  `credentials_ref`, `privacy_default`, `category_id`, `playlist_id`, `default`). Resolution:
  a per-project override (`project.yaml` `distribution.youtube`) wins, else the `default:true`
  channel for the language, else the first. `framework validate` **rejects two `default:true`
  channels for the same language**.
- `.claude/config/promotion.config.json` — per-platform `automatable`/`enabled` flags,
  message templates, and `credentials_ref`. A guardrail flags two *queued* posts with
  byte-identical text so a human differentiates before publishing (checklist/manual posts,
  which share the default template by design, are exempt).

### Guides (`docs/guides/`)

Generic, video-agnostic how-tos live in `docs/guides/` and index which platforms are
**automated** (`youtube-upload.md`, `x-twitter.md`, `telegram.md`, `discord.md` — also cover
OAuth/API-key setup + env vars) vs **manual-only** (`reddit.md`, `instagram.md`, `tiktok.md`,
`facebook.md`, `linkedin.md`, `rumble.md`, `odysee.md`, `peertube.md`, `podcast-rss.md`).
`youtube-upload.md` doubles as the **template** the per-project renderer fills (`{{TITLE}}`,
`{{DESCRIPTION}}`, `{{CHANNEL_ID}}`, `{{PRIVACY}}`, `{{LANGUAGE}}`, `{{VIDEO_PATH}}`,
`{{VIDEO_SHA256}}`). Even automated platforms get a guide so a human can always fall back to
posting by hand.

## Chapters / titled breakpoints

Chapters (titled timestamp breakpoints for YouTube's description and X threads) are produced
by the **same worksheet round-trip as translation** — the CLI never calls an LLM. `distribute
chapters export` reads the canonical captions and emits
`chapters/<lang>.chapters-worksheet.json`: the full cue list, an empty `chapters[]` for the
agent to fill, and heuristic `candidate_boundaries` pre-seeded at large silence gaps. The
agent (via the `generate-chapters` skill) groups cues into titled chapters; `distribute
chapters import` validates them (first chapter at `0`, strictly increasing `start_ms`, all
within the video duration, non-empty titles) and writes canonical `chapters/chapters.<lang>.json`,
registered as a content-addressed `chapters@<lang>` artifact. Editing and re-importing
supersedes the hash and invalidates any approval bound to the old one (rule 6). At packaging,
`render_youtube_description_timecodes` turns the chapters into the `M:SS Title` / `H:MM:SS
Title` block YouTube parses (first line always `0:00`), appended to the description.

## MCP server (read-only)

`.claude/mcp/video_translation_house_server.py` exposes query/dry-run tools only —
`framework_validate`, `project_get_status`, `project_plan`, `catalog_list`/`show`,
`transcript_show`, `glossary_check_preview`, `caption_validate_preview`, `sync_report_show`,
`audio_qa_preview`, `package_manifest_show`, `final_qa_preview`, `budget_status`, and more.
No MCP tool can mutate state; every real write goes through the CLI. The pre-tool hook
additionally hard-denies any write-capable MCP tool name as defense in depth. Configured via
`.mcp.json` (human-owned, write-protected).

## Hooks and permission posture

`.claude/hooks/pre_tool_policy.py` runs before every `Bash`/`Write`/`Edit`/`NotebookEdit`/
`WebFetch`/`WebSearch` call:

- **Hard deny (never allowed, no prompt):** hand-writing CLI-owned project state
  (`state.json`, `manifest.json`, anything under `approvals/`/`events/`); editing
  `.claude/settings.json` or `.mcp.json`; `yt-dlp` unless `VIDTRANS_FETCH_ENABLED=1`;
  external-publish verbs (`videos.insert`, `captions.insert`, `--upload`, standalone
  `publish`/`purchase`, X/Telegram/Discord post verbs) unless `VIDTRANS_EXTERNAL_WRITES=enabled`.
- **Ask (prompts the human):** secrets/credentials/`.git` writes, writes outside the repo,
  `rm -rf` outside OS temp dirs, `sudo`, `git push`/`reset --hard`, cloud-CLI writes,
  write-capable MCP tools.
- **Allow:** normal project/source file writes, temp-dir cleanup, read-only MCP tools.

`.claude/hooks/post_tool_audit.py` hashes touched files and appends an event after every
write, independent of what the CLI itself logs.

## Worked example: end to end with no ML engines installed

Every stage has a **no-engine path** — you can walk a project the whole way to
`READY_FOR_REVIEW` with zero ASR/TTS binaries installed, using `import` verbs that accept
pre-rendered content. This is also exactly how the test suite exercises the full lifecycle.

```bash
CLI=".venv/bin/python3 .claude/scripts/vid_cli.py"
VID=yt-abc12345678

$CLI project init $VID --url "https://youtube.com/watch?v=abc12345678" --targets en,fa --audio en
$CLI project transition $VID --to LANGUAGE_ID --actor agent
$CLI langid set $VID --language fa --source manual --confidence 0.9
$CLI project transition $VID --to TRANSCRIPTION --actor agent

# no ASR installed: import a Whisper-shaped transcript instead of `transcript run`
$CLI transcript import $VID --from my-transcript.json --advance

$CLI transcript qa $VID
# a human now grants the transcript_qa approval, bound to the transcript's sha256:
$CLI approval grant $VID --gate transcript_qa --approver "Jane" \
  --artifact <transcript-sha256> --scope transcript
$CLI project transition $VID --to SEGMENT_RESOLUTION --actor human

# resolve the selection (here: none → whole video) and advance into TRANSLATION
$CLI segments resolve $VID --advance

# per language: export a worksheet, translate it (this is where Claude does the work), import it back
$CLI translate export $VID --language en
# ... fill in target_text in captions/en.worksheet.json ...
$CLI translate import $VID --language en --advance
$CLI translate qa $VID
$CLI approval grant $VID --gate translation_qa --lang en --approver "Jane" \
  --artifact <captions-sha256> --scope captions
$CLI project transition $VID --to CAPTION_TIMING --actor human
$CLI captions build $VID --language en
$CLI captions validate $VID

# no TTS installed: import a pre-rendered dub instead of `dub run`
$CLI dub import $VID --language en --from my-dub.wav --advance
$CLI dub qa $VID
$CLI approval grant $VID --gate audio_qa --lang en --approver "Jane" \
  --artifact <dub-sha256> --scope audio
$CLI project transition $VID --to VIDEO_MUX --actor human

$CLI package mux $VID --language en --mode soft-subs --advance
$CLI package final-qa $VID
$CLI approval grant $VID --gate final_qa --approver "Jane" \
  --artifact <dubbed-video-sha256> --scope video
$CLI package build $VID --advance

# rights must be cleared before the last edge opens
$CLI rights set $VID --status self-authored --reviewer "Jane"
$CLI project transition $VID --to READY_FOR_REVIEW --actor human
```

`packages/en/` now holds `dubbed.mp4`, `captions.en.srt`/`.vtt`, a `README.md` with a rights
banner and SHA-256 checksums, and `package-manifest.json`. `packages/fa/` (caption-only)
holds just the captions and README — no video, since `fa` was never in `--audio`.

## Edge cases and caveats

- **Mixed dub-enabled / caption-only projects.** Fully supported — a caption-only track
  skips `DUBBING…VIDEO_MUX` and the top-level state's per-milestone quorum only counts the
  tracks for which that milestone applies (dub-enabled tracks for audio/mux, every active
  track for translation/captions/packaging).
- **No diarization.** Overlapping speech (Q&A, cross-talk) is not detected by a speaker
  model — that stack (pyannote/HF-gated) was deliberately ruled out as too heavy for this
  local-first posture (see `ANALYSIS.md` B1). Instead, `transcript_qa` flags any cue whose
  timing overlaps the previous cue as `possible-overlapping-speech`, a deterministic proxy
  that tells a human where to listen more carefully — it is not a real diarization pass.
- **Language auto-detection is a documented stub.** `langid detect` reports
  `available: false` unless a verified ASR adapter is installed; until then, set the source
  language manually with `langid set`. This is intentional (Phase 2 scope), not a bug.
- **Back-translation QA is heuristic-only.** `translate.py`'s back-translation check flags
  empty target text and cues left identical to the source — it cannot judge translation
  *quality*, because the CLI is forbidden from calling an LLM. Real editorial review happens
  at the `qa-translation` skill and the human `translation_qa` gate.
- **CJK and RTL scripts get different caption rules.** Line-wrapping and reading-speed
  checks branch on script class (`captions.script_class`): character-count wrapping and
  chars/min for CJK, word-count wrapping and words/min for Latin/Cyrillic, with RTL-aware
  font selection for burned-in Arabic/Persian/Urdu captions.
- **Editing an approved artifact invalidates its approval, silently from the artifact's
  perspective but loudly from the gate's** — the next `project status`/`plan` call will show
  the edge closed again, with the invalidation reason in the approval record. This is by
  design (rule 6); don't "fix" a gate that won't open by re-running `approval grant` without
  checking whether the underlying file actually changed.
- **`burned-in` mux mode needs a libass-enabled ffmpeg.** Many Homebrew/distro ffmpeg builds
  ship without the `subtitles` filter; `package mux --mode burned-in` fails fast with a
  clear error rather than silently degrading to soft-subs. Check with
  `ffmpeg -filters | grep subtitles`.
- **Ingest is allowlisted, not open.** The sanctioned network module only accepts
  `youtube.com`/`youtu.be`/`vimeo.com` URLs and only runs when `VIDTRANS_FETCH_ENABLED=1` —
  an agent cannot redirect ingest to an arbitrary host even if it tries.
- **Behind a TLS-inspecting proxy, point `yt-dlp` at the corporate CA bundle.** On networks
  that intercept HTTPS (e.g. Palo Alto Prisma Access), `ingest run` fails with
  `CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`. yt-dlp reads
  the trust store from the standard-library `ssl` module, which honors `SSL_CERT_FILE` —
  export it (and `REQUESTS_CA_BUNDLE`) to the bundle that contains the interception root
  before running ingest. See OPERATING-GUIDE.md for the exact recipe. This is an environment
  condition, not a framework bug.
- **A source selection is resolved late and can be re-cut.** Selection windows are turned
  into a hashed `segments.json` at `SEGMENT_RESOLUTION` and only *applied* (cut/joined) at
  PACKAGE — translation/dub run on the whole timeline throughout. Empty `selection: []`,
  `start >= end`, negative or non-parseable timecodes, and windows outside `[0, duration]` are
  errors (use `selection: null` for the whole video); overlapping / duplicate / out-of-order
  windows are **allowed** (re-sequencing is the point) and only noted. See
  [Selecting and clipping the source](#selecting-and-clipping-the-source-selection--join_clips).
- **Bare-number timecodes are seconds, not milliseconds.** In a selection window, `90` means
  90 seconds; write `"01:30"` (or `90.0`) for clarity. This trips up authors expecting ms.
- **Large binaries are gitignored, not committed.** `projects/*/source/`, `audio/`, `video/`,
  and `packages/` hold multi-hundred-MB files; only the JSON/manifest/schema layer is meant
  to live in version control. Phase 6 adds `projects/*/distribution/guides/` and
  `projects/*/distribution/renders/` to the ignore list (rendered per-project guides may
  carry draft copy) while keeping the distribution JSON manifests tracked.

## Testing

```bash
.venv/bin/python3 -m pytest tests/ -q                              # all pass, 1 skipped*
.venv/bin/python3 -m ruff check .claude/scripts .claude/mcp .claude/hooks tests
VIDTRANS_REPO_ROOT="$PWD" .venv/bin/python3 .claude/scripts/vid_cli.py framework validate
```

\* the one skip is the burned-in mux render test, which needs a libass-enabled ffmpeg; the
mode-validation logic around it is still covered and passes.

Most of the lifecycle is tested through the **no-engine `import` paths** (`transcript
import`, `dub import`) precisely so the suite doesn't depend on any ASR/TTS binary being
installed in CI. Tests that do need `ffmpeg`/`ffprobe` skip cleanly (`ffmpeg_required`) when
absent. The Phase 6 suites (`test_chapters.py`, `test_distribution.py`, `test_publish.py`)
exercise the full distribution lifecycle to `MONITORING` entirely through the **dry-run**
path — no external write, no publish credentials, and no SDK required.

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,mcp]'
```

This installs the package (`video_translation_house`) in editable mode with dev tooling
(`ruff`, `mypy`, `pytest`) and the MCP SDK. A `vid-cli` console script is also on `PATH`
inside the venv once installed, as an alternative to `python3 .claude/scripts/vid_cli.py`.

Optional, install as needed:
- `ffmpeg`/`ffprobe` — required for any real media operation (mux, probe, audio ops).
- `yt-dlp` — required for `ingest run`; not needed if you supply source video files directly.
  Install it into the project venv (`.venv/bin/pip install yt-dlp`); the CLI resolves
  tools on `PATH` **and** in the venv's own `bin/` (next to the running interpreter), so a
  venv-installed `yt-dlp` is found without activating the venv.
- An ASR engine (`mlx-whisper`, `faster-whisper`) — optional; `transcript import` works
  without one.
- A TTS engine (`kokoro`, `piper`, `xtts`, `chatterbox`, or a vendor CLI) — optional; `dub
  import` works without one.
- The Phase 6 uploader SDKs (`pip install -e '.[publish]'`:
  `google-api-python-client`, `google-auth-oauthlib`, `tweepy`, `requests`) — optional; the
  distribution dry-run path works without them, and a missing SDK degrades to
  `PublishDisabled` rather than crashing.

## What's out of scope

**Phase 6 — distribution and promotion** (YouTube upload, cross-platform promotion, chapters)
is now **implemented but opt-in and off by default** — see
[Distribution and promotion](#distribution-and-promotion-phase-6-opt-in). The *autonomous*
pipeline still ends at `READY_FOR_REVIEW`: an upload-ready package on disk, nothing pushed
anywhere. Going further is never automatic — it requires a human to clear rights, pass the
`RELEASE_AUTHORIZATION` gate against the exact final-video hash, and deliberately set **both**
publish flags (`VIDTRANS_PUBLISH_ENABLED=1` + `VIDTRANS_EXTERNAL_WRITES=enabled`) plus supply
platform credentials via `.env`. With any of those absent, Phase 6 runs only as a no-network
dry-run.

Still genuinely out of scope: speaker diarization (see `ANALYSIS.md` B1); a hosted/scheduled
publishing service (Phase 6 fires on human command, it does not run a queue daemon); and
analytics beyond the terminal `MONITORING` marker (no view/engagement scraping).
