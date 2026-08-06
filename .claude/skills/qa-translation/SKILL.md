---
name: qa-translation
description: >-
  Second-pass quality review of the human-reviewed track's translated captions (en) before its
  translation_qa human gate. Runs the deterministic translation-qa + glossary reports, then
  adds a focused editorial review of flagged and glossary-relevant cues, preparing an
  evidence-backed gate packet. auto_translate targets skip this human gate. Use when a project
  (or the en track) is at TRANSLATION_QA_GATE.
allowed-tools: Bash, Read
argument-hint: <video-id> [--language <iso>]
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus1/us.anthropic.claude-sonnet-5"
effort: xhigh
---

# qa-translation

## Goal
Give the human gate reviewer everything they need to approve or reject a language's
translation in one pass: the deterministic findings (glossary hard-check + empty/untranslated
signals), plus a careful editorial read of the cues that carry sensitive content or glossary
terms.

## Scope — this gate is only for the human-reviewed track(s)
Under the two-axis model (rule 7 extension) the `translation_qa` **human** gate applies **only
to `en`** (and the source language, which is `skip_translation` and needs no translation gate
at all). Every other target is `auto_translate: true`: it still gets deterministic
`translation-qa` + `glossary` QA (run by `translate qa`), but **no human approval** — do not run
this skill for those tracks, and do not seek an approval for them (`state.transition_blockers`
already excludes them). This skill's editorial pass + gate packet is for the `en` track.

## Preconditions
- The project is at `TRANSLATION_QA_GATE` (or the track's `stage` is `TRANSLATION_QA_GATE`).
- Canonical captions exist at `captions/captions.<iso>.json`.
- The language is human-reviewed (`en`) — neither `skip_translation` nor `auto_translate`.

## Procedure
1. Regenerate the reports (idempotent, aggregate across active tracks):
   `vid_cli.py translate qa <id>`. Read the `translation_qa` and `glossary` decisions and
   every finding.
2. `Read captions/captions.<iso>.json` and `Read transcript/source.<lang>.json`.
3. For each finding:
   - **glossary-missing-required** (blocker): confirm the required term truly applies to the
     cue, then direct a fix (re-translate the cue using the canonical rendering) — this
     FAILs the gate until resolved.
   - **glossary-preferred-rendering** (warn): note whether the alternative rendering is
     acceptable in context; not blocking.
   - **untranslated-suspect** / **empty-translation**: verify and direct a fix.
4. Editorial pass on cues flagged `editorial-religious` / `editorial-political` in the
   worksheet: verify the translation neither sanitizes nor sharpens the source meaning.
   Quote the source and the rendering as evidence.
5. Spot-check faithfulness on a sample of ordinary cues (meaning preserved, no cues merged
   or split, timing untouched).
6. Summarize a per-language recommendation (APPROVE / REVISE) with cue-referenced evidence.

## Outputs
- Refreshed `reviews/translation-qa-gate-latest.json` and `reviews/glossary-gate-latest.json`
  (decisions read by the state machine).
- A written gate-packet summary per language for the human reviewer (in your response, not a
  committed approval).

## Stop / Escalate — HUMAN GATE (decide-not-operate, CLAUDE.md rule 13)
`translation_qa` is human-bound but scoped to the human-reviewed track(s) (`en`): a grant records
the **human's** decision, executed by you only after an explicit confirmation. `auto_translate`
tracks are not part of this gate. Run it as a conversation, for the human-reviewed language:
1. **Surface + present options** (`AskUserQuestion`) — REVISE with the specific fixes (glossary miss,
   editorial cue) / edit-then-re-QA — **plus an explicit `Approve this language` option**, each with
   its consequence.
2. **On *approve*, disclose then confirm** (the recorded decision): echo the **gate**
   (`translation_qa`) + **language**, the **active** `captions-json` **SHA-256** on disk (verify
   against the file), the **relative path** (`captions/<iso>.json` or equivalent), and any **concerns**
   worth a look, each briefly explained. Get one explicit confirmation; log it into `--notes` /
   `reviews/`.
3. **Then execute it yourself** (no `!` needed), flags verified with `--help`:
   ```bash
   vid approval grant <id> --gate translation_qa --lang <iso> --approver "<human>" --scope captions \
       --artifact sha256:<active> --notes "<decision + disclosed concerns>"
   ```
   The `CAPTION_TIMING` transition opens once the human-reviewed track (`en`) is approved **and**
   every `auto_translate` track has passed deterministic QA — run
   `vid project transition <id> --to CAPTION_TIMING --actor human` then. On REVISE,
   run the human-directed transition back to `TRANSLATION`. Never grant/transition without the explicit
   confirmation (rule 2); never approve to unblock your own work.

## Completion contract
The gate reports reflect the current captions; every glossary blocker and editorial-flagged cue has a
second-pass note; a clear per-language APPROVE/REVISE recommendation with evidence is presented; any
grant/transition performed by the agent was the execution of an explicit, disclosed human confirmation.
