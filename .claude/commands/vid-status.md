---
description: Show a project's current lifecycle state, per-language track status, and the deterministic next-step recommendation.
argument-hint: <video-id>
model: "@bedrock-eus1/us.anthropic.claude-haiku-4-5-20251001-v1:0"
effort: low
---

Run, in order, and report both results to the user:

```
python3 "$CLAUDE_PROJECT_DIR/.claude/scripts/vid_cli.py" project status $ARGUMENTS
python3 "$CLAUDE_PROJECT_DIR/.claude/scripts/vid_cli.py" project plan $ARGUMENTS
```

Summarize plainly: current top-level state, each language track's stage, any open human
gates (name the gate and what artifact/hash it needs), and the `plan` command's
`autonomy_action` (`PROCEED` / `STOP_AT_GATE` / `BLOCKED` / `TERMINAL`) with its stated
reason. Treat `autonomy_action` as authoritative for what to do next — do not recommend
skipping a `STOP_AT_GATE` or `BLOCKED` result.

If `project status` reports the project does not exist, say so and suggest
`vid_cli.py project init <id> --url <source-url> --targets <iso-codes>` or the
`download-videos` skill instead of guessing at project state.
