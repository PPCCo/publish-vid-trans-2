---
description: Friendly entry point + status router. With no args, greet and offer where-to-start options. With a playlist id, video-id, or a phrase ("videos status", "next commands"), route to the right status view and hand over the next commands.
argument-hint: "[playlist-id | video-id | 'videos status' | 'next commands']"
model: "@bedrock-eus1/us.anthropic.claude-sonnet-5"
effort: low
---

# /hi — status router for the video-translation house

You are the friendly front door. Read `$ARGUMENTS`, decide the user's intent, and route to the
right **read-only** status view — delegating the heavy JSON reading to the `vid-status-reporter`
subagent (spawn it via the Agent tool, `subagent_type: "vid-status-reporter"`). Keep your own
output tight: lead with a one-line tally, then an interactive step. Never dump raw JSON, never a
wall of ids.

This command is **read-only**. It never mutates state and never grants an approval. Any approval
follows rule 13 and is performed by the matching QA/gate skill (see the video-id branch).

Let `ARG = $ARGUMENTS` (trimmed).

## Routing

### A) No args — greet + offer where to start
Greet briefly ("Hi! Here's where things stand — what would you like?"), then ask with
`AskUserQuestion` (single-select), **recommended option first**:
1. **Where do I start? (Recommended)** — spawn reporter `mode=wip`. If anything is in flight,
   drill into it (go to the in-flight drill-down in §E). If nothing is in flight, spawn reporter
   `mode=playlist <first playlist>` (from `catalog playlists`) and recommend kicking off its first
   `not-started` video.
2. **Work-in-progress status** — spawn reporter `mode=wip`; render the aggregate + in-flight rows.
3. **Next commands to run** — spawn reporter `mode=next-commands`.
4. **Browse a playlist** — run `--compact catalog playlists` yourself (cheap), list them, then
   spawn reporter `mode=playlist <chosen>`.

(The tool always adds "Other" — the user can paste an id/phrase there, which re-enters this
router with that as ARG.)

### B) ARG matches a playlist id  (`^(PL|UU|OL)[A-Za-z0-9_-]{10,}$`, or found in `catalog playlists`)
Spawn reporter `mode=playlist <ARG>`. Present: playlist title + the one-line `status_summary`
tally, then the **next actionable video** with its `next_command` verbatim. Then:
- If ≥1 video is in flight → `AskUserQuestion` with the **first 3 in-flight** `video_id`s
  (playlist order, most-advanced first) as options; "Other" = type a video-id / "more". On a
  pick, drill into that video (§E).
- If none in flight → recommend kicking off the first `not-started`; emit its kickoff block in
  **operator-terminal form** (see Handoff blocks below).

### C) ARG matches a video id  (`^yt-` or a bare 11-char youtube id)
Normalize a bare id to `yt-<id>`. Route into the single-project flow — behave exactly like
`/vid-status <id>`: spawn reporter `mode=project <id>`, render it, then follow the gate /
next-command UX (§E). This keeps one code path for single-project drill-down.

### D) ARG is a phrase — cheap keyword match
- contains `video` / `status` / `wip` / `progress` → reporter `mode=wip`, then §E drill-down.
- contains `next` / `command` → reporter `mode=next-commands`.
- contains `playlist` → run `catalog playlists`, list, then reporter `mode=playlist`.
- anything else → treat as free intent; fall back to the no-args menu (§A).

### E) Drill-down / gate + next-command UX (shared by B, C, D)
After rendering a single project's `mode=project` digest, branch on the plan `autonomy_action`:
- **PROCEED** → surface the next step. Offer via `AskUserQuestion`: *Show next command
  (Recommended)* / *Run the manual pipeline (hand off)* / *Full JSON*. For the command, either run
  `--compact nextcmd <id>` and present its block, or (when several deterministic steps remain) the
  `run_pipeline.sh <id> --mt` wrapper — both in operator-terminal form.
- **STOP_AT_GATE** → name the gate + what artifact/hash it needs, then `AskUserQuestion` offering
  the concrete fixes for that gate **and** an explicit **`Approve & advance`** option. Because
  `/hi` is a light router, it does **not** bind hashes or run the grant itself — on any gate
  action, **invoke the matching QA/gate skill** (`qa-transcript` / `qa-translation` /
  `qa-caption-sync` / `qa-audio-sync` / `qa-final`, or `mux-and-package` /
  `prepare-distribution` at their gates),
  which owns the full rule-13 disclose→confirm→execute flow. `/hi` surfaces the decision; the
  skill performs it. The three outward-facing gates (`rights-check`,
  `video-release-authorize`, `video-promote-approve`) stay human-run.
- **BLOCKED** → explain the blocker in plain language; hand over the exact clearing command
  (operator-terminal form).
- **TERMINAL** → report; for `READY_FOR_REVIEW` offer `/prepare-distribution` (human-gated, never
  uploads).

## Rendering rules
- Always lead with the one-line tally when showing an aggregate, e.g.
  `1 in-progress · 3 gate-pending · 1840 not-started` (omit zero buckets; done/in-progress/
  gate-pending/blocked first, not-started last).
- When multiple videos are in flight, show the **first 3** as `AskUserQuestion` options
  (most-advanced-state first) + "Other" (type a video-id / "more"). Never enumerate hundreds.
- Concise. A tally line + one question beats a paragraph.

## Runnable-block conventions
- **You run these yourself in-session** (read-only, no `!` prefix):
  ```
  "$CLAUDE_PROJECT_DIR/.venv/bin/python3" "$CLAUDE_PROJECT_DIR/.claude/scripts/vid_cli.py" --compact catalog playlists
  ```
- **Handoff blocks** for the operator's terminal use the literal absolute path (never `~`, never
  `$CLAUDE_PROJECT_DIR`) + relative paths + no `!`:
  ```
  cd /Users/qaiser.abbas/Dev/my-repos/pub/publish-vid-trans
  export VIDTRANS_FETCH_ENABLED=1
  .claude/scripts/vid_cli.py project init yt-<id> --url <url> --targets en,ar,ur && .claude/scripts/vid_cli.py ingest run yt-<id>
  ```
- A gate-reference line is **never** runnable (rule 13) — present it as reference only.

## Guardrails
- Read-only router; the reporter subagent does the heavy reads.
- Never grant/transition — defer every gate to its QA skill (rule 13). Never approve to unblock.
- Prefer the deterministic CLI fields (`status`, `status_summary`, `next_command`) over anything
  you compute in-prompt.
