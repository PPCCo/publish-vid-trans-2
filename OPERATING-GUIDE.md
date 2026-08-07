# OPERATING-GUIDE.md

Day-to-day operating guide for **publish-vid-trans**, for use *after* the one-time setup in
[README.md](README.md#installation) is done (venv created, `pip install -e '.[dev,mcp]'`,
`yt-dlp` installed into the venv, `ffmpeg`/`ffprobe` on `PATH`).

For the *why* — the rules, the state machine, rights/consent, mux modes, distribution —
read the README. This file is the "what do I type" reference.

---

## 0. Environment every session needs

The CLI is always run as `.venv/bin/python3 .claude/scripts/vid_cli.py …` (no need to
"activate" the venv — the CLI finds venv-installed tools automatically). A shell alias makes
the rest of this guide copy-pasteable:

```bash
cd /Users/qaiser.abbas/Dev/my-repos/pub/publish-vid-trans
alias vid='.venv/bin/python3 .claude/scripts/vid_cli.py'
```

### Network flags (only when you actually fetch/publish)

Nothing touches the network unless you opt in per operation:

```bash
export VIDTRANS_FETCH_ENABLED=1          # required for `ingest run` (yt-dlp) + model/vendor downloads
# Phase-6 publishing needs BOTH of these (off by default — see README):
# export VIDTRANS_PUBLISH_ENABLED=1
# export VIDTRANS_EXTERNAL_WRITES=enabled
```

### Behind a TLS-inspecting proxy (this machine)

This machine sits behind a Palo Alto **Prisma Access** proxy that intercepts HTTPS. Without
telling Python which CA to trust, `ingest run` and model downloads fail with
`CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`. The bundle that
contains the interception root here is `~/certs/aipe-certs.pem`. Export it before any egress:

```bash
export SSL_CERT_FILE="$HOME/certs/aipe-certs.pem"
export REQUESTS_CA_BUNDLE="$HOME/certs/aipe-certs.pem"
```

> yt-dlp and huggingface_hub both read their trust store from the stdlib `ssl` module, which
> honors `SSL_CERT_FILE`. The proxy also sets `HTTP(S)_PROXY` for you. This is an environment
> condition, not a framework setting — on an un-proxied network you can skip this block.

A ready-to-source snippet (put in `.env.local`, **not** committed):

```bash
# .env.local — source this before fetching:  source .env.local
export VIDTRANS_FETCH_ENABLED=1
export SSL_CERT_FILE="$HOME/certs/aipe-certs.pem"
export REQUESTS_CA_BUNDLE="$HOME/certs/aipe-certs.pem"
export HF_HUB_DISABLE_PROGRESS_BARS=1
```

---

## 1. Health check

```bash
vid doctor              # ffmpeg/ffprobe, yt-dlp, optional ASR/TTS engines, framework self-check
```

`status: pass` means every *required* tool is present. Optional engines showing
`optional-missing` is fine — the `import` paths work without them.

---

## 2. The daily driver: status → plan → skill

For any project, these two commands tell you exactly where it is and what to do next:

```bash
vid project status <video-id>     # full state + per-language tracks
vid project plan   <video-id>     # deterministic next step
```

`plan` returns `autonomy_action`, which is authoritative:

| value | meaning | what you do |
|---|---|---|
| `PROCEED` | a normal step is next | run the CLI verb / the matching skill |
| `STOP_AT_GATE` | a human approval is required | prepare the gate packet, then a **human** runs `approval grant` |
| `BLOCKED` | something else is unmet (usually rights) | resolve the blocker (e.g. a human runs `rights set`) |
| `TERMINAL` | nothing left (at `READY_FOR_REVIEW`) | done — Phase 6 is a separate, opt-in path |

Inside Claude Code, `/vid-status <video-id>` runs both and summarizes them, then you invoke
the skill for the current stage.

---

## 3. Create + ingest a new project

```bash
# id must be filesystem-safe; a YouTube-derived id like yt-<11 chars> is fine
# (mixed case IS allowed — real YouTube ids are case-sensitive, e.g. yt-YP0FDR7Wc-8).
vid project init yt-YP0FDR7Wc-8 \
  --url "https://www.youtube.com/watch?v=YP0FDR7Wc-8" \
  --targets en,ar,fa,ur --audio en          # dub en; caption-only ar/fa/ur

# ingest needs the fetch flag + (behind a proxy) the CA bundle — see §0
source .env.local            # or export the vars inline
vid ingest run yt-YP0FDR7Wc-8               # download + WAV + probe + catalog → LANGUAGE_ID
```

Or let the **`/new-video`** skill do it conversationally: give it the URL, it asks you (two
checklists) which languages to **translate** and which of those to **dub**, then runs
`project init` + `ingest run`. A **playlist** URL routes instead to `catalog add-playlist`
(see §8).

**Two-axis translate/dub scope.** Translation *and* dubbing run for every language you pick,
but only the **source language** and **English** get a human-filled worksheet + a human
`translation_qa` approval. Every other target (`fr,es,zh,pt,ru,…`) is **AI-auto-translated**
(marked `auto_translate`) with deterministic QA only — no human gate. So `--targets en,ar,fa,ur`
on an `fa` source means: `fa` verbatim (skip), `en` human-reviewed, `ar`+`ur` AI-auto-translated.

For clip/selection projects (process only certain windows), see the README's
[Selecting and clipping the source](README.md#selecting-and-clipping-the-source-selection--join_clips).

**Decisions front-loaded at init (clone ON by default).** To cut friction on the scripted/manual
route, `project init` now captures several choices up front (the `/new-video` interview asks each
as an `AskUserQuestion`):

- `--no-clone` — opt OUT of voice cloning (neutral dub voice). **By company policy cloning is ON
  by default** (an authorized override of rule 5, see `company.default.json` `rights` block): when
  cloning is on, init auto-records `voice_clone_consent: true` + `rights_status: self-authored` in
  the rights record **through the sanctioned `rights set` path** (a `RIGHTS_SET` event is
  appended), so `--clone` dubbing is unblocked and `PACKAGE → READY_FOR_REVIEW` is pre-cleared.
  `--no-clone` records no consent and leaves the neutral rule-5 path intact.
- `--mux-mode {soft-subs,burned-in,no-subs}` — the default subtitle handling used by `package mux`
  when no per-run `--mode` is given (soft-subs = toggleable track; burned-in = painted into the
  picture; no-subs = clean video + sidecar files). Recorded as `project.yaml mux_mode`.
- `--glossary <id>` — pin a per-channel/speaker terminology glossary (`glossary/<id>.json`). List
  what's available with `vid glossary list`. Omit for none.
- `--images "<lang>=<path>,…"` — per-language **still image**: that language's picture becomes one
  fixed image shown for the whole dub, instead of the source video (dub over a static frame; needs
  no source video; also renders faster). A `lang=value` comma map; a language absent keeps the
  source video. Stored as `project.yaml images`. Change later with `vid project set-image`.
- `--speeds "<lang>=<factor>,…"` — per-language **playback speed**: a deliberate **uniform**
  whole-video speedup applied at mux (audio+video scale together → stay in sync; the fix for a slow
  source that drags in en/ur). Default 1.0x; a `lang=factor` comma map (a language absent → 1.0x).
  Stored as `project.yaml playback_speed`. Change later with `vid project set-speed`.

Both `--images` and `--speeds` use the `lang=value` comma-map convention; omit the flag entirely
when no language needs it.

**Revert the clone-on default company-wide:** set `rights.default_voice_clone: false` in
`company.local.json` (or `company.default.json`) — new projects then default to the neutral rule-5
voice with no consent recorded at init. The three outward-facing gates (`rights-check`,
`release_authorization`, `promotion_review`) stay human-only regardless, and a human can always
revise the rights record at the `rights-check` gate (a later `rights set` fully overwrites the
init-written one).

---

## 4. Walk the pipeline

Each stage has a **skill** (invoke with `/<skill-name>` in Claude Code) and the CLI verbs it
drives. Human gates are marked 🔒 — an agent prepares the packet, a **human** runs the
`approval grant` / `rights set`.

> **Token-thrifty alternative:** instead of driving each stage through Claude, run the whole
> deterministic chain via the scripted route — `/process-manual <id>` (or directly
> `bash .claude/scripts/run_pipeline.sh <id> --mt`). It runs ingest→…→dub with **no Claude
> tokens** (translation-fill via the MT engine, §6) and stops at the first human gate; then tell
> Claude *"the manual process for `<id>` is done"* and it does the editorial QA + gate walk.
> The per-stage Claude skills below remain the alternate route when you want Claude authoring.

### How a human gate feels (you decide in plain language; the agent does the plumbing)

You never type a CLI command to clear a gate. You decide in conversation; the agent executes
(CLAUDE.md rule 13):

1. **It surfaces the gate.** Ask "what's the current project, what stage is it at?" in *any* session
   and if it's waiting on you the agent says so — "it's at human approval, the `<gate>` gate" — and
   leads into the review.
2. **It presents the decision as options to pick from** — e.g. for a flawed transcript cue: *accept
   as-is* / *you edit the source in VS Code* / *you dictate the fix and I write it via the CLI* — plus
   an explicit **Approve & advance** option. Approve is an option you *select*, not a command you run.
3. **On approve, it discloses and asks you to confirm** — the gate, the exact artifact SHA-256 it will
   bind, the relative path(s) under review, and any concerns it wants you to look at (each briefly
   explained). Your confirmation is the recorded decision.
4. **Then it runs the grant + transition for you** — no `!`, no pasting. It records you as the
   approver and bakes the decision + concerns into the notes.

The agent only grants/transitions **after** that explicit confirmation, and never to unblock its own
work. If you'd rather run the command yourself, ask and it'll hand you an `!` block — but you don't
have to.

**Exception — the three outward-facing / legal gates** (`rights-check`, `video-release-authorize`,
`video-promote-approve`) keep the tighter posture: the agent gathers evidence, presents options, and
discloses, but **you** run the final `!` command (the skills are `disable-model-invocation: true`).
Flags differ by subcommand — `approval grant` uses `--approver`, `project transition` uses `--actor`;
the agent verifies with `--help` and fills them in, so you don't:

```bash
! vid approval grant <id> --gate <gate> --approver "Your Name" --scope <scope> \
    --artifact sha256:… --notes "<your decision>" \
  && vid project transition <id> --to <NEXT_STATE> --actor human
```

```bash
# LANGUAGE_ID — set the source language (auto-detect is a stub without ASR)
vid langid set yt-YP0FDR7Wc-8 --language fa --source manual --confidence 0.95
vid project transition yt-YP0FDR7Wc-8 --to TRANSCRIPTION --actor agent

# TRANSCRIPTION — with an ASR engine installed:
vid transcript run yt-YP0FDR7Wc-8 --model mlx-community/whisper-large-v3-turbo --advance
#   …or, no engine / have a transcript already:
vid transcript import yt-YP0FDR7Wc-8 --from my-transcript.json --advance
vid transcript qa yt-YP0FDR7Wc-8
# ENGLISH REVIEW-GLOSS (always, at this gate): produce a faithful English gloss of the source
# speech and AI-context-check it so the human can validate meaning — the source transcript, the
# English gloss, or both — before approving. Findings fold into the transcript-qa report (advisory).
vid transcript english-export yt-YP0FDR7Wc-8
#   … run the /english-context-check skill (Opus-4.8/high): it fills the gloss, does the
#     context/word-sense pass, fixes the English in place, and flags source problems …
vid transcript english-import yt-YP0FDR7Wc-8
vid transcript qa yt-YP0FDR7Wc-8   # re-run: now folds in the gloss's english-context/* findings
# 🔒 human: vid approval grant yt-YP0FDR7Wc-8 --gate transcript_qa --approver "Name" --artifact <sha256> --scope transcript
vid project transition yt-YP0FDR7Wc-8 --to SEGMENT_RESOLUTION --actor human

# SEGMENT_RESOLUTION — resolve selection (none → whole video) and advance
vid segments resolve yt-YP0FDR7Wc-8 --advance

# TRANSLATION — per language: export worksheet → Claude translates → import
# NOTE: the SOURCE language is never translated. If source_language is also a target (e.g. a
# Persian video with fa in --targets), `translate export --language fa` writes VERBATIM source
# captions (target==source) instead of an empty worksheet, marks that track done, and the fa
# track is excluded from the translation quorum and the translation_qa gate.
# TWO-AXIS SCOPE: only the source language and `en` get a HUMAN worksheet + human gate. Every
# other target is `auto_translate` — Claude fills its worksheet and imports it the same way, but
# it has NO human gate (deterministic QA only). So you walk the human gate for `en` alone; the
# ar/fr/es/… tracks just need to pass `translate qa` and are carried by the top-level --advance.
vid translate export yt-YP0FDR7Wc-8 --language en
#   … fill target_text in captions/en.worksheet.json (Claude does this; for auto tracks Claude
#     also fills them — same command, just no human approval afterward) …
vid translate import yt-YP0FDR7Wc-8 --language en --advance
vid translate qa yt-YP0FDR7Wc-8          # aggregate over ALL tracks (human + auto)
# 🔒 human: vid approval grant … --gate translation_qa --lang en --scope captions --artifact <sha256>
#   (only `en` needs this — auto_translate targets are not in the gate)
vid project transition yt-YP0FDR7Wc-8 --to CAPTION_TIMING --actor human

# CAPTION_TIMING / CAPTION_VALIDATION
# NOTE: `translate import` (and the source-verbatim path) now AUTO-SPLIT any caption cue longer
# than quality_bars.captions.max_cue_duration_ms (default 7000ms) into N proportional ≤-cap
# sub-cues — time divided into equal integer-ms slices, text divided at sentence→word→char
# boundaries, sub-cues renumbered 0..M. This is deterministic (stable re-render hashes) and
# CAPTION-ONLY: the human-approved source transcript and the TRANSCRIPT_QA_GATE English gloss keep
# their original cues and are never re-timed. So the caption cue-list can be finer than the 30-cue
# transcript, and `captions validate` no longer sees "very long" cues from long ASR merges.
vid captions build yt-YP0FDR7Wc-8 --language en
vid captions validate yt-YP0FDR7Wc-8

# DUBBING (dub-enabled langs only) — with a TTS engine, or import a rendered WAV
# INCREMENTAL: to add dubbing later ("add es dubbing for <id>") turn it on without re-translating —
#   the dub reuses captions/captions.es.json (a non-clone dub has NO source-video dependency):
#     vid project enable-dub yt-YP0FDR7Wc-8 --targets es    # flips dub_enabled on an existing track
#   No such track yet? `project add-languages … --targets es` first, then enable-dub. To re-render
#   ONE already-produced dub after AUDIO_QA_GATE (fix just `ar`, keep en/ur/zh), use the surgical
#   `project redub … --targets ar` (not `project reset`, which wipes every track) — see §8.
#   A `--clone` dub (or `package mux`) that finds source/ deleted
#   re-fetches it via `ingest ensure` — flag-gated, so VIDTRANS_FETCH_ENABLED=1 (else clean FetchDisabled).
# FREEZE-FRAME: if the neutral dub voice runs slower than the source and `dub qa` reports
#   CONDITIONAL_PASS with `audio-stretch-over-cap` cues, don't crank max_time_stretch — turn on
#   freeze-frame (re-times the picture to the audio instead of over-speeding it). See §9.
# MULTIPLE LANGUAGES: run `dub run` once PER language, sequentially — one ffmpeg-heavy job at a
#   time (two concurrent dubs/muxes can contend → ffmpeg dies on banner, `concat failed (232)`).
#   Concat now uses the demuxer (1 handle), so a single long dub is robust. A non-clone dub needs
#   NO env (no `source .env.local`) — it's local piper+ffmpeg over the already-built captions.
vid dub run    yt-YP0FDR7Wc-8 --language en --advance
vid dub import yt-YP0FDR7Wc-8 --language en --from my-dub.wav --advance
vid dub qa yt-YP0FDR7Wc-8
# 🔒 human: vid approval grant … --gate audio_qa --lang en --scope audio --artifact <sha256>
vid project transition yt-YP0FDR7Wc-8 --to VIDEO_MUX --actor human

# VIDEO_MUX / FINAL_QA / PACKAGE
vid package mux yt-YP0FDR7Wc-8 --language en --mode soft-subs --advance    # or burned-in / no-subs
vid package final-qa yt-YP0FDR7Wc-8
# 🔒 human: vid approval grant … --gate final_qa --scope video --artifact <sha256>
vid package build yt-YP0FDR7Wc-8 --advance

# 🔒 human: rights must be distributable before the last edge opens
vid rights set yt-YP0FDR7Wc-8 --status self-authored --reviewer "Name"
vid project transition yt-YP0FDR7Wc-8 --to READY_FOR_REVIEW --actor human   # autonomous stop
```

Deliverables land in `projects/<id>/packages/<lang>/`.

---

## 5. Getting a `--artifact <sha256>` for a gate

Gate approvals bind to an exact file hash. List the current artifacts to copy the right one:

```bash
vid artifact list yt-YP0FDR7Wc-8
```

Editing an approved artifact auto-invalidates its approval — `plan` will show the gate closed
again. Don't re-grant without checking whether the file actually changed (README edge cases).

---

## 6. Installing optional engines

All ASR/TTS engines are opt-in and go **into the venv** (found without activation):

```bash
source .env.local                        # engines download models from HuggingFace → needs egress + CA bundle
.venv/bin/pip install mlx-whisper        # ASR, Apple Silicon (default)
.venv/bin/pip install faster-whisper     # ASR, CPU fallback
# TTS: kokoro (en) / piper (fa,ur) / TTS==Coqui XTTS / a vendor CLI
vid doctor                               # confirm they now show `pass`
```

Pick a real model for real work — `whisper-large-v3-turbo` is a good quality/speed balance;
the default `whisper-tiny` is only adequate for smoke tests.

> **Known environment gotcha:** model downloads pull from `huggingface.co`. On this network HF
> is **blocked by corporate policy** (Prisma Access `edl-custom_saas_sites_block` — the `503` is a
> block page, not an outage; github/pypi/youtube work). Validate before assuming an outage:
> `SSL_CERT_FILE=$HOME/certs/aipe-certs.pem .venv/bin/python3 -c "import urllib.request as u; print(u.urlopen('https://huggingface.co', timeout=10).status)"`
> — a `503` with an `edl-`/"SaaS sites block" body confirms the policy block. When blocked,
> transcription/TTS park cleanly; use the offline-model recipe below.

### Offline model when HuggingFace is blocked

github.com/api.github.com are proxy-allow-listed, so stage the model through a GitHub repo and
reconstruct the HF cache offline. One-time per model:

```bash
# 1. On an UN-PROXIED device (HF reachable), download the model repo, e.g.:
#    huggingface-cli download mlx-community/whisper-large-v3-turbo --local-dir ./whisper-turbo-stage
# 2. Push the files to a GitHub repo and attach the large weights as a Release asset
#    (the repo must have at least one commit before `gh release create` — an empty repo 422s):
gh repo create <you>/whisper-turbo-stage --private
#    ...commit a README, push, then:
gh release create v1 --repo <you>/whisper-turbo-stage
gh release upload  v1 weights.safetensors config.json README.md --repo <you>/whisper-turbo-stage

# 3. On THIS (proxied) device — github works, so download the assets to a stage dir:
gh release download v1 --repo <you>/whisper-turbo-stage --dir ~/Dev/my-repos/pub/whisper-turbo-stage
```

Then reconstruct the HF cache layout as **symlinks pointing at the stage dir** (HF resolves a
model from `refs/main` → `snapshots/<rev>/<file>` → `blobs/<sha256>`):

```
<HF_HOME>/hub/models--mlx-community--whisper-large-v3-turbo/
  refs/main                     # a revision id string (any stable token, e.g. offlinestage0000…)
  blobs/<sha256>                # symlink -> whisper-turbo-stage/<file>   (one per file)
  snapshots/<rev>/<file>        # symlink -> ../../blobs/<sha256>         (one per file)
```

This project uses `HF_HOME=~/Dev/my-repos/pub/.cache/huggingface`, with the blobs symlinked to
`~/Dev/my-repos/pub/whisper-turbo-stage/`. **Deleting `whisper-turbo-stage/` breaks the cache
symlinks** — keep it. Run transcription fully offline (no egress, no CA bundle needed):

```bash
export HF_HOME=~/Dev/my-repos/pub/.cache/huggingface
export HF_HUB_OFFLINE=1
vid transcript run yt-YP0FDR7Wc-8 --model mlx-community/whisper-large-v3-turbo --advance
```

### Transcription decode flags + auto-retry ladder

`transcript run` auto-runs an escalating ladder of decode settings, validating each attempt
(min cue granularity ≈ 1 cue/10s, no repetition-hallucination run ≥ 3 identical cues, no cue
> 30s) and keeping the first clean result. Rejected attempts are backed up under
`transcript/engine/attempts/`; if every rung fails, the best attempt is kept and a
`TRANSCRIPTION_QUALITY_WARNING` event + `quality_problems` are surfaced (never a silent pass).
The QA gate (`transcript qa`) independently FAILs on a repetition run and CONDITIONAL_PASSes on
too-coarse output.

Override the ladder to run a single configuration (e.g. to fight a repetition loop directly):

```bash
vid transcript run <id> --model <m> --no-condition-on-previous-text \
  --hallucination-silence-threshold 2.0 --no-retry
# --temperature <t> also available (default 0 = deterministic)
```

### MT engine (translation-fill for the scripted/manual route)

The token-thrifty scripted route (`/process-manual`, `run_pipeline.sh --mt`, `project autopilot
--mt`) fills translation worksheets + the rule-11 English gloss with a **machine-translation
engine** instead of Claude — the one otherwise-non-deterministic step. It is **opt-in**; the
Claude route (`create-closed-captions`) still works unchanged when no MT engine is installed.

Enable it by pinning `translation.mt_baseline` in the gitignored `tools.local.json` (or leave it
`null` in `tools.default.json` to auto-select the first installed engine), then install the
engine into the venv. Supported providers (`engines/mt.py`): `argos`, `ctranslate2`, `nllb`,
`opus-mt` — each driven as a stdin→stdout subprocess (no in-process import, no network in the
adapter).

```bash
.venv/bin/pip install argostranslate          # simplest: offline .argosmodel packages
# pin it (or another engine) in tools.local.json:
#   { "translation": { "mt_baseline": "argos" } }
vid doctor                                     # engine:mt should now list the provider
vid translate machine-fill <id> --language en  # fills empty cues; does NOT import (autopilot does)
```

HF is policy-blocked here, so **stage MT model data offline** exactly like ASR/TTS — for Argos,
download the `.argosmodel` language packages on an un-proxied device, ship them via a GitHub
Release, and install them locally (`argospm install` from the local file, or point the engine at
the staged package dir). Follow the "Offline model when HuggingFace is blocked" recipe above.
With no MT engine installed, `translate machine-fill` / `autopilot --mt` fail with a graceful
`EngineUnavailableError` (install an engine, translate via Claude, or fill by hand) — never a
crash. MT-filled `en` still hits the human `translation_qa` gate; `auto_translate` targets skip
it (rule 7) — MT is just the cheap draft the human/QA then reviews.

### Dub voices & gender (male is the hard default)

Dub voices are **owned by the company**, not hand-picked per run. `dub run` (with no `--model`)
auto-selects a piper `.onnx` from the gender-tagged registry `dubbing.voices[<lang>][male|female]`,
resolving the gender as **`--gender` (CLI) → project `dubbing.voice_gender` → company
`default_voice_gender` (male)**. If the resolved gender isn't staged for that language, `dub run`
**fails loudly** with a staging message — it never silently dubs the wrong gender. An explicit
`dub run --model <path>` still overrides (operator escape hatch).

- **Shape** lives in `.claude/config/company.default.json` (`default_voice_gender: "male"` + an
  empty per-language `voices` map, kept portable — no machine paths).
- **Real absolute `.onnx` paths** go in the gitignored `.claude/config/company.local.json`, which
  deep-merges over the default:
  ```json
  "dubbing": {
    "voices": {
      "en": { "male": { "provider": "piper", "model": "/abs/…/en_US-ryan-high.onnx" } },
      "ar": { "male": { "provider": "piper", "model": "/abs/…/ar_JO-kareem-medium.onnx" } }
    }
  }
  ```
- **Staging a voice** = same offline pattern as any piper model: download the voice on an
  un-proxied device (piper voices ship as `<voice>.onnx` + `<voice>.onnx.json` — the `k2-fsa/
  sherpa-onnx` GitHub releases mirror them, so no HuggingFace needed), stage under
  `~/Dev/my-repos/pub/piper-voices-stage/`, and add the abs path to `company.local.json` under the
  right `<lang>.<gender>`. Then `vid doctor` lists it (`dub-voice:<lang>:<gender>` → present/absent).
- **Gender decisions.** The project's dub gender is set once at onboarding (`/new-video` →
  `project init --dub-voice-gender`, default male; `--voice-clone-requested` records cloning
  **intent** only — consent is still the `rights-check` gate). The **source speaker's** gender is a
  separate human note at LANGUAGE_ID (`langid set --source-voice-gender`, default male); a female
  source is the only prompt to consider female dubs — otherwise everything stays male. To render one
  project's dubs female without changing its stored gender, pass `dub run --gender female` (needs a
  staged female voice per language).

> **piper male-voice availability.** en/es/ru/fa/ar/ur all have solid male piper voices
> (`en_US-ryan`, `es_*`, `ru_*`, `ar_JO-kareem`, `ur_PK-fasih`, …). **zh (Mandarin) male options
> on piper are thin** — if no acceptable male zh voice exists, keep zh **female as a disclosed,
> recorded exception** (note it at the `audio_qa` gate) rather than shipping a broken track.

**Running multiple dubs — one at a time, no network env.** When several languages need dubbing
(e.g. re-dub en + first-dub es/ru), run them **sequentially, one `dub run` per command**, each
fully returning before the next:

```bash
vid dub run yt-YP0FDR7Wc-8 --language en    # let it finish…
vid dub run yt-YP0FDR7Wc-8 --language es    # …then the next…
vid dub run yt-YP0FDR7Wc-8 --language ru    # …then the next.
```

- **Run only ONE ffmpeg-heavy job at a time** — don't background two `dub run`s (or a dub + a
  `package mux`) concurrently, and don't chain them in a shell `for … do … done` loop that overlaps.
  **Root cause (verified 2026-08-07):** the dub's final concat used to open one ffmpeg input handle
  **per cue** (400+ on a long speech) via an N-way `filter_complex`; under concurrent ffmpeg load
  that exhausted process/FD resources and ffmpeg died early emitting only its version banner
  (`rc 232`), surfaced as a misleading `ffmpeg concat failed (232)`. Synthesis, tempo-fit, and
  concat each pass in isolation — it was contention, not data. **Fixed in code:** `concat_wavs` now
  uses ffmpeg's **concat demuxer** (a single `-i playlist.txt`), opening **one** handle regardless
  of cue count, so a single long dub is robust. Running one job at a time is still the rule (two
  concurrent ffmpeg jobs can contend regardless).
- **A non-clone `dub run` needs NO env** — no `source .env.local`, `VIDTRANS_FETCH_ENABLED`, or CA
  bundle. It is fully local (piper TTS + ffmpeg) over the already-built captions. The fetch flag + CA
  bundle (§0) are **only** for media downloads: `ingest run`/`ingest ensure`, a `--clone` dub, or a
  `package mux` that must re-fetch deleted source. Prepending `source .env.local` to a plain dub is
  unnecessary (and errors if the file doesn't exist).
- **`dub qa` is aggregate** (no `--language`) — run it **once** after all the language dubs are
  rendered; it QAs every dubbed track together.

---

## 7. Phase 6 (distribution) — opt-in, human-bound

Off by default. It never uploads without a human clearing rights, passing the
`release_authorization` gate against the exact final-video hash, **and** both publish flags
set. With the flags unset every uploader/poster is a no-network dry-run. See the README's
[Distribution and promotion](README.md#distribution-and-promotion-phase-6-opt-in) for the
full flow.

```bash
vid distribute youtube  yt-YP0FDR7Wc-8 --language en    # dry-run by default
vid distribute promote  queue yt-YP0FDR7Wc-8 --advance  # drafts posts; stops at promotion_review
```

---

## 8. Housekeeping

```bash
vid catalog list                                     # every ingested source video
vid budget status                                    # vendor spend ceiling / spent / remaining
vid event tail yt-YP0FDR7Wc-8                         # recent lifecycle events for a project
vid framework validate                               # state-machine + schema self-check

.venv/bin/python3 -m pytest tests/ -q                # test suite
.venv/bin/python3 -m ruff check .claude/scripts .claude/mcp .claude/hooks tests
```

### Reset / delete a project

Start a project over — or remove it entirely — **through the CLI**, never with `rm -rf` +
hand-editing `catalog/videos.json` (that violates the CLI-owns-state rule and the pre-tool hook
denies it). Both are recorded as events (append-only history is preserved, not rewritten).

```bash
# Reset: wipe downstream work and rewind. By DEFAULT keeps the (expensive) source/ media and the
# source ASR transcript and resumes at TRANSCRIPTION, so the transcript-QA gate can be re-run
# without re-downloading or re-running ASR. Clears gloss, QA reports, reviews, approvals,
# segments, captions, audio, video, packages, chapters, distribution, rights.
vid project reset yt-YP0FDR7Wc-8
vid project reset yt-YP0FDR7Wc-8 --to LANGUAGE_ID     # rewind further
vid project reset yt-YP0FDR7Wc-8 --drop-transcript    # keep media, re-run ASR from scratch
vid project reset yt-YP0FDR7Wc-8 --full               # drop source+transcript too, back to INGEST

# Delete: permanently remove the project directory AND its catalog entry (destructive).
vid project delete yt-YP0FDR7Wc-8
vid project delete yt-YP0FDR7Wc-8 --keep-catalog      # remove the dir, leave the catalog entry
```

Never hand-edit `state.json`, `manifest.json`, approvals, or events — the CLI owns them, and
the pre-tool hook hard-denies those writes.

### Add languages to a project already in flight

To *widen* an in-progress project's language set — add more translation/dub tracks without
throwing away work already done — use `project add-languages`. This is the sanctioned
alternative to `reset` + re-`init` (which would discard existing translations, captions, and
approvals). It appends fresh tracks at the `TRANSLATION` stage, leaves every existing track,
approval, and registered artifact untouched, updates both `state.json` and the project config,
and logs a `LANGUAGES_ADDED` event.

```bash
# Add 5 new languages, each translated AND dubbed (default: every added language is dub-enabled).
vid project add-languages yt-YP0FDR7Wc-8 --targets zh,fr,es,pt,ru

# Add captions-only for some (dub only the subset you name via --audio).
vid project add-languages yt-YP0FDR7Wc-8 --targets zh,fr --audio zh
```

The new tracks are picked up automatically by the translation stage — run `translate export
--language <new-lang>` when you're ready to produce them. Adding a language that's already a
target is a no-op (idempotent, no event). If a newly-added language equals the confirmed
`source_language`, it's marked `skip_translation` (rule 7: source→source isn't translated; it
still gets verbatim captions); a non-en/non-source add is marked `auto_translate` (AI-translated,
deterministic QA only, no human gate — see the two-axis scope in §4). **Reset vs. add:** `reset`
rewinds and rebuilds; `add-languages` widens in place.

### Turn on dubbing for an already-translated language

To add **audio** for a language that already has captions ("add es dubbing for <id>"), don't
re-translate or re-init — flip `dub_enabled` on the existing track. A non-clone dub reuses that
track's `captions/captions.<iso>.json` and has **no** source-video dependency.

```bash
vid project enable-dub yt-YP0FDR7Wc-8 --targets es        # idempotent; adds es to audio_languages, logs DUB_ENABLED
vid project enable-dub yt-YP0FDR7Wc-8 --targets es,pt,ru  # several at once
```

To turn dubbing **off** for a track, use the same verb with `--disable` (removes it from
`audio_languages`, logs `DUB_DISABLED`). It refuses if a dub was already produced (an active
`dub-wav@<lang>` artifact would be stranded, rule 6) unless you add `--force`:

```bash
vid project enable-dub yt-YP0FDR7Wc-8 --targets fr,es --disable          # captions-only from now on
vid project enable-dub yt-YP0FDR7Wc-8 --targets fr --disable --force     # override the rule-6 guard
```

If the named track doesn't exist yet, run `project add-languages … --targets es` first (that
creates the translate track), then `enable-dub`.

### Re-render one dub track after the audio gate (`redub`)

When a single language's dub needs re-rendering **after** the project has advanced past
`AUDIO_QA_GATE` — e.g. you fixed the freeze/trim plan for `ar` but en/ur/zh are already correct
and approved — use the **surgical** `project redub`, not `project reset` (which would wipe ALL
downstream work for every track):

```bash
vid project redub yt-YP0FDR7Wc-8 --targets ar        # rewind top state + reset only ar
```

It pulls the top `current_state` **backward** to `AUDIO_SYNC_ADJUST` (a dub-allowed state) only
if the project had advanced past it, resets **only** the named dubbed track(s) to the dubbing
stage, and leaves every other track + all artifacts + all approvals untouched. It refuses a
captions-only track (`enable-dub` first) and never pushes state forward. Then re-dub and re-gate
that one language:

```bash
vid dub run yt-YP0FDR7Wc-8 --language ar --provider piper --model <ar.onnx>   # fresh dub-wav supersedes; rule 6 invalidates the stale audio_qa approval
vid dub qa  yt-YP0FDR7Wc-8                                                     # aggregate over ALL tracks — no --language; expect the ar block PASS
# …then re-surface the ar audio_qa gate (rule 13 disclose+confirm) and re-mux ar.
```

> **`dub qa` takes no `--language`** — it QAs every dubbed track at once and writes the aggregate
> `audio-sync` gate report. `dub run` **is** per-language (`--language <iso>`); `dub qa` is not.

(The old advice to `project reset … --to CAPTION_VALIDATION` for a post-gate redub is superseded
by this verb — reset is the whole-project teardown; `redub` is the one-track rewind.)

### Reconcile two-axis markers on a pre-feature project (`sync-scope`)

A project created **before** the two-axis translate/dub feature has tracks with no
`skip_translation`/`auto_translate` markers. `project sync-scope <id>` recomputes both markers for
every track from the confirmed `source_language` and the fixed `en` human-review rule: the source
becomes `skip_translation`, `en` + the source stay human-reviewed (no `auto_translate`), every
other translatable target becomes `auto_translate`. It's a true recompute (a marker that no longer
applies is removed) and **idempotent** — a no-op once markers are correct.

```bash
vid project sync-scope yt-YP0FDR7Wc-8      # logs SCOPE_SYNCED if anything changed
```

It never touches `dub_enabled`, stages, artifacts, or approvals — a recorded `translation_qa`
approval on `en` stays valid (rule 6). Requires a confirmed `source_language` (run `langid set`
first). New projects need no `sync-scope` — `project init` + `langid set` already mark tracks
correctly.

### Re-fetch a deleted source video (`ingest ensure`)

Once a project moves past INGEST you may delete the (large) `source/` media to save space. Two
operations still need it: a `--clone` dub and `package mux`. Both call `ingest ensure` first,
which is a **media-restore** — it re-downloads + re-extracts + re-registers the source artifacts
without touching top-level state. It's flag-gated like any media download:

```bash
VIDTRANS_FETCH_ENABLED=1 vid ingest ensure yt-YP0FDR7Wc-8   # no-op if source/ still present
```

With the flag unset and the media missing you get a clean `FetchDisabled`, not a crash.

### Onboard a video or a whole playlist

`/new-video <url>` (skill) confirms the translate + dub sets via two checklists, then
`project init` + `ingest run`. A **playlist** URL routes to `catalog add-playlist` instead, which
**enumerates** the playlist (titles/ids only) and indexes each video under it. Enumeration is the
one sanctioned **flag-free** network call (metadata only, no media) — the per-video media download
still needs `VIDTRANS_FETCH_ENABLED=1`.

```bash
vid catalog add-playlist "https://youtube.com/playlist?list=..."   # flag-free; indexes each video
vid catalog playlists                                              # list known playlists
vid catalog playlist <playlist-id>                                 # the playlist's videos, enriched
vid catalog list                                                   # every catalogued video, enriched
vid catalog show yt-XXXX                                           # one entry, enriched
```

`list` / `show` / `playlist` enrich each entry **on read** (never persisted) with a derived
`next_command` — a literal, `!`-runnable command for that video's current phase (kickoff if it has
no project yet, the next pipeline verb while it PROCEEDs, the approve command at a gate, a
`rights set` when BLOCKED, null when TERMINAL) — and, at a gate step only, a `review_files` array
of the relative artifact paths under review. Re-adding a playlist that contains an
already-catalogued video **moves that entry under the playlist** (keeping its `project_id`,
`rights_status`, and full status) rather than duplicating it. The `next_command` is a convenience
shortcut; it does **not** bypass the gate protocol (rule 13) — when the agent drives a gate in
conversation it still discloses and confirms before granting.

### Per-language still image + playback speed (`set-image` / `set-speed`)

Set (or change) a per-language **still image** or **playback speed** on an in-flight project —
the same two settings `project init --images/--speeds` capture at onboarding — without re-init.
Both write `project.yaml` through the CLI (a `lang`-keyed map) and log an event; `package mux`
picks them up automatically (config-driven, no mux flag).

```bash
# Still image: this language's picture is one fixed image for the whole dub (dub over a static
# frame), instead of the source video. Needs no source video; renders faster. Meant for a
# dub-enabled track (there must be a dub to lay over it). Logs IMAGE_SET.
vid project set-image yt-YP0FDR7Wc-8 --language ur --path ~/art/ur-cover.png
vid project set-image yt-YP0FDR7Wc-8 --language ur --clear          # revert ur to source video

# Playback speed: a deliberate UNIFORM whole-video speedup applied at mux (audio+video scale
# together → stay in sync; the fix for a slow source that drags in en/ur). Logs SPEED_SET.
vid project set-speed yt-YP0FDR7Wc-8 --language en --factor 1.25
vid project set-speed yt-YP0FDR7Wc-8 --language en --factor 1.0     # 1.0 resets (removes the entry)
```

The speed is applied **last** at mux, after the picture+audio pair is built (still-image or
freeze-retimed) — it scales the whole finished track uniformly, so it stays in sync and is fully
compatible with the constant-audio rule (§9): the per-cue audio is still natural, only the global
tempo is faster.

### Re-time any existing video, and compute 16:9 dimensions (`speed` / `size`)

Two standalone, project-free verbs:

```bash
# speed: uniformly re-time ANY video file (audio+video by the same factor → stay in sync). The
# SOURCE IS NEVER TOUCHED; output lands beside it as <stem>_<factor><suffix> unless --out is given.
vid speed 1.25 projects/yt-YP0FDR7Wc-8/video/en/dubbed.mp4          # → …/en/dubbed_1.25.mp4
vid speed 2 some/clip.mp4 --out /tmp/fast.mp4                       # explicit destination

# size: full 16:9 (YouTube) dimensions from a single given width OR height (rounded to even for
# H.264). Handy for sizing a still image to the canvas before `set-image`.
vid size w 1920        # → 1920x1080
vid size h 1080        # → 1920x1080
vid size w 1042        # → even-rounded WxH
vid size h 480 --aspect 4:3
```

## 9. Constant audio speed + freeze-frame dubbing (mandatory)

**Hard rule (TASK 2 / rule 5): dubbed audio always plays at its natural TTS speed — it is never
time-stretched per cue.** Per-cue tempo-stretch produced audible rubber-banding, so it was removed
entirely. The *picture* absorbs 100% of every cue mismatch (freeze/trim), which makes freeze-frame
the **mandatory, only** behavior for every language — `quality_bars.audio.freeze_frame_enabled` is
now the company **default** (`true`), not an opt-in. The legacy stretch bars
(`max_time_stretch`, `freeze_stretch_cap`, `cumulative_drift_ceiling_ms`) are **reporting-only**:
still read for back-compat, but they no longer speed the audio.

## 9a. Freeze-frame dubbing for over-length dub tracks

**Symptom.** The neutral piper voice (the only working TTS engine here — see the piper note in
§6) speaks *slower* than fast source oratory, so many cues' synthesized audio runs longer than
their caption slot. Because the audio is **never** sped up (§9, the hard rule), the picture must
be re-timed to it — this is the mandatory freeze-frame mechanism.

**How it works (always on).** Freeze-frame re-times the picture to the audio: the audio plays at
natural length and the picture holds/trims to stay in sync. It is the company default
(`quality_bars.audio.freeze_frame_enabled: true`) — there is deliberately **no per-run CLI flag**
(an agent can't override company policy per-invocation). Optional companions: `max_freeze_ms_per_cue`
(default `4000` — a hold longer than this escalates to a human-visible finding). The legacy
`freeze_stretch_cap` / `max_time_stretch` bars are reporting-only (audio is never stretched).

**The plan is computed on a *running gap*, not each cue's own overflow.** `dub run` lays cue audio
**back-to-back** (it inserts lead silence only when a cue's audio would start *early*), so a long
cue's overrun pushes every following cue's audio later until a natural pause absorbs it. Freezing
only each cue's *own* overflow therefore leaves that propagated backlog uncancelled — on the
al-'Asr project it drove `en` cumulative drift to ~43s and **FAILed**. The correct plan holds the
picture by the running gap between the audio playhead and the already-retimed picture at each cue
boundary (`gap_i = rendered_start_ms − (caption_start_ms + cum_freeze − cum_trim)`).

**Two modes — pick per language with `freeze_trim_languages`.** The company bar
`quality_bars.audio.freeze_trim_languages` (an array of ISO codes, default `[]`; **no CLI flag**)
chooses each language's mode:

- **Hold-only (Model B — languages *not* in the list):** freeze/hold only, never drop source
  frames. Where the audio *underruns* the picture in aggregate a one-sided residual remains
  (picture lags audio); it's surfaced **honestly** as signed `drift_ms`. Reach `PASS` by raising
  `per_cue_drift_tolerance_ms` to cover it — a **disclosed, recorded** bar relaxation, appropriate
  only when the residual is small. On al-'Asr: en −3490ms, ur −404ms, zh −1021ms → raised
  `per_cue_drift_tolerance_ms` to `3600`.
- **Freeze + trim (Model A — languages *in* the list):** additionally **trims** the picture (skips
  source frames at the cue boundary) where the audio runs earlier than the picture, so the residual
  reaches **0 by construction**. Use when hold-only would leave an unacceptable lag — on al-'Asr,
  `ar`'s hold-only residual was **−11.6s**, so `freeze_trim_languages: ["ar"]`. The plan records
  `trims[]` + `total_trim_ms`; the sync report carries `trim_planned`/`trim_ms` per cue. Trimming
  drops real source frames (on cue boundaries) — an accepted trade vs a large desync.

```jsonc
// al-'Asr project overlay (yt-YP0FDR7Wc-8):
{ "quality_bars": { "audio": {
  "freeze_frame_enabled": true,
  "freeze_trim_languages": ["ar"],
  "per_cue_drift_tolerance_ms": 3600
} } }
```

**What changes when the bar is on:**

1. **`vid dub run`** fits each cue's audio only to the gentle `freeze_stretch_cap` (not
   `max_time_stretch`) and writes a per-language **freeze plan**
   (`audio/freeze-plan.<lang>.json`, a registered artifact tracing to the dub-wav) with `mode`
   (`hold`|`trim`), the running-gap `freezes[]`, and (trim mode) `trims[]`.
2. **`vid dub qa`** — a freeze/trim-planned cue's post-adjust `drift_ms` is `0` (Model A) or the
   honest one-sided residual (Model B) and drops out of the over-cap list **by construction**, so
   the track reaches `PASS` — *honestly* (Model A) or under the raised tolerance (Model B). Each
   freeze is an informational `audio-freeze-planned` note; a hold beyond `max_freeze_ms_per_cue`
   becomes an `audio-freeze-excessive` **major** so a person sees a frame that would linger.
3. **`vid package mux`** rebuilds the picture on the post-adjust timeline (source slices
   interleaved with frozen-frame inserts, **and trimmed regions dropped** in trim mode; re-encoded
   H.264/AAC) and re-times **both** the embedded soft-subs and — at **`vid package build`** — the
   standalone `captions.<lang>.vtt/.srt` deliverables onto that timeline. The **canonical approved
   caption doc is never edited** (rules 6/12); retimed subs are derived mux/package outputs.

**Scope limit.** Freeze-planning is the **real-TTS `dub run` path only**. `dub import` distributes
one whole-track offset with no per-cue natural durations, so it produces **no** freeze plan (its
result carries a `freeze_plan_skipped_reason`).

**No opting out.** Freeze-frame is mandatory (audio is never stretched), so there is no "turn it
off" — when a language's plan is empty (audio already fits every cue) mux naturally falls back to
the plain `-c:v copy` path with no re-encode. The old band-aid overrides (`max_time_stretch`,
`cumulative_drift_ceiling_ms`) are superseded and reporting-only.

**Tradeoff (accepted for v1).** Retiming re-encodes the whole picture (libx264 crf18) instead of
`-c:v copy` — slower and technically lossy on long videos, though crf18 is near-lossless and
matches the bar `slice_video`/`mux_video_burned_in` already use. Freezing mid-motion can look
unnatural on high-motion footage (fine for narration/static-speaker content); the
`max_freeze_ms_per_cue` escalation surfaces over-long holds for human review.
