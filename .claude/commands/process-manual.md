---
description: Run the transcribe→translate→dub pipeline via the token-thrifty scripted route (hand-off), or QA + clear gates after a scripted run is done.
argument-hint: <video-id> [done]
model: "@bedrock-eus2/us.anthropic.claude-opus-4-8"
effort: high
---

Invoke the **process-manual** skill for `$ARGUMENTS`.

- `/process-manual <id>` → **hand-off mode**: surface the single `run_pipeline.sh <id> --mt`
  command that runs the deterministic pipeline outside Claude and stops at the next human gate,
  and where it will stop. Do NOT run the pipeline in-conversation — that's the token spend this
  route avoids.
- `/process-manual <id> done` (or the operator says "the manual process for `<id>` is done") →
  **resume / QA mode**: run the editorial QA for each pending gate and walk the operator through
  approval per rule 13.

Follow the skill's procedure exactly, including the disclose→confirm gate protocol.
