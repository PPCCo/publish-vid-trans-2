# publish-vid-trans — Company policy (agent operating rules)

A file-based **video-translation production company** for Claude Code. It translates
Persian, Arabic, and Urdu (and other) video speeches into English audio and multi-language
closed captions, autonomously but under human-bound gates.

## Non-negotiable rules

1. **The CLI owns all state.** Never hand-write `state.json`, `manifest.json`, approvals,
   or events. Every mutation goes through `python3 ${CLAUDE_PROJECT_DIR}/.claude/scripts/vid_cli.py`.
2. **Gates are human-bound — a grant records a human decision, not the agent's.** You may
   *recommend* a transition and *prepare* a gate packet on your own initiative. You may run
   `approval grant` / a gated `project transition` / `rights set` **only** as the mechanical
   execution of an explicit human decision you have just obtained in-conversation, under the
   disclosure-and-confirm protocol in rule 13 (surface the gate → present options incl. an
   *approve* option → disclose gate + artifact hash + path(s) + concerns → get one explicit
   human confirmation → then execute, recording the human as `--approver`/`--actor`). Absent
   that confirmation you may never grant, transition, or set rights status. Never approve to
   unblock your own work. The `disable-model-invocation: true` skills (rights, release,
   promotion) stay human-only at the skill layer — see rule 13 for which gates keep the tighter
   posture.
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

    **Filling the gloss worksheet in batches (avoid the single-response timeout).** When the AI is
    the gloss translator, do **not** try to write all cues' `target_text` in one response — a dense
    source (dozens of cues, some 900–1600 chars of Persian/Arabic) overflows a single turn and yields
    an `API Error: The operation timed out`. Instead fill the worksheet **programmatically in bounded
    batches**: write a small `_batchN.json` of `{ "<cue-id>": {"target_text", "context_note"?,
    "flags_add"?} }` for a handful of cues at a time and merge it into `english-gloss.worksheet.json`
    by cue id with a throwaway merge-by-id helper (a plain Edit can't target the identical empty
    `"target_text": ""` anchors). The merge helper and `_batch*.json` are **scaffolding, not
    deliverables** — delete them before/after `transcript english-import` (import reads only the
    worksheet). This keeps each turn small, is idempotent per cue id, and never hand-edits CLI-owned
    state (the worksheet is a fill-in artifact, not `state.json`/manifest/approvals).

12. **Long caption cues auto-split at caption build.** Cue durations are inherited from the ASR
    transcript, which can merge long uninterrupted passages into multi-minute cues. At caption-build
    time (`translate import` and the source-verbatim path) any cue longer than
    `quality_bars.captions.max_cue_duration_ms` (default 7000ms) is deterministically split into N
    proportional ≤-cap sub-cues (equal integer-ms time slices; text divided at sentence→word→char
    boundaries; renumbered 0..M). This is **caption-only**: the human-approved source transcript —
    and the rule 11 English gloss built from it — keep their original cues and are **never re-timed**
    (the transcript stays the approved artifact). Split output is pure/deterministic (stable
    re-render hashes) and schema-validated. Implemented in `captions.split_long_cues`, wired at the
    single caption-doc chokepoint in `translate.py`.

13. **Human gates are decide-not-operate — the human decides in natural language; you do the
    plumbing.** The human should never have to type a CLI command or an `!` block to clear a gate.
    At every human-bound gate:
    (a) **Surface the gate.** Whenever asked about a project's status — including in a brand-new
        session with no prior context — if it sits at a human gate, say so plainly ("it's at human
        approval, the `<gate>` gate"), summarize what's under review, and lead straight into (b).
        Don't make the human dig for the fact that a decision is waiting on them.
    (b) **Present the decision as selectable options** via `AskUserQuestion` — the concrete fixes for
        any issue (e.g. *accept-as-is* / *I edit the source* / *I transcribe it for you*) **and an
        explicit `Approve & advance` option**. Approve is offered as *an option to select*, never as a
        command to run.
    (c) **Disclose, then confirm — this is the recorded decision.** When the human picks *approve*,
        echo back and get **one explicit confirmation** of: the **gate**; the exact **artifact
        SHA-256** you'll bind (the **active**, non-superseded hash on disk — verify against the file,
        not just manifest order); the **relative path(s)** being reviewed/approved; and **any
        issues/concerns** you want the human to look at, each with a brief plain-language explanation.
        Log that confirmation into the approval `--notes` (and/or a short `reviews/` note).
    (d) **Execute the confirmed decision yourself.** After the confirmation you MAY run `approval
        grant` (`--approver "<human>"`, the bound `--artifact` hash, decision + concerns in `--notes`)
        and the gated `project transition` (`--actor human`) as ordinary Bash calls — no `!` block
        required. Verify flags with `--help` first (`approval grant` → `--approver`; `project
        transition` → `--actor`); never push flag-checking or hash-hunting onto the human. If the
        human would rather run it themselves, offer an `!` block, but the default is you execute it.
    **Guardrails:** never grant/transition **without** the explicit post-disclosure confirmation
    (rule 2); never approve to unblock your own work. The `disable-model-invocation: true` gates
    (`rights-check`, `video-release-authorize`, `video-promote-approve`) are **outward-facing / legal**
    and keep the tighter posture — those stay human-run (the skill layer enforces it); you still do
    all the fact-gathering and disclosure, but the human runs the final `!` command for those three.
    This is the standing gate UX; the `## Stop / Escalate — HUMAN GATE` section of every gate skill
    follows it.

## Where to start

Run `/vid-status <video-id>` (or `vid_cli.py project status <id>`) first. Use
`vid_cli.py project plan <id>` for the deterministic next-step recommendation; treat its
`autonomy_action` (`PROCEED` / `STOP_AT_GATE` / `BLOCKED` / `TERMINAL`) as authoritative.

To start a project over or remove it, use the sanctioned teardown verbs — never `rm -rf` +
hand-editing the catalog (rule 1). `project reset <id>` wipes downstream work and rewinds
(by default keeps the expensive `source/` media + source ASR transcript and resumes at
`TRANSCRIPTION`; `--full` drops those too, back to `INGEST`); `project delete <id>` removes the
project directory and its catalog entry. Both append events — history is preserved, not rewritten.
See `OPERATING-GUIDE.md` §8.

To **widen an in-progress project's languages** (add more translation/dub tracks without
discarding existing work), use `project add-languages <id> --targets <codes> [--audio <subset>]`
— *not* reset+re-init. It appends new tracks at `TRANSLATION`, leaves existing tracks/approvals/
artifacts untouched, updates state + config, and logs `LANGUAGES_ADDED`. Added languages default
to translate+dub; a source-language add is marked `skip_translation` (rule 7).

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
