---
name: qa-translation
description: >-
  Second-pass quality review of a language's translated captions before the per-language
  translation_qa human gate. Runs the deterministic translation-qa + glossary reports, then
  adds a focused editorial review of flagged and glossary-relevant cues, preparing an
  evidence-backed gate packet. Use when a project (or a track) is at TRANSLATION_QA_GATE.
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
terms. The `translation_qa` gate is **per-language** — one approval per active track.

## Preconditions
- The project is at `TRANSLATION_QA_GATE` (or the track's `stage` is `TRANSLATION_QA_GATE`).
- Canonical captions exist at `captions/captions.<iso>.json`.

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
`translation_qa` is human-bound and per-language: a grant records the **human's** decision, executed
by you only after an explicit confirmation. Run it as a conversation, **per language**:
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
   The `CAPTION_TIMING` transition opens only once **every** non-source language is approved — run
   `vid project transition <id> --to CAPTION_TIMING --actor human` when the last one lands. On REVISE,
   run the human-directed transition back to `TRANSLATION`. Never grant/transition without the explicit
   confirmation (rule 2); never approve to unblock your own work.

## Completion contract
The gate reports reflect the current captions; every glossary blocker and editorial-flagged cue has a
second-pass note; a clear per-language APPROVE/REVISE recommendation with evidence is presented; any
grant/transition performed by the agent was the execution of an explicit, disclosed human confirmation.
