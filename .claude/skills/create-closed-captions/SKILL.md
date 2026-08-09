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

## Model policy — priority languages on Opus (inline), the rest on Sonnet (fan-out) (2026-08-10, company-config-driven)
The **most-important editions (`en`, `ar`, `ur`) render on
`@bedrock-eus2/us.anthropic.claude-opus-4-8` at `high` effort**; **every other target renders on
`@bedrock-eus1/us.anthropic.claude-sonnet-5` at `high`**. Fixed per-language split — no verify
pass, no escalation, no tracking; a language is either in the priority list or it isn't. The list
+ both models come from company config `translation_models` (`priority_languages`,
`priority_model`, `default_model`, `effort`; tune in `company.local.json`). `en` still stops at
its `translation_qa` human gate regardless of model. An explicit operator model override still
wins. The transcription stage above (ASR-adapter invocation, QA triage) stays on the skill
default.

**The split is enforced by PLACEMENT, because the Workflow `agent()` `model:` opt is silently
ignored** (a fan-out agent runs on the session-default model, not a passed override — root-cause
pending). So:
- **Priority langs (`en`/`ar`/`ur`) are filled INLINE in the main thread** — never as fan-out
  agents — so they land on the *session* model, which must be Opus. `translate export` runs a
  **fail-loud check**: if `ANTHROPIC_MODEL` isn't the configured `priority_model`, the export
  result carries a loud `priority_model_warning`. **Do not fill a priority worksheet while that
  warning is present — switch to Opus (`/model opus`) first**, then fill (rule 11 bounded batches).
  Multiple priority tracks → do them sequentially inline on the Opus session.
- **Non-priority langs fan out on Sonnet** (the ≥2-track Workflow below).

## Priority langs INLINE (Opus), non-priority langs fan out (Sonnet) — the ≥2-track rule
The dense per-cue fill is the token-heavy, embarrassingly-parallel part of this stage, and
every language is independent. But because the `agent()` `model:` opt is ignored (Model policy
above), the split is by **placement**:
- **`en`, `ar`, `ur` (priority) are filled INLINE** in the main thread, one at a time, on the
  Opus session (fail-loud checked at `translate export`) — **never fan-out agents**. Use the
  rule 11 bounded-batch idiom for each. `en` **STOPS at the per-language `translation_qa` human
  gate** after import — you produce the draft; you never cross the gate. `ar`/`ur` are
  `auto_translate` (no human gate) but still Opus-inline for quality.
- **Every non-priority `auto_translate` track** (`fr`, `es`, `zh`, `pt`, `ru`, …) fans out on
  Sonnet. **Whenever ≥2 of them need filling, you MUST fan them out with a `Workflow` — one agent
  per language, concurrently** — not inline one after another. Standing behavior, not an opt-in.
  (Exactly one non-priority track → fill inline; a one-agent fan-out is pure overhead.) No human
  gate.
- The **source track** is `skip_translation` (verbatim, no worksheet) — neither path.

How the non-priority fan-out works (priority `en`/`ar`/`ur` are filled inline instead — see the
placement rule above; the shared pivot below still serves them). See rule 16 in `.claude/CLAUDE.md`
for the canonical pattern:
1. Export every worksheet first (`translate export <id> --language <iso>` per track) so cue
   ids/timing exist. Build one compact **pivot** the agents share — for a tafsir/Quran video,
   the pivot is `{cue: {source, en_gloss, en_meaning}}` so every agent renders from the same
   meaning + the same canonical verse set. **A cited Quranic verse is rendered as the target
   language's translation + a numeric citation `(Quran <surah>:<ayah>)`; original Arabic script
   and transliteration NEVER go into a non-`ar` caption (json `target_text`, `.srt`, or `.vtt`)
   — the only exception is target `ar`, whose narration is Arabic. This keeps the non-Arabic
   dubs clean (rule 14 speaks `target_text` verbatim). The separate `distribution/notes/<lang>.txt`
   upload-description docs still keep Arabic verse citations — do not confuse the two.** Write the
   pivot to a scratch path (e.g. `/tmp/translate_pivot.json`).
   **Keep-source-audio cues (recited windows):** a cue that carries `flags:["keep-source-audio"]`
   (e.g. the opening Quranic recitation) will play the **original reciter's audio** in every
   language at dub time (rule 14) — the flag drives *audio*, not the caption. The agents must
   **carry that flag through unchanged** (it rides the worksheet's `flags` array) and still fill
   `target_text` normally: non-`ar` = the verse translation + `(Quran s:a)`, `ar`/source = Arabic
   verbatim. Never drop the flag and never put Arabic script into a non-`ar` `target_text`.
2. Launch a `Workflow` with `parallel(langs.map(...))` over the **non-priority** langs only, one
   `agent()` per language, each with a `schema` that forces a validated
   `{cues:[{id,target_text}]}` return. The agent reads the pivot, renders **all** cues into its
   language, and returns the map. Keep timing fixed: never change
   `id`/`start_ms`/`end_ms`/`source_text`, never merge/split cues. These run on the Sonnet session
   (the `model:` opt is ignored — don't rely on it). Priority `en`/`ar`/`ur` are NOT in this
   fan-out — fill them inline on the Opus session (placement rule above).
3. Back in the main thread, write each returned map into `captions/<iso>.worksheet.json` by cue
   id (a throwaway merge-by-id helper — scaffolding, delete after), then `translate import`
   each track through the normal CLI chokepoint. The worksheet is a fill-in artifact, not
   CLI-owned state (rule 1); the fan-out only *fills* it, the CLI still *imports* it.
This keeps each agent's turn small (one language, avoids the single-response timeout that the
old inline bounded-batch idiom worked around) and runs N languages in parallel instead of
serially. The bounded-batch idiom (rule 11) remains the fallback for a single inline track.

## Procedure (fan out once for all tracks that need filling; steps 3-6 per language)
1. Export the worksheet(s): `vid_cli.py translate export <id> --language <iso>` for each
   target that needs filling. This writes `captions/<iso>.worksheet.json` — source cues with
   fixed timing, empty `target_text` slots, per-cue editorial flags, and (if
   `project.yaml.glossary_id` is set) the glossary's required renderings in
   `glossary_instructions`.
2. Fill: **priority `en`/`ar`/`ur` inline on Opus (fail-loud checked), non-priority via the
   Sonnet fan-out per the placement rule above** (≥2 non-priority tracks → one `Workflow` agent
   per language; a single track → fill inline in bounded batches). **Do not** change `id`, `start_ms`,
   `end_ms`, or `source_text` — timing is the contract, and merging/splitting cues corrupts
   downstream sync. Honour the glossary: `[MUST]` terms must appear verbatim (or an accepted
   alias). Optionally add a `back_translation` per cue to aid QA. Merge each agent's returned
   `{id,target_text}` map into its worksheet by cue id, then delete the merge scaffolding.
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
