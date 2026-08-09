---
description: Resume an in-progress video — read its own configured settings, show where it sits, and hand over the exact next external command (or drive the human gate). Uses the project's real state, never the kickoff SETTINGS.
argument-hint: <video-id>   (a video already onboarded, e.g. yt-MFuUIoF5PSc)
model: "@bedrock-eus2/us.anthropic.claude-opus-4-8"
effort: medium
---

# /continue — resume an in-progress video

For a video that is **already onboarded** (a `projects/<ID>/` directory exists), figure out where
it sits in the pipeline **from its own state/config** — NOT from the `/kickoff` SETTINGS — and
hand the operator the exact next step. This command is read-only: it prints external commands and
drives human gates; it never mutates state or crosses a gate on its own.

Let `ID = $ARGUMENTS`.

## Procedure (do in ONE response)

### 1. Confirm it's really in progress
Run `.venv/bin/python3 .claude/scripts/vid_cli.py project status <ID>`.
- If it errors with `Project not found` / `ProjectNotFoundError` → this video is **not** onboarded.
  Tell the operator to onboard it first with `/kickoff <ID>`, and stop.
- Otherwise continue.

### 2. Read the project's OWN settings (for context, not for editing)
From the `status` JSON, surface a short summary so the operator sees what was actually configured
(these override any kickoff defaults — a project may have been onboarded with different values):
- `state.current_state` and `previous_state`
- `config.target_languages` and `config.audio_languages`
- `config.dubbing.voice_clone`
- `config.glossary_id`
- per-language `playback_speed` / `images` if present in `project.yaml`
- rights posture (`voice_clone_consent`, `rights_status`) via
  `.venv/bin/python3 .claude/scripts/vid_cli.py rights show <ID>` if useful

Do **not** propose changing any of these. If the operator wants to change languages/dub/speed/
images on an in-progress project, point them at the sanctioned verbs (`project add-languages`,
`project enable-dub`, `project set-speed`, `project set-image`, `project redub`) rather than
re-onboarding — but only when they ask.

### 3. Get the deterministic next step
Run `.venv/bin/python3 .claude/scripts/vid_cli.py project plan <ID>` and read `autonomy_action` +
`is_human_gate` (authoritative). Then branch:

**A) `autonomy_action == PROCEED` (a non-gate step is next).**
Surface the next external command via the sanctioned printer:
`.venv/bin/python3 .claude/scripts/vid_cli.py nextcmd <ID>` and present its block verbatim. It is one of:
- a raw-external + `vid_cli.py` option pair (e.g. an ingest / media step),
- a `CLI-only state op` line (a pure state op like `dub run … --advance`), or
- for the whole deterministic remainder, the token-thrifty wrapper:
  ```
  cd /Users/qaiser.abbas/Dev/my-repos/pub/publish-vid-trans
  .claude/scripts/run_pipeline.sh <ID> --mt
  ```
  Prefer `run_pipeline.sh <ID> --mt` when several deterministic steps remain (it runs them all
  outside Claude and halts at the next gate); use the single `nextcmd` line when only one discrete
  external step is pending. Emit bare-terminal form. Then remind the operator: when it halts, say
  **"the manual process for `<ID>` is done"** and Claude resumes QA (step B).

**B) `autonomy_action == STOP_AT_GATE` / `is_human_gate == true` (a human gate is next).**
Do **NOT** print a runnable command (rule 13). Instead **drive the gate in-conversation**:
- Invoke the matching QA/gate skill for `state.current_state` (e.g. `qa-transcript`,
  `qa-translation`, `qa-audio-sync`, `qa-final`, or `mux-and-package` / `prepare-distribution` at
  their gates), or run `/process-manual <ID> done` (resume/QA mode) to QA all pending gates.
- Follow the standing gate UX (rule 13): surface the gate → present options incl.
  `Approve & advance` via `AskUserQuestion` → disclose gate + active artifact SHA-256 + path(s) +
  concerns → get one explicit confirmation → then execute the grant/transition yourself.
- The three outward-facing gates (`rights-check`, `video-release-authorize`,
  `video-promote-approve`) stay human-run: do the disclosure, but hand the operator the final `!`
  command for those three.

**C) `autonomy_action == BLOCKED`.**
Read the plan `candidates[].blockers`, explain the blocker in plain language, and hand over the
exact external/CLI command that clears it (e.g. re-fetch source via `ingest ensure`, confirm the
source language via `langid set` if stuck at LANGUAGE_ID). If LANGUAGE_ID needs the source set,
note the project's source language is a human decision — confirm it with the operator, then the
next `run_pipeline.sh <ID> --mt` proceeds.

**D) `autonomy_action == TERMINAL`.**
The project is at a terminal state (e.g. `READY_FOR_REVIEW`, `CANCELLED`). Report it and, for
`READY_FOR_REVIEW`, offer the distribution path (`/prepare-distribution` — human-gated, never
uploads).

## Guardrails
- Route entirely off the project's **own** `state`/`config`/`plan` — never the kickoff SETTINGS.
- Never run a gate transition or `approval grant` except as the disclosed+confirmed execution of a
  human decision (rules 2/13); never approve to unblock your own work.
- Emit external commands in bare-terminal form (no `!`); a gate reference line is never runnable.
