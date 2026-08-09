---
name: process-manual
description: >-
  Token-thrifty MANUAL/SCRIPTED route for the transcribe→translate→dub pipeline. Two modes:
  (1) hand-off — surface the one `run_pipeline.sh <id> --mt` command that runs every
  deterministic step OUTSIDE Claude and stops at the next human gate, then step back; (2)
  resume/QA — when the operator says "the manual process for <id> is done", run the editorial
  QA at each pending gate and walk the operator through approval (rule 13). Use to run a
  project cheaply via scripts, or to QA + clear gates after a scripted run.
allowed-tools: Bash, Read, AskUserQuestion
argument-hint: <video-id> [done]
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus2/us.anthropic.claude-opus-4-8"
effort: high
---

# process-manual

## Goal
Cut token spend by keeping Claude OUT of the mechanical pipeline steps. Almost the entire
transcribe→translate→dub chain is pure deterministic script (`ingest`, `transcript`,
`translate import`/`qa`, `captions build`/`validate`, `dub run`/`qa`, `package mux`/`final-qa`/
`build`) — and, with an MT engine, even the one non-deterministic step (translation worksheet
+ English gloss fill) needs no Claude. The operator runs one command; Claude only re-engages to
do **editorial QA at the human gates**.

This is the alternate route to the Claude-driven skills (`create-closed-captions`,
`produce-dub`, …), which stay intact. Pick this route to save tokens; pick the Claude route when
you want Claude authoring the translation itself.

## Two modes — decide which from the args / phrasing
- **Hand-off mode** — `/process-manual <id>` (no `done`): the operator wants to run the scripted
  pipeline. Surface the command and where it will stop. **Do NOT run the pipeline yourself** —
  running `ingest`/`transcript`/`dub` in-conversation is exactly the token spend we're avoiding.
- **Resume / QA mode** — the operator says **"the manual process for `<id>` is done"** (or
  `/process-manual <id> done`): the scripted run has halted at a gate. Now Claude does the QA
  and gate work.

---

## Hand-off mode

1. Read the current state (cheap): 
   ```
   "$CLAUDE_PROJECT_DIR/.venv/bin/python3" "$CLAUDE_PROJECT_DIR/.claude/scripts/vid_cli.py" project plan <id>
   "$CLAUDE_PROJECT_DIR/.venv/bin/python3" "$CLAUDE_PROJECT_DIR/.claude/scripts/vid_cli.py" project autopilot <id> --dry-run --mt
   ```
   The `--dry-run` preview lists the exact ordered steps the scripted run will execute and the
   gate it will stop at — show the operator this so they know what will happen.
2. Tell the operator, in plain language:
   - the deterministic steps that will run (from the dry-run `steps_run`),
   - the **human gate** it will stop at (from the dry-run `stop_reason`),
   - that it runs Claude-free and won't publish or cross any gate.
3. Give them the single command to run in their shell (an `!`-runnable line):
   ```
   ! bash "$CLAUDE_PROJECT_DIR/.claude/scripts/run_pipeline.sh" <id> --mt
   ```
   - Drop `--mt` only if they intend to author translations via the Claude route instead — then
     the run stops at `TRANSLATION` and they'd use `create-closed-captions`.
   - `run_pipeline.sh` sets `VIDTRANS_FETCH_ENABLED=1` (media downloads permitted, rule 3) and
     the proxy CA bundle, then drives `project autopilot`. It is idempotent — safe to re-run.
4. Tell them how to come back: when it stops, say **"the manual process for `<id>` is done"** in
   this session. Then STOP — do not run the pipeline. The whole point is to spend no tokens here.

If `project autopilot --dry-run` HALTs before a gate (e.g. *source language not confirmed*, or
*translation needs --mt*), surface that instead: it's a decision the operator must resolve first
(confirm the source language via `/new-video` or `langid set`; install an MT engine — see
OPERATING-GUIDE.md §6; or use the Claude route). Present the fix, don't work around it silently.

---

## Resume / QA mode

The scripted run has stopped. Do full editorial QA for **every pending gate**, then walk each
gate per rule 13. This is the only place this route spends tokens — and only on QA + gates,
never on authoring or step-by-step shelling.

1. **Locate the stop.** 
   ```
   "$CLAUDE_PROJECT_DIR/.venv/bin/python3" "$CLAUDE_PROJECT_DIR/.claude/scripts/vid_cli.py" project status <id>
   "$CLAUDE_PROJECT_DIR/.venv/bin/python3" "$CLAUDE_PROJECT_DIR/.claude/scripts/vid_cli.py" project plan <id>
   ```
   `plan.autonomy_action` is authoritative. If it is `BLOCKED`/`HALT`-like (e.g. rights not set,
   MT missing), surface the blocker and stop — the operator resolves it, then re-runs the script.
2. **Run the editorial QA for the gate(s) now pending.** Use the existing QA skills — do not
   reinvent their logic — matching the current state:
   - `TRANSCRIPT_QA_GATE` → **qa-transcript** (source transcript) + the rule-11 English gloss:
     run **english-context-check** on `transcript/english-gloss.json` (the scripted run's `--mt`
     drafted it; the AI context/word-sense pass is Claude's job — it auto-fixes the gloss and
     folds `english-context/*` findings into the transcript-qa report; it NEVER edits the source
     transcript).
   - `TRANSLATION_QA_GATE` → **qa-translation** for the **`en`** track (and the source track's
     verbatim captions, editorial-only). `auto_translate` targets have **no** human gate (rule
     7) — do not gate them; their deterministic `translation-qa` + `glossary` reports must still
     be PASS (verify, don't approve).
   - `CAPTION_VALIDATION` (deterministic edge, not a human gate) → **qa-caption-sync** to confirm
     the caption report is PASS before it advances; if CONDITIONAL_PASS on reading-speed, see the
     caption reading-speed cap note (raise the bar via `company.local.json`, never re-time
     approved captions).
   - `AUDIO_QA_GATE` → **qa-audio-sync** per dubbed language.
   - `FINAL_QA_GATE` → **qa-final**.
3. **Surface each human gate per rule 13 (decide-not-operate).** For each pending gate, in one
   `AskUserQuestion`, present the concrete fixes for any issue **and** an explicit
   **`Approve & advance`** option. When the operator picks approve:
   - Disclose and get **one explicit confirmation** of: the **gate**; the **active** artifact
     **SHA-256** (verify against the file on disk, not just manifest order); the **relative
     path(s)** under review; and each **concern** in plain language.
   - Then execute yourself: `approval grant --approver "<operator>" --artifact <hash> --gate
     <gate> --scope <scope> [--lang <iso>] --notes "<decision + concerns>"`, then the gated
     `project transition --actor human --to <target>`. Verify flags with `--help` first
     (`approval grant` → `--approver`; `project transition` → `--actor`).
   - The three **outward-facing legal gates** (`rights-check`, `video-release-authorize`,
     `video-promote-approve`) keep the tighter posture — you gather facts and disclose, but the
     operator runs the final `!` command for those three.
4. **After a gate clears, the rest is scripted again.** If more deterministic work remains before
   the next gate (e.g. after `audio_qa` → VIDEO_MUX → FINAL_QA_GATE), hand back to the script:
   tell the operator to re-run `run_pipeline.sh <id> --mt`, which resumes and stops at the next
   gate. Alternate script-run → Claude-QA until `READY_FOR_REVIEW`.

## Guardrails (unchanged)
- Never grant/transition without the explicit post-disclosure confirmation (rules 2/13); never
  approve to unblock your own work.
- The pipeline stops at `READY_FOR_REVIEW`; no publication by default (rule 3).
- Rights precede distribution — `PACKAGE → READY_FOR_REVIEW` stays blocked while rights are
  `unreviewed`/`do-not-distribute` (rule 4); that's an operator decision, not an approval.
- `project autopilot` can never cross a gate — it only runs the PROCEED-eligible non-gate steps.

## Stop / Escalate — HUMAN GATE
At every human-bound gate, follow the standing gate UX: surface → options (incl. Approve) →
disclose (gate + active SHA-256 + path(s) + concerns) → one confirmation → execute. Do not push
flag-checking or hash-hunting onto the operator. The three outward-facing gates stay operator-run.
