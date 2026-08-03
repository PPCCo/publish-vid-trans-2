# publish-vid-trans — Company policy (agent operating rules)

A file-based **video-translation production company** for Claude Code. It translates
Persian, Arabic, and Urdu (and other) video speeches into English audio and multi-language
closed captions, autonomously but under human-bound gates.

## Non-negotiable rules

1. **The CLI owns all state.** Never hand-write `state.json`, `manifest.json`, approvals,
   or events. Every mutation goes through `python3 ${CLAUDE_PROJECT_DIR}/.claude/scripts/vid_cli.py`.
2. **Gates are human-bound.** You may *recommend* a transition and *prepare* a gate packet.
   You may never grant an approval, set rights status, or authorize a transition through a
   human gate. Those are human-only skills (`disable-model-invocation: true`).
3. **No external publication by default.** The pipeline stops at `READY_FOR_REVIEW` and
   produces upload-ready packages. It does not upload to YouTube or any platform. Ingest
   downloads (yt-dlp) and any vendor API call happen ONLY through the sanctioned network
   module and ONLY when `VIDTRANS_FETCH_ENABLED=1`.
4. **Rights precede distribution.** A project runs to `READY_FOR_REVIEW` regardless of
   rights, but nothing in `outputs/` is safe to publish until a human sets `rights_status`
   to a distributable value. `PACKAGE → READY_FOR_REVIEW` is blocked while `unreviewed` or
   `do-not-distribute`.
5. **Voice cloning requires recorded consent.** Default dubbing uses a neutral voice.
   Cloning the source speaker's voice requires `voice_clone_consent: true` in the rights
   record.
6. **Artifacts are content-addressed.** Register every produced file with the CLI; approvals
   bind to exact SHA-256 hashes. Editing an approved artifact auto-invalidates its approval.

## Where to start

Run `/vid-status <video-id>` (or `vid_cli.py project status <id>`) first. Use
`vid_cli.py project plan <id>` for the deterministic next-step recommendation; treat its
`autonomy_action` (`PROCEED` / `STOP_AT_GATE` / `BLOCKED` / `TERMINAL`) as authoritative.

## Models

Only these three model IDs are permitted:
- `@bedrock-eus2/us.anthropic.claude-opus-4-8` — high-stakes editorial/quality/orchestration.
- `@bedrock-eus1/us.anthropic.claude-sonnet-5` — production/build and bounded comparison QA.
- `@bedrock-eus1/us.anthropic.claude-haiku-4-5-20251001-v1:0` — cheap, narrow tasks.

Effort (reasoning depth) and model (capability class) are separate axes.
