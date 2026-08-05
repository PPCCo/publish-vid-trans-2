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

## Operating rules

These govern *how you work*, not just what the pipeline does:

7. **The source language is never translated.** If `source_language` is confirmed and also
   appears in `target_languages`, that language **skips the TRANSLATION stage** — a source→source
   translation is pointless. Verbatim source-language captions are still produced (target_text ==
   source_text), so the track still has captions/dubbing/packaging deliverables. Enforced in code:
   `langid set` marks the track `skip_translation: true`; `translate export` writes the verbatim
   caption doc instead of an empty worksheet; the source track is excluded from the translation
   quorum and the `translation_qa` gate. Human QA on that track is caption/editorial only, not
   translation.
8. **Always validate assumptions — never guess or hallucinate a cause/fix.** When something
   breaks, reproduce and confirm the cause with a command or a file read *before* acting on it.
   (This is how the HuggingFace 503 was traced to a corporate policy block rather than an outage,
   and how the "decode is random" assumption was disproven by reading `mlx_whisper --help` —
   temperature defaults to 0, i.e. deterministic.) A fix built on an unverified assumption is a
   guess; state what you verified and how.
9. **Record resolutions.** When you resolve a non-obvious issue, write it down: to project memory
   and/or `.claude/CLAUDE.md`. If it concerns human input or the operator workflow (what a person
   has to type/do), update `.claude/CLAUDE.md` and/or `OPERATING-GUIDE.md` too. Don't re-derive the
   same fix twice.
10. **Offline models when HuggingFace is blocked.** HF is blocked on this network by corporate
    policy (not an outage). To run an ASR/TTS model, stage it through GitHub and reconstruct the HF
    cache offline — see the "Offline model when HuggingFace is blocked" recipe in
    `OPERATING-GUIDE.md` §6.
11. **English review-gloss at `TRANSCRIPT_QA_GATE`.** At the transcript gate an English gloss of
    the source speech is always produced (`transcript english-export` → fill → `transcript
    english-import` → `transcript/english-gloss.json`) and put through an AI context/word-sense pass
    (the `english-context-check` skill, Opus-4.8/high) that verifies the words make sense in the
    speech's context, **auto-fixes the English gloss** where it can, and **flags source-transcript
    problems** for the human — so the human can validate the source transcript, the English gloss,
    or both. This is **advisory**: the gloss's context findings fold into the existing
    `transcript-qa` report (`english-context/*` categories; blocker→FAIL, major→CONDITIONAL_PASS),
    adding no new required gate report and leaving the `TRANSCRIPT_QA_GATE → SEGMENT_RESOLUTION`
    edge wiring unchanged. The gloss is a **review aid, not the en caption translation** (the later
    per-language TRANSLATION stage still produces that independently, unchanged) and is registered
    as an `english-gloss` artifact that never advances a language track. The AI **never edits the
    source transcript** — source edits are human/CLI-only (they change an approved artifact and
    re-trigger ASR QA).

## Where to start

Run `/vid-status <video-id>` (or `vid_cli.py project status <id>`) first. Use
`vid_cli.py project plan <id>` for the deterministic next-step recommendation; treat its
`autonomy_action` (`PROCEED` / `STOP_AT_GATE` / `BLOCKED` / `TERMINAL`) as authoritative.

## Models

Only these three model IDs are permitted:
- `@bedrock-eus2/us.anthropic.claude-opus-4-8` — high-stakes editorial/quality/orchestration.
- `@bedrock-eus1/us.anthropic.claude-sonnet-5` — production/build and bounded comparison QA.
- `@bedrock-eus1/us.anthropic.claude-haiku-4-5-20251001-v1:0` — cheap, narrow tasks.

Effort (reasoning depth) and model (capability class) are separate axes. Every skill and
command sets both explicitly in its frontmatter (`model:` + `effort:`, values
`low`/`medium`/`high`/`xhigh`/`max`) rather than inheriting — pick the model for the task's
required capability class and the effort for how much reasoning depth that specific task
warrants (e.g. a bounded QA/proofreading task can run `xhigh` effort on Sonnet without
needing Opus). Any new skill, command, or agent added to this repo should set both fields
the same way; don't leave them to inherit silently unless the task is truly trivial and
`low`/Haiku is the deliberate choice.
