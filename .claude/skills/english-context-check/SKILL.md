---
name: english-context-check
description: >-
  At TRANSCRIPT_QA_GATE, produce a faithful ENGLISH review-gloss of the source-language
  transcript, then run an AI context/word-sense pass over it — verifying the words used make
  sense within the speech's context, fixing the English gloss where it can, and flagging
  source-transcript problems for the human. Its findings fold into the transcript-qa gate
  report so the human can validate the source transcript, the English gloss, or both. Use when
  a project is at TRANSCRIPT_QA_GATE, before the qa-transcript second pass and the human gate.
allowed-tools: Bash, Read, Write, Edit
argument-hint: <video-id>
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus2/us.anthropic.claude-opus-4-8"
effort: high
---

# english-context-check

## Goal
Give the human gate reviewer a faithful English gloss of the source speech AND an AI
context/word-sense verification of it, so meaning can be validated at the transcript gate —
long before the pipeline's later per-language translation. Fix what can be fixed in the English
gloss automatically; flag the rest for the human. This runs on **Opus-4.8 / high** because
cross-lingual meaning verification of (often religious/political) speech is high-stakes
editorial (CLAUDE.md model policy).

## Scope — what this is and is NOT
- **IS** a review-time English gloss of the *source transcript* (a review aid), registered as an
  `english-gloss` artifact under `transcript/`. Advisory only.
- **IS NOT** the later per-language English caption translation (the TRANSLATION stage still
  produces that independently and is unchanged).
- The AI **never edits the source transcript** — that's a human/CLI action that would change an
  approved artifact and re-trigger ASR QA. Source problems become findings, not silent edits.

## Preconditions
- Project state is `TRANSCRIPT_QA_GATE` (`vid_cli.py project status <id>`).
- A source transcript exists at `transcript/source.<lang>.json`.

## Procedure
1. **Export the gloss worksheet** (idempotent — won't clobber a partially-filled one):
   `vid_cli.py transcript english-export <id>`.
2. **Read the material**: `Read transcript/source.<lang>.json`, `Read source/metadata.json`
   (title, dialect, any context), and the worksheet `transcript/english-gloss.worksheet.json`.
3. **Gloss every cue**: fill `target_text` with plain, meaning-first English. Do NOT touch
   `id`/`start_ms`/`end_ms`/`source_text`; do not merge or split cues (timing is fixed).
4. **Context / word-sense pass (the core of this skill):**
   - First read the whole gloss to establish the speech's *context* — topic, register, who is
     speaking, the domain (e.g. a Quran tafsir lecture).
   - Then for each cue, verify the English words you chose make sense **in that context**.
     - If an English word choice is wrong or ambiguous → **fix `target_text` in place** and add a
       short `context_note` explaining the decision.
     - If the problem is in the **source transcript** (a word that can't make sense in context —
       a likely ASR mistranscription, a repetition/placeholder like an `[نامفهوم …]` marker) →
       do NOT edit the source. Set the cue's `flags` to include `"context-mismatch"` and write a
       `context_note` describing what looks wrong and your best reading. This becomes a
       human-facing finding.
   - Give cues flagged `editorial-religious` / `editorial-political` extra care (verify terms,
     transliterations, and that the rendered sense is faithful, not editorialized).
5. **Import the gloss**: `vid_cli.py transcript english-import <id>` (validates the worksheet,
   rejects any empty `target_text`, writes `transcript/english-gloss.json`).
6. **Regenerate the QA report**: `vid_cli.py transcript qa <id>`. The gloss's context findings
   are now folded into `reviews/transcript-qa-gate-latest.json` (a `context-mismatch` →
   CONDITIONAL_PASS, an empty gloss → FAIL); `metrics.gloss_present` is `true`.
7. **Prepare the gate packet** (in your response, not a committed approval): present the source
   transcript AND the English gloss together, cue-referenced; summarize the fixes you made to the
   gloss, every `context-mismatch` you flagged in the source, and the sensitive-term notes; end
   with an APPROVE / REVISE recommendation. Make explicit that the human may validate the source
   transcript, the English gloss, or both.

## Outputs
- `transcript/english-gloss.worksheet.json` (transient) and `transcript/english-gloss.json`
  (canonical gloss, registered as an `english-gloss` artifact).
- Refreshed `reviews/transcript-qa-gate-latest.json` with the folded-in context findings.
- A written, cue-referenced gate packet (source + English gloss + recommendation) in your reply.

## Stop / Escalate — HUMAN GATE
`transcript_qa` is human-bound. This skill NEVER grants the approval or transitions the gate.
Present the packet and stop. On REVISE, the human directs a transition back to `TRANSCRIPTION`
(and may correct the source transcript); on APPROVE, the human runs the approval skill and then
authorizes `TRANSCRIPT_QA_GATE -> SEGMENT_RESOLUTION`.

## Completion contract
An English gloss of every source cue exists and validates; the context pass has fixed the gloss
where possible and flagged source problems with `context-mismatch` + `context_note`; the
transcript-qa report reflects those findings; a clear APPROVE/REVISE recommendation presenting
both the source and the English gloss is given for the human. No approval or transition performed
by the agent, and the source transcript is unmodified.
