---
name: qa-transcript
description: >-
  Second-pass quality review of a source-language transcript before the transcript_qa
  human gate. Runs the deterministic QA report, then adds a careful human/agent listen-back
  on the flagged cues, preparing an evidence-backed gate packet. Use when a project is at
  TRANSCRIPT_QA_GATE.
allowed-tools: Bash, Read
argument-hint: <video-id> [--language <iso>]
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus1/us.anthropic.claude-sonnet-5"
effort: high
---

# qa-transcript

## Goal
Give the human gate reviewer everything they need to approve or reject the transcript in
one pass: the deterministic QA findings, plus a focused second-pass review of the cues the
machine flagged (low confidence, silence/music, timing anomalies, sensitive terms).

## Preconditions
- Project state is `TRANSCRIPT_QA_GATE`.
- A transcript exists at `transcript/source.<lang>.json`.

## Procedure
0. **English review-gloss first.** Run the `english-context-check` skill for `<id>` (or confirm
   `transcript/english-gloss.json` already exists). It produces a faithful English gloss of the
   source speech and an AI context/word-sense pass whose findings fold into the transcript-qa
   report. This lets you (and the human) validate meaning, not just the source script.
1. Regenerate the deterministic report (idempotent): `vid_cli.py transcript qa <id>`.
   Read `decision`, `metrics` (including `gloss_present`), and every `finding` — including the
   `english-context/*` findings folded in from the gloss.
2. `Read transcript/source.<lang>.json`, `Read transcript/english-gloss.json` (the English
   gloss), and `Read source/metadata.json` (duration, dialect).
3. For each flagged cue, do a second-pass check:
   - **timing** findings (blocker/minor): confirm the cue's start/end against the audio;
     these gate a FAIL if the timeline is corrupt.
   - **confidence** findings: judge whether the text is plausibly correct for the
     dialect; note likely mistranscriptions with the corrected text as evidence.
   - **non-speech / silence**: confirm whether real speech was dropped vs. music/applause.
   - **sensitive-religious / sensitive-political**: verify the transliteration/term is
     rendered accurately; flag anything whose *translation* will need editorial care.
   - **english-context/* findings** (from the gloss): a `context-mismatch` means the AI thinks
     a source word can't make sense in context (likely an ASR error) — check it against the audio;
     `context-note`/`length-outlier`/`untranslated-suspect` are lower-severity meaning signals.
4. Summarize a recommendation (APPROVE / REVISE) with a short, cue-referenced rationale,
   presenting the source transcript AND the English gloss (the human may validate either or both).
   If REVISE, list the specific fixes (re-transcribe with a larger model, correct N cues,
   re-slice a silence gap).

## Outputs
- `transcript/english-gloss.json` (from step 0) — the English review-gloss of the source.
- Refreshed `reviews/transcript-qa-gate-latest.json` (decision read by the state machine),
  including the folded-in `english-context/*` findings.
- A written gate-packet summary for the human reviewer (source + English gloss, in your
  response, not a file the agent commits as an approval).

## Stop / Escalate — HUMAN GATE (decide-not-operate, CLAUDE.md rule 13)
`transcript_qa` is human-bound: a grant records the **human's** decision, executed by you only after
an explicit confirmation. Run the gate as a conversation, not a command:
1. **Surface + present options** (`AskUserQuestion`) — for each blocking/major finding give the real
   choices with consequences, e.g. *accept-as-is (CONDITIONAL_PASS)* / *you edit
   `transcript/source.<lang>.json` in VS Code, then I re-run gloss + QA* / *you dictate the fix and I
   write it via the CLI, then re-QA* — **plus an explicit `Approve & advance` option**. Offer a
   clickable file link for the edit path.
2. **On *approve*, disclose then confirm** (the recorded decision): echo the **gate** (`transcript_qa`),
   the **active** transcript **SHA-256** on disk (verify against the file, not just manifest order),
   the **relative path** (`transcript/source.<lang>.json`), and any **concerns** worth a look, each
   briefly explained. Get one explicit human confirmation and log it into `--notes` / a `reviews/` note.
3. **Then execute it yourself** (no `!` needed) — verify flags with `--help` first:
   ```bash
   vid approval grant <id> --gate transcript_qa --approver "<human>" --scope transcript \
       --artifact sha256:<active> --notes "<decision + disclosed concerns>"
   vid project transition <id> --to SEGMENT_RESOLUTION --actor human
   ```
   On REVISE, run the human-directed transition back to `TRANSCRIPTION` instead. Never grant/transition
   without the explicit confirmation (rule 2); never approve to unblock your own work. If the human
   prefers, offer an `!` block, but the default is you execute on confirmation.

## Completion contract
The gate report reflects the current transcript, every machine-flagged cue has a second-pass note, a
clear APPROVE/REVISE recommendation with evidence is presented, and any grant/transition performed by
the agent was the execution of an explicit, disclosed human confirmation.
