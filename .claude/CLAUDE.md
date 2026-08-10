# publish-vid-trans — Company policy (agent operating rules)

A file-based **video-translation production company** for Claude Code. It translates
Persian, Arabic, and Urdu (and other) video speeches into English audio and multi-language
closed captions, autonomously but under human-bound gates.

## Non-negotiable rules

1. **The CLI owns all state.** Never hand-write `state.json`, `manifest.json`, approvals,
   or events. Every mutation goes through `${CLAUDE_PROJECT_DIR}/.venv/bin/python3
   ${CLAUDE_PROJECT_DIR}/.claude/scripts/vid_cli.py`. **Never invoke a bare `python3`/`python`** —
   it resolves to the system interpreter that lacks the venv's deps (jsonschema/yaml/piper/…);
   always use `.venv/bin/python3` (or another explicit interpreter path). This is enforced as a
   hard deny in `.claude/hooks/pre_tool_policy.py`.
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
   **Narrow carve-out — playlist enumeration is metadata-only and flag-free.** `catalog
   add-playlist <url>` (→ `net.fetch.ytdlp_playlist_entries`, `yt-dlp --flat-playlist
   --skip-download`) lists a playlist's video titles/ids/urls without pulling any media, so it
   runs even when `VIDTRANS_FETCH_ENABLED` is unset. This is the *only* flag-free network call;
   every **media** download (`ingest run`, `ingest ensure`, and any per-video kickoff) still
   requires the flag. Enumeration is still host-allowlisted.
4. **Rights precede distribution.** A project runs to `READY_FOR_REVIEW` regardless of
   rights, but nothing in `outputs/` is safe to publish until a human sets `rights_status`
   to a distributable value. `PACKAGE → READY_FOR_REVIEW` is blocked while `unreviewed` or
   `do-not-distribute`.
5. **Voice cloning requires recorded consent.** Cloning the source speaker's voice requires
   `voice_clone_consent: true` in the rights record. (Separately, a cue flagged `keep-source-audio`
   preserves the **original** speaker's own audio for a recited/untranslatable window in every
   language instead of dubbing it — a distinct mechanism, no consent gate; see rule 14.)

   **Male is the hard default piper dub voice for every language.** Voices come from a company
   gender-tagged registry (`dubbing.voices[<lang>][male|female]` in `company.default.json` + real
   `.onnx` paths in gitignored `company.local.json`), **not** hand-passed `--model`. `dub run` with
   no `--model` resolves gender as `--gender` (CLI) → project `dubbing.voice_gender` → company
   `default_voice_gender` (**male**), and **fails loudly** (`DubbingError`) when that gender isn't
   staged — never a silent wrong-gender dub. `dub run --model <path>` wins (operator override).
   Project gender set once at init (`project init --dub-voice-gender`, default male). The **source
   speaker's** gender is a separate human observation at LANGUAGE_ID (`langid set
   --source-voice-gender`, default male); a **female** source is the only trigger to ask whether
   dubs go female. `doctor` lists staged voices (`dub-voice:<lang>:<gender>`).

   **Clone-language exception — `dubbing.clone_languages` (default `["en","zh"]`) are ALWAYS XTTS
   voice-clone, gender N/A.** These langs dub by XTTS clone off `source/audio.wav`, not the piper
   registry. `dub run --language en`/`zh` with no `--model`/`--clone` auto-resolves to `provider=xtts`
   + `clone=True` in `run_dub` (`auto_clone = not clone and model is None and language in
   clone_languages(root)`), **before** the consent gate — so it is consent-gated (this rule) and
   **fails loud** if consent is missing, XTTS isn't installed, or no clone reference exists — never a
   silent piper fallback. `--model`/`--gender` still wins. **This is the source of truth for
   clone-vs-piper**; `tts.per_language` in `tools.default.json` only picks the *engine* for an
   unresolved provider (dead config for clone-vs-piper — why `en` once dubbed piper, rule 8). Tune
   the list in `company.local.json`.

   **Authorized override (2026-08-07) — cloning ON + rights pre-recorded at init.** By human
   authorization, cloning **defaults ON** and rights are auto-recorded at init, easing the
   scripted route. The company `rights` block (`default_voice_clone: true`,
   `auto_consent_at_init: true`, `default_rights_status: "self-authored"`, `default_reviewer:
   "company-standing-authorization"`) drives it: `project init` records `voice_clone_consent: true`
   + `rights_status` **through `rights.set_rights`** (rule 1 — never hand-written; `RIGHTS_SET`
   event). Not a silent bypass — `/new-video` offers a one-click **Neutral voice** opt-out
   (`project init --no-clone`, records no consent), it's opt-out company-wide via
   `default_voice_clone: false`, and the human-only `rights-check` gate can revise (`set_rights`
   overwrites). The three `disable-model-invocation: true` outward-facing skills keep their
   human-only posture regardless. The old "clone intent never flips the rights record" caveat now
   applies only to the `--no-clone`/explicit-intent-flag path; the default path records consent at init.
6. **Artifacts are content-addressed.** Register every produced file with the CLI; approvals
   bind to exact SHA-256 hashes. Editing an approved artifact auto-invalidates its approval.

## Operating rules

These govern *how you work*, not just what the pipeline does:

7. **The source language is never translated.** If `source_language` is confirmed and also
   appears in `target_languages`, that language **skips the TRANSLATION stage** — a source→source
   translation is pointless. Verbatim source-language captions are still produced (target_text ==
   source_text), so the track still has captions/dubbing/packaging deliverables.

   **Source language can be pre-filled at init (convenience, not a gate removal).**
   `project init --source-language <code> [--source-voice-gender <g>]` runs the exact same
   `langid.set_language()` path normally invoked at `LANGUAGE_ID`, right after init — the
   `/kickoff` and `/new-video` onboarding flows ask for it up front (via `AskUserQuestion` in
   `/new-video`; a pinned SETTINGS value in `/kickoff`) instead of deferring the question. It's
   opt-in: omitting the flag reproduces today's exact behavior (`source_language: null`).
   `LANGUAGE_ID` remains the human checkpoint — a human can still correct it later via
   `detect-language`/`langid set` if the guess was wrong for a particular video; `langid set`
   simply overwrites the pre-filled value. Enforced in code:
   `langid set` marks the track `skip_translation: true`; `translate export` writes the verbatim
   caption doc instead of an empty worksheet; the source track is excluded from the translation
   quorum and the `translation_qa` gate. Human QA on that track is caption/editorial only, not
   translation.

   **Two-axis translate/dub scope (human review is scoped to source + English).** Translation
   *and* dubbing happen for **every** selected language, but the **human-review machinery** —
   the human-filled worksheet and the per-language `translation_qa` human approval — is scoped:
   only the **source language** (verbatim, `skip_translation`) and **English** (`en`) keep it.
   Every other translatable target (`fr`, `es`, `zh`, `pt`, `ru`, …) is **AI-auto-translated
   with deterministic QA only — no human gate**, marked `auto_translate: true` on its track at
   creation (`project init` / `add-languages`; source is never `auto_translate`). The AI fills
   those worksheets itself (bounded batches, rule 11 idiom) and `translate import`s them through
   the same caption-build chokepoint; the deterministic `translation-qa` + `glossary` reports
   still run and must pass. Enforced in code at the single gate line
   (`state.transition_blockers` excludes both `skip_translation` and `auto_translate` tracks from
   `translation_qa`); the top-level advance quorum still counts auto tracks (they produce
   captions), so only the *human approval* requirement differs. Don't re-impose uniform gating on
   the non-en targets.
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

    **Regenerating a hallucinating mlx-whisper transcript.** Default `transcript run` (even with its
    built-in escalating retry ladder) can still land on a decode with repetition-hallucination loops
    (the same short phrase/token repeating 20-40x consecutively) and/or long cues that merge real
    speech across a silent/blank stretch. `transcript run --no-condition-on-previous-text` is the
    primary defense against the repetition loops (verified: it eliminated all repeat-runs on a
    re-transcribe that previously had three separate 20-39x loops); pair it with
    `--hallucination-silence-threshold <seconds>` to reduce (not eliminate) merge-over-silence cues —
    it trades cue count for merge count, so expect fewer, longer cues rather than zero long cues; the
    `transcript-qa` gate treats long-span cues as informational "granularity" notes, not blockers, so
    this trade is fine. To regenerate: the project must be back at `TRANSCRIPTION` state first —
    `project reset <id> --drop-transcript --actor human` (rewinds to `LANGUAGE_ID`, keeps `source/`
    media), then `project transition <id> --to TRANSCRIPTION --actor agent` (no gate — source language
    was already confirmed), then the flagged `transcript run --advance`. This is a *plain rerun*, not
    a gated transition — no human confirmation needed to regenerate a not-yet-approved transcript; the
    gate re-fires only when this reaches `TRANSCRIPT_QA_GATE` again for human review.
11. **English review-gloss at `TRANSCRIPT_QA_GATE`.** An English gloss of the source is always
    produced (`transcript english-export` → fill → `transcript english-import` →
    `transcript/english-gloss.json`) and run through an AI context/word-sense pass (the
    `english-context-check` skill, Opus-4.8/high) that verifies word-sense in context, **auto-fixes
    the gloss**, and **flags source-transcript problems** for the human. **Advisory**: findings fold
    into the `transcript-qa` report (`english-context/*`; blocker→FAIL, major→CONDITIONAL_PASS),
    adding no new gate report and leaving the `TRANSCRIPT_QA_GATE → SEGMENT_RESOLUTION` edge
    unchanged. The gloss is a **review aid, not the en caption translation** (TRANSLATION produces
    that independently), registered as an `english-gloss` artifact that never advances a track. The
    AI **never edits the source transcript** — source edits are human/CLI-only (they re-trigger ASR QA).

    **Verse-override review aid + auto-reconcile (Quran/tafsir).** A companion worksheet
    `transcript/english-verses-gloss.worksheet.json` may list only the verse-bearing gloss cues —
    each with its `(Quran s:a)` citation(s), source text, English `translated_text` (== the gloss
    cue's `target_text`), and an empty `modified_translation` the human fills only to change a
    rendering. `transcript reconcile-verses <id>` copies each non-empty `modified_translation` into
    the matching cue's `target_text` in `english-gloss.worksheet.json` (by id) and mirrors it back;
    it is **always the first action of `transcript english-import`** (returned as
    `verses_reconciled`), so plain import picks up latest overrides. Idempotent; no-op when absent.
    Both are fill-in worksheets (not CLI state, not registered — only `english-gloss.json` is), so
    reconcile never touches state/manifest/approvals (rule 1); appends
    `ENGLISH_GLOSS_VERSES_RECONCILED`. **Every cited verse renders inline** in `target_text`: English
    rendering + numeric `(Quran s:a)`, **no Arabic script** (rule 16 — review aid, not shipped
    caption). `ar`'s TRANSLATION captions remain the sole Arabic-verbatim path.

    **Fill the gloss worksheet in bounded batches (avoid single-response timeout).** When the AI is
    the gloss translator, do **not** write all cues in one response — a dense source overflows a turn
    (`API Error: The operation timed out`). Instead write small `_batchN.json` maps
    (`{ "<cue-id>": {"target_text", "context_note"?, "flags_add"?} }`) for a few cues at a time and
    merge into the worksheet by id with a throwaway merge helper (a plain Edit can't target identical
    empty `"target_text": ""` anchors). Helper + `_batch*.json` are scaffolding — delete before/after
    `transcript english-import` (import reads only the worksheet). Idempotent per cue id; never
    hand-edits CLI state.

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
    **The catalog `next_command` is convenience, not a bypass.** `catalog list`/`show` surface a
    derived, `!`-runnable `next_command` (+ `review_files` at a gate step) computed fresh from
    `state.plan()`, never persisted — a power-user shortcut; at `STOP_AT_GATE` it emits the approve
    command **with a `disclose+confirm first` reminder**. It does NOT relax this rule: when *you*
    drive a gate you still surface→options→disclose→confirm→execute, and the three outward-facing
    gates stay human-run.

14. **Constant audio speed — the picture is always re-timed to the audio (mandatory; TASK 2).**
    Dubbed audio is **NEVER time-stretched** to fit a slot (per-cue rubber-banding sounded terrible,
    removed). Every cue plays at its natural TTS length; the **picture** absorbs 100% of the mismatch
    via freeze/trim. Old `quality_bars.audio.max_time_stretch`/`freeze_stretch_cap`/
    `freeze_frame_enabled` bars are retained for reporting only and **no longer gate audio speed** —
    freeze-planning is unconditional: `dub run` **always** writes a per-language freeze plan
    (`audio/freeze-plan.<lang>.json`, registered, traces to dub-wav) and `package mux` **always**
    rebuilds the picture from a non-empty plan (empty plan → plain `-c:v copy`). `media.normalize_wav`
    is format-only, duration identity. **The plan uses a *running gap*, not per-cue overflow** —
    `dub run` lays audio back-to-back (lead silence only when audio is early), so a long cue's overrun
    propagates until a natural pause; per-cue-own-overflow left the backlog uncancelled (FAILed on
    real data). Plan holds the picture by `gap_i = rendered_start_ms - (caption_start_ms + cum_freeze
    - cum_trim)` at each boundary where audio is later than the (re-timed) picture. **Two modes, per
    language via `quality_bars.audio.freeze_trim_languages` (company bar, default `[]`; no CLI flag):**
    - **Hold-only (Model B, default, langs NOT in list):** only freeze/hold, never drop frames. Where
      audio underruns the picture in aggregate a one-sided residual remains (picture lags audio),
      surfaced **honestly** as signed `drift_ms`; reach PASS by accepting it under a raised
      `per_cue_drift_tolerance_ms` (disclosed, recorded relaxation) — for small residuals.
    - **Freeze + trim (Model A, langs IN list):** additionally trims the picture (skips `-gap_i` ms of
      source at the boundary) where audio runs earlier, so residual reaches **0 by construction**
      (`trims[]`/`total_trim_ms` in plan; `trim_planned`/`trim_ms` per cue). Use when hold-only lag is
      too large (e.g. 11.6s ar residual on al-'Asr → `ar` is in the list). Drops real source frames on
      boundaries — accepted trade vs a large desync.
    In `_build_sync_report` post-adjust residual = `rendered_start - (caption_start + cum_freeze -
    cum_trim)`; a planned cue's `drift_ms` → 0 (Model A) or honest residual (Model B) and drops out of
    the over-cap list by construction — so `analyze_sync`'s blocker/major checks read truthful numbers
    and the track PASSes honestly (A) or under raised tolerance (B). Each freeze is an informational
    `audio-freeze-planned` note; a hold beyond `max_freeze_ms_per_cue` (default 4000ms) → an
    `audio-freeze-excessive` **major**. At `package mux` the picture is rebuilt on the post-freeze
    timeline (source slices + frozen inserts, re-encoded H.264/AAC) and both embedded soft-subs and
    standalone `captions.<lang>.vtt/.srt` are re-timed onto it — the **canonical caption doc is never
    edited** (rules 6/12); retimed subs are derived outputs. **Scope:** real-TTS `dub run` only;
    `dub import` has no per-cue natural durations → no plan (`freeze_plan_skipped_reason`). Freezes/
    trims land on cue boundaries (`at_ms` == corrected `caption_start_ms`), never mid-cue.

    **Keep-source-audio cues — preserve the original speaker's voice for a window (rule 5 tie-in).**
    A cue tagged `flags: ["keep-source-audio"]` makes `dub run` splice `source/audio.wav[start:end]`
    into the dub for that cue **instead of TTS**, in **every** language — the original reciter/speaker
    plays there. For recited/untranslatable passages (e.g. a Quran recitation ASR never transcribed)
    where TTS is wrong and silence is worse. Rides the existing `flags` array end-to-end (zero schema
    change) and drives **audio only**; `target_text` still drives the *caption* (non-`ar` = translation
    + `(Quran s:a)`, `ar`/source = Arabic verbatim, rule 16). Needs source media, so `dub run` restores
    it once (fetch-gated `ensure_source_present`) even for a non-clone dub. First branch of the per-cue
    synth in `run_dub`; the slice flows through the same normalize→concat→loudnorm path; mux unchanged.

    **Silent-span QA — a long dead-air run FAILs the track (`silent_span_max_ms`, default 7000ms).**
    `dub run`'s sync report records `silent_spans_ms`/`max_silent_span_ms` (pure-silence runs on the
    rendered timeline: leading + inter-cue gaps ≥ 250ms floor). `analyze_sync` escalates any span
    longer than the bar to a **blocker** → `dub qa` **FAILs** (would have caught the 33s al-ʿAdiyat
    hole). **Keep-source windows are exempt.** Clear a real hole by recovering the missing speech into
    the transcript, or marking the window `keep-source-audio` if genuinely untranslatable.

15. **Per-language still image + deliberate playback speed (TASK 1 / TASK 3).** Two per-language
    presentation choices live in `project.yaml` (which is `additionalProperties: true`) — **not**
    `state.json` — as `lang=value` maps, set at init or adjusted later:
    - **Still image** (`images: {ur: "/abs/a.jpg", …}`): a language showing a fixed image displays
      that one frame for the whole runtime with the dub over it, **instead of** the source video —
      a talking-head/backdrop speech, faster to render, and it needs **no source video and no freeze
      plan** (a static frame can't desync). Other languages keep the source video. Set at
      `project init --images "ur=/p/a.jpg,en=/p/b.png"` or adjust with `project set-image <id>
      --language <iso> --path <file>` / `--clear` (event `IMAGE_SET`). Paths are validated (exists +
      image suffix) and stored absolute. Applies to **dub-enabled** tracks (there must be a dub to
      lay over the image).
    - **Playback speed** (`playback_speed: {en: 1.25, …}`, default 1.0): a **deliberate, uniform,
      whole-video** speed change applied at `package mux` — audio **and** video scaled by the **same**
      factor, so they stay mutually in sync. This is the sanctioned way to speed up a slow source in
      en/ur; it is **not** the per-cue rubber-banding rule 14 forbids (that scales cues
      independently — this scales the whole finished track uniformly). Set at `project init --speeds
      "en=1.25,ur=1.25"` or adjust with `project set-speed <id> --language <iso> --factor <f>`
      (`--factor 1.0` resets; event `SPEED_SET`). Applied **last** in `run_mux`, after
      freeze-retiming/still-image, so it scales whatever picture+audio pair was built.

    **`lang=value` map convention:** `--images` / `--speeds` take a comma-separated `lang=value`
    list (e.g. `en=1.25,ur=1.25`); a language absent from the map keeps the default (source video /
    1.0x). The `/new-video` interview captures both in natural language and parses them to these
    flags.

16. **The TRANSLATION stage ALWAYS fans out in parallel when ≥2 tracks need filling — this is
    standing behavior, never an opt-in.** The per-cue worksheet fill is the token-heavy,
    embarrassingly-parallel part of the pipeline, and each language track is independent. So at
    `TRANSLATION`, whenever **two or more** language tracks still need their `target_text` filled,
    you MUST drive it with a `Workflow` — **one agent per language, run concurrently** — rather
    than filling worksheets inline one after another. Do **not** ask the human whether to
    parallelize and do **not** weigh it as a choice; reach for the workflow first. The only
    inline case is exactly **one** unfilled track (a one-agent fan-out is pure overhead) — there
    the rule 11 bounded-batch idiom still applies.
    **Which tracks are inline vs fan-out — priority langs are filled INLINE, the rest fan out.**
    Because the Workflow `agent()` `model:` opt is silently ignored (a fan-out agent runs on the
    session-default model, not a passed override — root-cause pending), a priority track routed
    into the fan-out would silently draft on Sonnet. So the priority editions are filled **inline
    in the main thread** where they land on the *session* model, which must be Opus:
    - **`en`, `ar`, `ur` (the priority languages) are filled INLINE**, one at a time, in the main
      thread — never as fan-out agents — so they draft on the session model. `translate export`
      runs a **fail-loud model check** (`_priority_model_check`): if the live session model
      (`ANTHROPIC_MODEL`) isn't the configured `priority_model` (Opus), the export result carries a
      loud `priority_model_warning` telling you to switch to Opus (`/model opus`) **before** filling
      that worksheet. Do not fill a priority worksheet while that warning is present — switch first
      (or drive it from an Opus context), then fill. `en` still **STOPS at its `translation_qa`
      human gate** after import (rules 2/13). Use the rule 11 bounded-batch idiom for each inline
      track. When several priority tracks are unfilled, do them sequentially inline (they share the
      Opus session), not via a fan-out.
    - **Every non-priority target** (`zh`, `fr`, `es`, `pt`, `ru`, … — all `auto_translate`) is a
      **fan-out agent on Sonnet** (`default_model`). When ≥2 of them need filling, you MUST drive
      them with a `Workflow`, one agent per language, concurrently (this is the standing ≥2-track
      fan-out behavior — no opt-in, no human choice). Exactly one non-priority track → fill it
      inline (a one-agent fan-out is pure overhead). No human gate on these.
    - The `skip_translation` source track never joins either path (verbatim, no worksheet).

    **Model policy — priority langs on Opus, the rest on Sonnet (2026-08-10; config-driven).**
    `en`/`ar`/`ur` render on `@bedrock-eus2/us.anthropic.claude-opus-4-8`/`high`; every other target
    on `@bedrock-eus1/us.anthropic.claude-sonnet-5`/`high`. Fixed per-language split — **no verify
    pass, no escalation, no redo tracking**. List + models in company config
    (`translation_models.priority_languages`/`priority_model`/`default_model`/`effort`). Enforced by
    *placement*, not the ignored `agent()` `model:` opt: priority langs fill inline on the Opus
    session (fail-loud at `translate export`); the rest fan out on Sonnet. `en` still STOPS at its
    `translation_qa` gate. Operator model override wins.
    **Canonical pattern.** (a) `translate export` every worksheet first (cue ids/timing exist);
    (b) build one compact shared **pivot** agents render from — for tafsir/Quran, `{cue: {source,
    en_gloss, en_meaning}}`, so every language renders the same meaning + canonical verse set. **A
    cited Quranic verse renders as the *target language's* translation + numeric `(Quran <surah>:<ayah>)`
    (e.g. `(Quran 100:6)`). Arabic script/transliteration NEVER appears in a non-`ar` `target_text`/
    `.srt`/`.vtt` (nor its dub — rule 14 speaks `target_text` verbatim). Only `ar` carries the verse
    in Arabic verbatim.** **The `(Quran s:a)` citation is caption-only, never spoken:** it stays
    inline in `target_text` but `dub run` synthesizes from a citation-stripped copy
    (`dubbing._strip_citations`, `re.sub(r"\s*\(Quran[^)]*\)", "", text)` + collapse) in **every**
    language incl. `ar`. The caption artifact is never mutated — only the string to `synthesize_cue`;
    the keep-source splice and render paths are untouched. (Reversed 2026-08-09 from "Arabic script +
    translation in every language" — the Arabic strings corrupted the piper dubs. Per-language
    `distribution/notes/<lang>.txt` docs keep Arabic verse citations, rule 11.) Pivot goes to a
    scratch path; (c) `parallel(langs.map(...))` one `agent()` per language, each with a `schema`
    forcing a validated `{cues:[{id,target_text}]}` — the agent reads the pivot, renders all cues,
    returns the map; (d) merge each map into `captions/<iso>.worksheet.json` by cue id with a
    throwaway helper (deleted after), then `translate import` each track. Timing is the contract:
    never change `id`/`start_ms`/`end_ms`/`source_text`, never merge/split cues. The CLI still imports
    (rule 1); auto-split (rule 12) + deterministic `translation-qa`+`glossary` QA run unchanged. This
    is the standing procedure in the `create-closed-captions` translation stage.

    **Standalone helper verbs (no project needed):**
    - `vid_cli.py size w 1042` / `size h 583` — from one axis compute the full 16:9 (or `--aspect
      W:H`) frame, even-rounded for H.264 (e.g. `1042x586`). Sizes the still-image canvas. Pure math.
    - `vid_cli.py speed 1.25 <any.mp4> [--out <path>]` — uniformly re-time **any** video (audio+video),
      **source untouched**, output beside it as `<stem>_<factor><suffix>` when `--out` omitted. Refuses
      to overwrite the source.
    - `vid_cli.py cmd <video-id-or-url>` / `vid_cli.py nextcmd <project-id>` — **read-only command
      printers**: print (never run) the copy-paste terminal block for onboarding/ingesting a video/
      playlist (`cmd`) or a project's next step (`nextcmd`). Where a step maps to both an external tool
      and a CLI verb, both are labelled options with their env preamble (raw `yt-dlp` → `export
      VIDTRANS_FETCH_ENABLED=1`; `ingest run` → full `source .env.local`; playlist enum → none, rule 3);
      a pure state op falls back to the CLI verb. At `STOP_AT_GATE` `nextcmd` prints only the gate
      explanation + approve line **as reference** (never runnable — rule 13). Bare-terminal by default;
      `--for-claude` re-adds `!` on runnable lines only. A template-driven **view** over state (same
      argv builders as the real downloader), never mutates anything (rule 1).

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
to translate+dub; a source-language add is marked `skip_translation`, a non-en/non-source add is
marked `auto_translate` (rule 7).

To **turn on dubbing for an already-translated language** ("add es dubbing for <id>"), use
`project enable-dub <id> --targets <codes>` — flips `dub_enabled` on existing track(s), reuses their
captions (non-clone dub has no source-video dep); no re-translate/re-init. `--disable` is the inverse
(back to captions-only; refuses to strand a produced `dub-wav` unless `--force`, rule 6). If the track
doesn't exist, `add-languages` first. A `--clone` dub or `package mux` finding `source/` deleted
re-fetches via `ingest ensure` (flag-gated).

To **re-render one dub track after it has advanced past the audio gate** (e.g. a freeze/trim-plan
fix on just `ar` when en/ur/zh are approved), use `project redub <id> --targets <codes>` — *not*
`project reset` (which wipes ALL downstream work). It pulls `current_state` **backward** to
`AUDIO_SYNC_ADJUST` only if past it, resets **only** the named track(s) to dubbing, leaves everything
else untouched. Refuses captions-only tracks (→ `enable-dub` first), never pushes state forward.
After it runs: re-`dub run` the language (fresh `dub-wav@<lang>` supersedes the old; rule 6
auto-invalidates the stale `audio_qa` approval), `dub qa`, then re-surface the `audio_qa` gate (rule
13). Logs `TRACK_REDUB_REQUESTED`.

**Running `dub run` for several languages — one ffmpeg-heavy job at a time; NO network env.**
`dub run` is **per-language** (`--language <iso>`); run multiple languages **one command at a time**,
each fully returning first. Don't run two `dub run`s (or a dub + `package mux`) concurrently or in an
overlapping loop. **Root cause (verified 2026-08-07):** the concat used to open one ffmpeg handle per
cue (400+) via an N-way `filter_complex`; under concurrent load that exhausted FDs and ffmpeg died on
its banner (`rc 232`), surfaced as a misleading `ffmpeg concat failed (232)`. **Fixed:**
`media.concat_wavs` now uses the concat demuxer (single `-i playlist.txt`, one handle), and `media.py`
errors show the stderr *tail* not the banner head. Still run one ffmpeg-heavy job at a time. A
**non-clone** `dub run` needs **no env** (local piper TTS + ffmpeg over built captions); the fetch
flag + CA bundle are only for media downloads (`ingest run`/`ensure`, a `--clone` dub, or a `package
mux` re-fetching deleted source, rule 3). `dub qa` takes **no** `--language` — run once, QAs all dubs.

To **reconcile the two-axis markers on a project created before the feature** (no
`skip_translation`/`auto_translate` on its tracks), use `project sync-scope <id>` — it recomputes
both markers from `source_language` + the `en` rule (source→`skip_translation`; en+source
human-reviewed; every other target→`auto_translate`), idempotently, without touching
dub/artifacts/approvals. New projects don't need it (`init` + `langid set` already mark correctly).

To **onboard a new video or a whole playlist**, use the `/new-video` skill (URL → confirm
translate + dub sets → `project init` + `ingest run`). A **playlist** URL routes to `catalog
add-playlist <url>` (flag-free enumeration; indexes each video under the playlist, no media).
`catalog list` / `catalog show <id>` / `catalog playlist <plid>` surface each entry's derived
`next_command` (+ `review_files` at gates) for the current phase.

To **run the pipeline cheaply (token-thrifty scripted/manual route)**, use `/process-manual
<id>` — the alternate to the Claude-driven skills, which stay intact. Almost the entire
transcribe→translate→dub chain is pure deterministic script; the one non-deterministic step
(translation worksheet + rule-11 English gloss fill) is handled by an **MT engine** (opt-in
`translation.mt_baseline` in `tools.local.json`; staged offline per OPERATING-GUIDE.md §6),
so with `--mt` the whole chain runs with **zero Claude tokens**. `project autopilot <id> [--mt]
[--until <state>] [--dry-run]` drives the deterministic loop on `state.plan()` and stops at the
**first human gate / blocker** — it can NEVER grant an approval or cross a gate (rules 2/13
hold structurally; it only runs PROCEED-eligible non-gate verbs). `run_pipeline.sh <id> --mt`
is the one-command wrapper (sets `VIDTRANS_FETCH_ENABLED=1` + the proxy CA bundle, idempotent).
The contract: the operator runs the script → it halts at a gate → they say **"the manual
process for `<id>` is done"** → Claude runs the editorial QA at each pending gate and walks the
operator through approval (rule 13). That QA is the **only** place this route spends tokens.
Without `--mt`, autopilot stops at `TRANSLATION` (worksheet-fill needs MT or the Claude route).
Source-language confirmation at `LANGUAGE_ID` is still a human decision (ASR auto-detect is
unavailable), so a fresh project halts there until the source is set.

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
