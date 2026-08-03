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

## Stop / Escalate — HUMAN GATE
This skill NEVER grants an approval or transitions the gate. `translation_qa` is human-bound
and per-language. Present findings + recommendation and stop. On REVISE, the human directs a
transition back to `TRANSLATION`; on APPROVE, the human runs the approval skill (bound to that
language's `captions-json` hash) and then authorizes `TRANSLATION_QA_GATE -> CAPTION_TIMING`.

## Completion contract
The gate reports reflect the current captions; every glossary blocker and editorial-flagged
cue has a second-pass note; a clear per-language APPROVE/REVISE recommendation with evidence
is presented. No approval or transition performed by the agent.
