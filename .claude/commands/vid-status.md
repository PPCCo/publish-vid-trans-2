---
description: Rich single-project drill-down — current lifecycle state, per-language track table, rights posture, and the deterministic next step, with AskUserQuestion for the next command or a gate approval (approvals are performed by the matching QA skill, rule 13).
argument-hint: <video-id>
model: "@bedrock-eus1/us.anthropic.claude-haiku-4-5-20251001-v1:0"
effort: low
---

# /vid-status — single-project status + next step

For one video (`ID = $ARGUMENTS`), report where it sits and hand over the next step. Read-only:
this command surfaces the decision; it never mutates state and never grants an approval itself.
For an aggregate / playlist / "where do I start" view, that's `/hi`.

## Procedure (do in ONE response)

### 1. Read the project state
Run both (self-executing, no `!`):
```
"$CLAUDE_PROJECT_DIR/.venv/bin/python3" "$CLAUDE_PROJECT_DIR/.claude/scripts/vid_cli.py" project status $ARGUMENTS
"$CLAUDE_PROJECT_DIR/.venv/bin/python3" "$CLAUDE_PROJECT_DIR/.claude/scripts/vid_cli.py" project plan $ARGUMENTS
```
If `project status` reports the project does not exist, say so and suggest `/hi` (to see what
exists) or `/new-video` / `/kickoff <id>` to onboard it — then stop.

### 2. Report richly
- `current_state` (+ `previous_state` if set).
- A per-language track table: language · stage · status, flagging `skip_translation` /
  `auto_translate` / `dub_enabled` where set.
- Rights posture: `rights_status` + `voice_clone_consent` (from `project status`).
- Any open human gate: name it and what artifact/hash it needs.
- The plan's `autonomy_action` (`PROCEED` / `STOP_AT_GATE` / `BLOCKED` / `TERMINAL`) + its reason.
  Treat `autonomy_action` as **authoritative** — never recommend skipping a `STOP_AT_GATE` or
  `BLOCKED`.

### 3. Offer the next step (AskUserQuestion), branching on `autonomy_action`

**PROCEED** — a non-gate step is next. `AskUserQuestion`, recommended first:
1. **Show the next command (Recommended)** — run `nextcmd <ID>` and present its block verbatim in
   operator-terminal form.
2. **Run the manual pipeline (hand off)** — present the token-thrifty wrapper (runs all remaining
   deterministic steps, halts at the next gate):
   ```
   cd /Users/qaiser.abbas/Dev/my-repos/pub/publish-vid-trans
   .claude/scripts/run_pipeline.sh <ID> --mt
   ```
   Remind: when it halts, say **"the manual process for `<ID>` is done"** and Claude resumes QA.
3. **Full JSON** — print the raw `project status` / `project plan`.

**STOP_AT_GATE** — a human gate is next. Name the gate and what it needs, then `AskUserQuestion`
offering the **concrete fixes** for that gate (e.g. *accept-as-is* / *I edit the source* /
*I transcribe it for you*, per the gate) **and** an explicit **`Approve & advance`** option.
On ANY gate action — because this command is Haiku/low and must never bind a hash or run a grant
itself — **invoke the matching QA/gate skill**, which owns the full rule-13
disclose→confirm→execute flow in its own (heavier) model:
- `TRANSCRIPT_QA_GATE` → `qa-transcript`
- `TRANSLATION_QA_GATE` → `qa-translation`
- `CAPTION_VALIDATION` (the caption gate) → `qa-caption-sync`
- `AUDIO_QA_GATE` → `qa-audio-sync`
- `FINAL_QA_GATE` → `qa-final`
- `PACKAGE → READY_FOR_REVIEW` (packaging) → `mux-and-package` / `prepare-distribution`
- the outward-facing gates — `PACKAGE`/`PLATFORM_PACKAGING` rights (`rights-check`),
  `RELEASE_AUTHORIZATION` (`video-release-authorize`), `PROMOTION_QUEUE`
  (`video-promote-approve`) — stay human-run: the skill discloses, the operator runs the final `!`.
`/vid-status` surfaces the decision; the skill performs it. Do not grant/transition here.

**BLOCKED** — read `candidates[].blockers`, explain in plain language, and hand over the exact
clearing command (operator-terminal form) — e.g. re-fetch source via `ingest ensure`, or confirm
the source language via `langid set` if stuck at `LANGUAGE_ID` (that's a human decision).

**TERMINAL** — report it; for `READY_FOR_REVIEW` offer `/prepare-distribution` (human-gated, never
uploads). For `CANCELLED`/`ERROR`, say so and point at the sanctioned recovery verb
(`project reset` / `project delete`).

## Runnable-block conventions
- Read-only calls you run in-session: `"$CLAUDE_PROJECT_DIR/.venv/bin/python3" …`, no `!`.
- Operator-terminal handoff blocks: `cd /Users/qaiser.abbas/Dev/my-repos/pub/publish-vid-trans`
  (literal absolute path — never `~`, never `$CLAUDE_PROJECT_DIR`) + relative paths + no `!`.
- A gate-reference line is never runnable (rule 13) — reference only.

## Guardrails
- Never run a gate transition or `approval grant` here — defer to the QA skill (rules 2/13);
  never approve to unblock your own work.
- Route entirely off the project's own `state` / `plan`, never kickoff SETTINGS.
