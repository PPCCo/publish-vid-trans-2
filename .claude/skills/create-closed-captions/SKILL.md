---
name: create-closed-captions
description: >-
  Produce closed captions for a project: the source-language transcript (TRANSCRIPTION),
  then per-target-language translation and byte-stable SRT/VTT rendering (TRANSLATION ->
  CAPTION_VALIDATION). Runs the ASR adapter or imports a transcript, then drives the
  translation worksheet, glossary hard-check, and caption validators — stopping at each
  human gate. Use when a project is at TRANSCRIPTION or TRANSLATION.
allowed-tools: Bash, Read, Write, Edit
argument-hint: <video-id> [--language <iso>] [--provider <asr>] [--model <name>]
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus1/us.anthropic.claude-sonnet-5"
effort: high
---

# create-closed-captions (transcription stage)

## Goal
Turn `source/audio.wav` into a canonical, cue-timed transcript at
`transcript/source.<lang>.json`, registered as a content-addressed artifact, then run
the deterministic QA pass so a human can review the transcript_qa gate.

The **cue-level timing is the contract** every downstream stage depends on. Word-level
timing is a bonus the engine may or may not produce — never require it.

## Preconditions
- Project state is `TRANSCRIPTION` (`vid_cli.py project status <id>`).
- `state.source_language` is set (run `/detect-language` first if not).
- `vid_cli.py doctor` shows at least one `engine:*whisper*` as installed — OR you have a
  pre-transcribed JSON to import.

## Procedure
1. Confirm readiness: `vid_cli.py project status <id>` (state TRANSCRIPTION, source_language set).
2. Check for an ASR engine: `vid_cli.py doctor` → look for `engine:mlx_whisper` /
   `engine:faster_whisper` = installed.
3. Transcribe:
   - Engine present: `vid_cli.py transcript run <id> [--provider mlx-whisper] [--model large-v3]`.
     This shells out to the engine in a subprocess (the CLI never imports torch/mlx itself).
   - No engine installed: transcribe out-of-band, then
     `vid_cli.py transcript import <id> --from <whisper.json>` (accepts Whisper-shaped JSON
     with a `segments` array). Do NOT hand-edit `transcript/source.<lang>.json`.
4. Run QA: `vid_cli.py transcript qa <id>`. Read the reported findings and metrics.
5. If QA `decision` is `FAIL` (timing corruption / empty transcript) or the low-confidence
   share is high, fix the root cause (re-run with a larger model, or correct the source
   audio) and re-transcribe before advancing.
6. Advance to the gate: `vid_cli.py project transition <id> --to TRANSCRIPT_QA_GATE`
   (or pass `--advance` to `transcript run`). Then STOP — see below.

## Sensitive content
The QA report flags religious/political terms as **notes**, never blockers. Do not
silently alter or omit them. Surface them for the human reviewer; translation of such
references is an editorial decision reserved for a human at the gate.

## Outputs
- `transcript/source.<lang>.json` (schema-valid, registered artifact).
- `transcript/qa-report.json` + `reviews/transcript-qa-gate-latest.json` (gate report).
- Events: `TRANSCRIPTION_COMPLETED`, `TRANSCRIPT_QA_REPORTED`.

## Stop / Escalate — HUMAN GATE
`TRANSCRIPT_QA_GATE -> TRANSLATION` is human-bound. You may prepare the gate packet and
recommend a decision, but you may NOT grant the approval. The edge stays closed until a
human runs the approval skill. If QA is CONDITIONAL_PASS, summarize exactly which cues
need attention so the reviewer can decide fast.

## Completion contract
A schema-valid transcript exists and is registered; a `transcript-qa` gate report is
written with an explicit decision; the project rests at `TRANSCRIPT_QA_GATE` awaiting a
human approval. No transition through the gate has been attempted by the agent.

---

# create-closed-captions (translation + caption stage)

Once a human has approved the transcript gate and the project is at `TRANSLATION`, the
lifecycle becomes **per-target-language** (each `state.language_tracks` entry advances on
its own). Translation is *your* work as the agent — the CLI never calls an LLM. You fill a
worksheet the CLI emits, then hand it back for validation and content-addressing.

## Two-axis translate/dub scope — which tracks need a HUMAN gate
Translation **and** dubbing happen for every selected language, but the **human review
machinery is scoped** (rule 7 extension):

- **Source language** — `skip_translation: true`. No worksheet, no translation gate; the CLI
  emits verbatim source captions (`translate export` short-circuits). Human QA on this track
  is caption/editorial only.
- **English (`en`)** — human-reviewed. Fill its worksheet and STOP at the per-language
  `translation_qa` gate for a human approval (unless `en` is itself the source → skip).
- **Every other target** (`fr`, `es`, `zh`, `pt`, `ru`, …) — marked `auto_translate: true`
  at track creation. **You (the AI) fill its worksheet and `translate import` it with NO human
  gate.** Deterministic QA (`translation-qa`, `glossary`) still runs and must pass; only the
  *human approval* requirement is lifted. `state.transition_blockers` already excludes these
  tracks from the `translation_qa` gate — do not seek an approval for them.

Check each track's markers with `vid_cli.py project status <id>` (or read
`state.language_tracks.<iso>`): `skip_translation` → verbatim; `auto_translate` → AI-fill no
gate; neither (i.e. `en`) → human gate.

## Model policy
Translate volume cues with `@bedrock-eus1/us.anthropic.claude-sonnet-5` at `high` effort
(this is the skill's default — a bounded per-cue rendering task, not a capability problem).
For cues the worksheet marks with an `editorial-religious` / `editorial-political` flag,
escalate to `@bedrock-eus2/us.anthropic.claude-opus-4-8` at `high` effort — these references
are high-stakes and must be rendered with care, not paraphrased away. The transcription stage
above (ASR-adapter invocation, QA triage) stays on the skill default; only the flagged
translation cues need the capability upgrade.

## Procedure (run once per target language in `state.target_languages`)
1. Export the worksheet: `vid_cli.py translate export <id> --language <iso>`. This writes
   `captions/<iso>.worksheet.json` — source cues with fixed timing, empty `target_text`
   slots, per-cue editorial flags, and (if `project.yaml.glossary_id` is set) the glossary's
   required renderings in `glossary_instructions`.
2. Translate: fill every cue's `target_text` with `Edit`/`Write`. **Do not** change `id`,
   `start_ms`, `end_ms`, or `source_text` — timing is the contract, and merging/splitting
   cues corrupts downstream sync. Honour the glossary: `[MUST]` terms must appear verbatim
   (or an accepted alias). Optionally add a `back_translation` per cue to aid QA.
   For a dense source, fill in **bounded batches** (rule 11 gloss idiom) to avoid a
   single-response timeout: write small `_batchN.json` maps of `{ "<cue-id>": {"target_text"}}`
   and merge them into the worksheet by cue id with a throwaway helper, then delete the
   scaffolding before import.
3. Import: `vid_cli.py translate import <id> --language <iso>` — validates the worksheet,
   builds canonical `captions/captions.<iso>.json`, registers it, and advances that language
   track to `TRANSLATION_QA_GATE`. Add `--advance` to move the top-level state once *every*
   active track has been translated.
4. QA: `vid_cli.py translate qa <id>` — writes the aggregate `translation-qa` and `glossary`
   gate reports across all active tracks (human AND auto). If `glossary` is `FAIL`, a `[MUST]`
   term is missing from some cue's translation; fix the cue, re-import, and re-run QA.
   - **`auto_translate` tracks:** no human gate — once deterministic QA passes they are done;
     they advance with the top-level `--advance` quorum. Do NOT run `/qa-translation` or seek
     an approval for them.
   - **`en` (human) track:** STOP at the per-language human gate (see below).
5. After the human approves and opens `TRANSLATION_QA_GATE -> CAPTION_TIMING`: render
   captions with `vid_cli.py captions build <id> --language <iso> --format srt,vtt`
   (deterministic; re-rendering the same JSON yields identical bytes).
6. Validate: `vid_cli.py captions validate <id>` — aggregate `caption` gate report
   (reading speed, cue duration, line length/count; CJK measured in characters, RTL/Latin in
   words). Fix flagged cues by editing `captions.<iso>.json` line wrapping or re-translating,
   then re-build and re-validate.

## Stop / Escalate — HUMAN GATE (source + en only)
`TRANSLATION_QA_GATE -> CAPTION_TIMING` is human-bound but **scoped to the human-reviewed
track(s)** — in practice just `en` (the source track is `skip_translation`; every other target
is `auto_translate` and carries no human gate). The `en` track needs a human approval bound to
its `captions-json` hash. You may prepare the packet and recommend, but never grant the
approval or transition the gate. Use `/qa-translation` to build the reviewer packet for the
human-reviewed track. Once `en` is approved and every `auto_translate` track has passed
deterministic QA, the top-level gate opens.

## Outputs
- `captions/<iso>.worksheet.json` (transient), `captions/captions.<iso>.json` (canonical,
  registered), `captions/captions.<iso>.srt` + `.vtt` (registered, byte-stable).
- `reviews/translation-qa-gate-latest.json`, `reviews/glossary-gate-latest.json`,
  `reviews/caption-gate-latest.json`.
- Events: `TRANSLATION_WORKSHEET_EXPORTED`, `TRANSLATION_IMPORTED`,
  `TRANSLATION_QA_REPORTED`, `CAPTIONS_RENDERED`, `CAPTION_VALIDATED`.

## Completion contract (caption stage)
Every active language track has a schema-valid, registered `captions.<iso>.json` with
byte-stable SRT/VTT; the `translation-qa`, `glossary`, and `caption` gate reports carry
explicit decisions; each track rests at its human gate awaiting a per-language approval. No
gate transition has been attempted by the agent.
