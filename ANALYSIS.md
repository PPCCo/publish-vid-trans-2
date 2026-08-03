# Plan Analysis — `publish-vid-trans`

Analysis of `publish-vid-trans-plan.md` before implementation, grounded in a direct
study of the reference framework at `/Users/qaiser.abbas/Dev/my-repos/publish-book`
and this machine's actual capabilities.

---

## A. What the plan gets right (keep as-is)

The plan is strong on **product vision and domain risk** (rights, voice-cloning consent,
religious-terminology accuracy, drift, dialect). It also correctly identifies that the
`publish-book` operating model is the right skeleton. The following map cleanly onto the
reference framework and need no rethinking:

- File-based state, append-only events, hash-verified artifact manifest, human-bound
  approvals, "no external publish by default." These are **exactly** how `publish-book`
  works — the mechanics are directly portable (see §D).
- Data-driven lifecycle (`workflow_states.json` with `transitions` + `gate_edges`).
- Deterministic CLI owns all mutation; agents/skills only call it; MCP is read-only.
- `company.default.json` + gitignored `.local.json` deep-merge for config/secrets.
- Single sanctioned network module gated by an env flag (mirrors `net/fetch.py`).

---

## B. Critical gaps & hard problems (must resolve before/while building)

### B1. The reference CLI is pure-Python and shells out; the plan's stack is heavy ML — and this is an Apple M4 (no CUDA)

**This is the single biggest gap.** `publish-book`'s CLI depends only on PyYAML,
jsonschema, Pillow, reportlab — it never imports an ML model; it shells out to
`ffmpeg`/`pandoc`/`epubcheck`. The vid-trans plan (§5) assumes `faster-whisper`
(CTranslate2), WhisperX, NLLB-200, Coqui XTTS — a GPU/CUDA-oriented local-first stack.

Verified on this machine:
- **Apple M4 Pro, Metal only, no NVIDIA/CUDA.** `python3.14`. `ffmpeg`/`ffprobe` 8.1 present.
- `yt-dlp` **not installed**. `whisper`/`whisperx` **not installed**.

Consequences:
- `faster-whisper`/CTranslate2 runs **CPU-only** on Mac (no Metal accel) — slow but works.
  The Mac-native fast path is **`mlx-whisper`** (Apple MLX) or **`whisper.cpp`** (Metal).
- **WhisperX** forced-alignment/diarization pulls a heavy torch+pyannote stack that is
  awkward on Apple Silicon and Python 3.14. Diarization also needs a gated HF token.
- **XTTS-v2** on Mac-CPU is slow and its dependency tree is fragile on 3.14.
- **NLLB-200** local inference is heavy for marginal benefit when Claude does the
  translation anyway.

**Recommendation / resolution:**
1. **Keep the CLI pure-Python and orchestration-only**, exactly like `ph_cli.py`. It must
   never `import torch`. ASR/TTS/MT run as **external processes or vendor APIs** behind a
   `tools.local.json` adapter, invoked the same way `publish-book` shells out to ffmpeg.
2. Define an **engine-adapter abstraction** per capability (ASR, MT, TTS) with a
   `provider` field + `*_verified` flag in `tools.default.json` (this pattern already
   exists in the book repo). Providers are pluggable: `local-subprocess` vs `vendor-api`.
3. **Default engine matrix for this machine** (revisit if a CUDA box is added):
   - ASR: `mlx-whisper large-v3` (Metal) — or Groq hosted Whisper (free tier) as fallback.
   - Word-level timestamps: prefer `whisper.cpp`/`whisperX` alignment **if installed**;
     otherwise use segment-level timestamps + a documented accuracy caveat. Do **not**
     hard-block the pipeline on WhisperX being present.
   - Translation: **Claude** (Sonnet 5 volume / Opus 4.8 hero) via the agent layer — this
     is the natural strength and needs no local model. NLLB optional, off by default.
   - TTS: `en → Kokoro`; `fa/ur → Piper` or vendor; `zh/fr/es/pt/ru → XTTS or vendor`.
     All behind the adapter; none required for the pipeline to *validate*.
4. `th_cli.py doctor` must **probe every engine as optional** and degrade gracefully —
   the book's `doctor.py` already marks tools `required` vs `optional-missing`; reuse that.

**Decision needed from you:** local-first vs vendor-first default (drives B1.3 and cost).

### B2. Multi-language fan-out changes the state-machine shape (book is single-track; video is per-language)

`publish-book`'s lifecycle is one linear track per project. The vid-trans lifecycle from
`TRANSLATION` onward is **per-target-language and parallel** (en, ar, fa, ur, zh, …). The
plan's §3 lifecycle is written as if linear.

**Problem:** a single `state.json.current_state` can't represent "en is at DUBBING, ar is
at TRANSLATION_QA, fa failed CAPTION_VALIDATION." The book's `active_artifacts[type] = hash`
map is also single-valued per type.

**Recommendation:**
- Keep **one project-level lifecycle** for the shared, source stages
  (`INGEST → LANGUAGE_ID → TRANSCRIPTION → TRANSCRIPT_QA_GATE`).
- After the transcript gate, model per-language progress as **sub-state** inside the
  project, not as the top-level `current_state`. Concretely: a `language_tracks` object in
  `state.json` (`{ "ar": {"stage": "DUBBING", ...}, "fa": {...} }`), and key
  `active_artifacts` by **`(type, lang)`** rather than `type` alone.
- The top-level `current_state` advances to `PACKAGE`/`READY_FOR_REVIEW` only when a
  configurable quorum of language tracks reach their per-language terminal. Gate approvals
  bind to `(gate, lang, artifact_hash)`.
- This is a real schema/CLI change vs. the book, not a copy-paste. Budget for it.

### B3. Word-level timestamps, forced alignment, and the "captions.json is source of truth" claim

The plan (§4.2, §5.4) leans on WhisperX forced alignment for accurate word-level
timestamps and asserts `captions.<lang>.json` is canonical with SRT/VTT rendered from it.
That JSON-as-source-of-truth design is excellent and should be kept. But:

- Word-level alignment is the **least reliable** part on Apple Silicon (see B1). Design the
  `captions.<lang>.json` schema to carry **sentence/cue-level** timing as the contract, with
  word-level timing **optional/enrichment**. Don't let downstream stages (dubbing, mux)
  require word-level timing that may not exist.
- The SRT/VTT renderer must be a **deterministic script** (like the book's epub renderer),
  producing byte-stable output so artifact hashes are reproducible across rebuilds
  (the book already has `write_deterministic_zip`/`deterministic_modified` for exactly this).

### B4. Timestamp-fit / cumulative-drift is genuinely unsolved and needs to be a first-class, measured gate — not a "hope"

The plan honestly flags this (§6). To make it real:
- `sync-report.json` must be a **schema-validated artifact** with per-cue drift, stretch
  factor, and **cumulative offset**, and `AUDIO_QA_GATE` must read it and refuse to pass if
  cumulative drift exceeds a config ceiling (±500ms) — mechanically identical to how the
  book's `transition_blockers()` reads `*-gate-latest.json` and requires `decision == PASS`.
- Time-stretch cap (1.15–1.3×) and the "borrow silence / flag for paraphrase" fallback must
  be **implemented in the dub sync script**, with over-cap cues emitted as findings, not
  silently stretched.

### B5. Rights gate wiring, and the fact that ingest itself downloads third-party content

`publish-book` treats rights as a PASS-gated stage that blocks progression. The vid-trans
twist: **the very first stage (`INGEST`) downloads someone else's copyrighted video.** The
plan addresses redistribution (§7) but the *download* step needs its own guardrail.

**Recommendation:**
- `INGEST` sets `rights_status: "unreviewed"` and the pipeline runs to `READY_FOR_REVIEW`
  regardless (matches plan) — but **`PACKAGE`/any external step must be blocked** unless
  `rights_status ∉ {unreviewed, do-not-distribute}`, enforced in `transition_blockers()`.
- `yt-dlp` invocation must live **only** inside the single sanctioned network module and be
  **disabled unless an env flag is set** (`VIDTRANS_FETCH_ENABLED`, mirroring
  `RESEARCH_FETCH_ENABLED`), so "what can reach YouTube and why" is auditable in one file.
- Voice-cloning consent is a **hard gate**: dub-with-cloned-voice requires a recorded
  consent field in the rights record; default is neutral non-cloned voice.

### B6. Glossary / terminology consistency is described but has no data home or enforcement

§6 and §8 make glossary-term hit-rate a **hard gate** ("100% must appear correctly"). That
needs: (a) a schema'd glossary file (per channel/speaker, transliteration + canonical
English renderings), (b) injection into every translation prompt, (c) a deterministic
**checker** that scans translated cues for flagged source terms and verifies the expected
target rendering, emitting a PASS/FAIL report the `TRANSLATION_QA_GATE` reads. None of this
exists in the book (no analog) — it's net-new and non-trivial. Budget for it explicitly.

### B7. CJK caption rules (Mandarin) need a separate code path

§11 correctly notes Mandarin measures line length in **characters** and reading speed in
**chars/min**, with no whitespace to wrap on. The caption **validator** and **line-wrapper**
must branch on script class (CJK vs Latin/Cyrillic vs RTL Arabic/Persian/Urdu). RTL also
needs handling in burned-in rendering (bidi shaping) and font selection (Noto Sans
Arabic/CJK). This is easy to under-scope.

### B8. Distribution addendum (§12) is a whole second framework

The YouTube-upload + promotion addendum roughly doubles the surface (OAuth per channel,
YouTube Data API quota accounting, per-platform promotion adapters, Reddit-as-manual-guide).
It shares the human-only-gate posture but adds live external-write credentials. **This must
be a separate, explicitly-authorized extension phase**, not part of the core build — and the
core must remain fully functional (through `READY_FOR_REVIEW`) without it. The plan says as
much (§12 intro); the phasing below honors that.

---

## C. Smaller gaps / ambiguities to nail down

1. **Naming inconsistency:** plan says `th_cli.py` (from "translate house") in some places
   and describes a `publish-vid-trans` MCP server. Pick one package name. Suggest
   package `video_translation_house/`, CLI `vth_cli.py`, MCP `video_translation_house_server.py`.
   (Using `th_cli.py` verbatim is fine too — just be consistent.)
2. **`catalog/videos.json` write concurrency:** must use the same atomic-write + lock
   primitives as the book, and "merge, never silently overwrite; log a diff event" (plan §4.1)
   needs an actual merge implementation + `CATALOG_ENTRY_MERGED` event.
3. **video-id derivation:** must be deterministic and filesystem-safe (`yt-<11charID>`);
   define it once in `paths.py`-equivalent.
4. **Large binary artifacts:** hashing multi-GB MP4s with the book's 1MB-chunk streaming
   hasher is fine, but manifests should store size + hash and the artifact files must be
   **gitignored** (the book already gitignores `projects/*/outputs/`). Add
   `projects/*/source/`, `.../audio/`, `.../video/` to gitignore.
5. **Model IDs:** use only the three allowed Bedrock IDs. Effort and model are separate
   axes (plan §17 is right). Proposed defaults per agent in the phase plan.
6. **`disable-model-invocation: true`** on every human-only gate skill (approve/rights/
   release) — this is how the book enforces "Claude can prepare but not grant."
7. **Diarization/overlap** (Q&A lectures): plan wants VAD+diarization but that's the
   fragile HF-gated path (B1). Make diarization **optional**; when absent, flag overlapping
   speech for human review rather than failing.
8. **Tests:** the book has a `tests/` suite validating the framework (`framework validate`
   checks gate_edges ⊆ transitions, schemas load, etc.). Mirror this — a `framework validate`
   that self-checks the state machine and schemas is high-value and cheap.

---

## D. Machinery that ports 1:1 from `publish-book` (verified by direct study)

These are directly reusable patterns (rewrite for the video vocabulary, keep the shape):

- **CLI shape:** `th_cli.py` (16-line entrypoint) → `<pkg>/cli.py` (argparse, two-level
  subcommands, single `dispatch(args, root)`, JSON-to-stdout, errors-to-stderr exit code 2).
- **State engine:** `state.py` reads `workflow_states.json`; `transition()` locks, checks
  `allowed_transitions`, runs `transition_blockers`, atomic-writes, appends event.
- **Gate pattern:** gate lives on the **edge** (`gate_edges`), blocker = "valid approval
  for gate" + "`<stage>-gate-latest.json` decision == PASS".
- **Artifacts:** content-addressed (`sha256:` prefix), `register_artifact` dedupes,
  versions, sets `supersedes`, repoints `active_artifacts`, **auto-invalidates approvals
  bound to superseded hashes**, `freeze` on content-freeze-equivalent gate.
- **Provenance:** `source_artifact_ids` + open `provenance` dict per artifact (video built
  from video-hash + audio-hash + caption-hash — plan §4.4 maps directly).
- **Events:** `events.ndjson`, `append_event()` after every mutation, sorted-keys JSON lines.
- **Approvals:** bind approver + gate + **set of artifact hashes** + scope; validity is a
  pure function (`approval_is_current`); release authorization is single-use, N-approver,
  pinned to exact package-manifest hashes.
- **Paths/security:** `repo_root()` walks to `.claude/CLAUDE.md`; `ProjectPaths.require()` /
  `resolve_inside()` blocks path traversal; fixed `PROJECT_DIRS` skeleton at init.
- **Safety primitives:** `atomic_write_json` (temp+fsync+os.replace), `project_lock`
  (`fcntl.flock`), schema-validate-before-persist, deterministic zip/timestamps.
- **Hooks:** Python, wired per-event in `settings.json`; `pre_tool_policy.py` structured-deny
  for protected paths + dangerous Bash + external-write MCP names; `post_tool_audit.py`
  hashes touched files and appends events.
- **MCP:** FastMCP stdio server, **read/validate/dry-run tools only**, wrapping the same
  package modules the CLI uses. `PUBLISHING_EXTERNAL_WRITES: disabled`-equivalent env.
- **Config merge:** `<name>.default.json` (committed) deep-merged with gitignored
  `.local.json`.
- **Agent/skill formats:** agent frontmatter (`name, description(>-), effort, model,
  permissionMode, maxTurns, tools, skills, memory, background, color`) + body
  (Mission/Invoke-when/Inputs/Outputs/Procedure/Rubric/Prohibited/Escalate/Completion
  contract). Skill frontmatter (`allowed-tools`, `argument-hint`, `user-invocable`,
  `disable-model-invocation`) + body (Goal/Args/Entry/Delegation/Procedure/Outputs/Stop/
  Completion). Workflows are `.js` with `agent()`/`pipeline()`, gates kept outside workflows.

---

## E. Recommended phasing

The plan is far too big for one pass. Six phases; each ends with something runnable and
`framework validate`-clean. Phases 1–5 stop at `READY_FOR_REVIEW`; phase 6 is the
separately-authorized distribution extension.

**Phase 0 — Skeleton & determinism spine (no ML at all).**
`.claude/` layout, package + `th_cli.py`, `paths/util/errors/events/validation`,
`workflow_states.json` (with per-language track design from B2), core schemas
(project, state, artifact, event, approval, review, finding), `project init/status/
transition/validate/next/plan`, artifact register/freeze, approvals, `doctor` (probes all
engines as optional), `framework validate`, hooks, config default+local, `.mcp.json` +
read-only server, gitignore for binaries, `tests/`. **Deliverable:** a project can be
created and walked through empty stages with gates enforced — zero media processing yet.

**Phase 1 — Ingest & catalog.** `download-videos` skill + sanctioned net module
(`yt-dlp` behind `VIDTRANS_FETCH_ENABLED`), ffmpeg WAV extraction, `catalog/videos.json`
merge + diff events, `detect-language` (cheap LID, human-overridable). `rights-check`
skill + rights schema/gate. **Deliverable:** URL list → downloaded + cataloged + language
prefilled + rights record.

**Phase 2 — Transcription + transcript QA gate.** ASR adapter (mlx-whisper default,
segment-level timing baseline, word-level if aligner present), `create-closed-captions`
transcription half, `transcript/qa-report.json`, `qa-transcript` (second-pass diff),
`TRANSCRIPT_QA_GATE`. **Deliverable:** source-language transcript + QA report + gate.

**Phase 3 — Translation + captions (per language).** Glossary schema + injection + hard
checker (B6), Claude translation agents (Sonnet/Opus), back-translation QA, `captions.<lang>.json`
canonical artifact, deterministic SRT/VTT renderer, caption validators incl. CJK/RTL branch
(B7), `TRANSLATION_QA_GATE` + `CAPTION_VALIDATION`. **Deliverable:** validated multi-language
captions.

**Phase 4 — Dubbing + sync.** TTS adapters (Kokoro/Piper/XTTS/vendor per language),
per-cue fit + time-stretch cap, `sync-report.json` with cumulative drift, loudness
normalize, `AUDIO_QA_GATE` reading the sync report (B4), voice-clone consent gate (B5).
**Deliverable:** per-language dub track within drift tolerance.

**Phase 5 — Mux + package + final gate.** `merge-audio-video-channels` (soft-subs +
burned-in modes, CJK/RTL font handling), ffprobe verification, provenance-chained final
artifact, `FINAL_QA_GATE`, `PACKAGE`, `READY_FOR_REVIEW`. **Deliverable:** upload-ready
bundles per language. **Core framework complete here.**

**Phase 6 (separate authorization) — Distribution extension (§12).** Extended lifecycle,
`upload-youtube` (OAuth per channel, quota accounting, idempotent by hash),
`promote-content` (Telegram/Discord/RSS/X/Meta adapters + Reddit-as-manual-guide),
human-only release/promotion gates. Built only after Phases 0–5 are solid and you
explicitly authorize live external writes.

---

## F. Open decisions I need from you before Phase 0

(These are the plan's own §10 open items, now sharpened by what this machine can do.)

1. **Commercial vs personal use?** → gates whether XTTS (non-commercial) is allowed.
2. **Local-first vs vendor-first default?** → given no CUDA here, vendor APIs (Groq
   Whisper free tier, ElevenLabs/Azure/Google TTS) may be the pragmatic default with local
   as opt-in. This drives cost-guard design and `doctor` requirements.
3. **Package/CLI name:** confirm `th_cli.py` + package name.
4. **Target language set** to bake into `company.default.json` defaults (en required for
   audio; which caption languages by default?).
5. **Scope of this build:** Phases 0–5 only (recommended), or include Phase 6 groundwork?
