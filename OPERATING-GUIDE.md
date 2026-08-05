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

For clip/selection projects (process only certain windows), see the README's
[Selecting and clipping the source](README.md#selecting-and-clipping-the-source-selection--join_clips).

---

## 4. Walk the pipeline

Each stage has a **skill** (invoke with `/<skill-name>` in Claude Code) and the CLI verbs it
drives. Human gates are marked 🔒 — an agent prepares the packet, a **human** runs the
`approval grant` / `rights set`.

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
# track is excluded from the translation quorum and the translation_qa gate. Only non-source
# targets get a worksheet to fill.
vid translate export yt-YP0FDR7Wc-8 --language en
#   … fill target_text in captions/en.worksheet.json (this is where Claude does the work) …
vid translate import yt-YP0FDR7Wc-8 --language en --advance
vid translate qa yt-YP0FDR7Wc-8
# 🔒 human: vid approval grant … --gate translation_qa --lang en --scope captions --artifact <sha256>
vid project transition yt-YP0FDR7Wc-8 --to CAPTION_TIMING --actor human

# CAPTION_TIMING / CAPTION_VALIDATION
vid captions build yt-YP0FDR7Wc-8 --language en
vid captions validate yt-YP0FDR7Wc-8

# DUBBING (dub-enabled langs only) — with a TTS engine, or import a rendered WAV
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

Never hand-edit `state.json`, `manifest.json`, approvals, or events — the CLI owns them, and
the pre-tool hook hard-denies those writes.
