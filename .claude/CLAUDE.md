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
5. **Voice cloning requires recorded consent.** Default dubbing uses a neutral voice.
   Cloning the source speaker's voice requires `voice_clone_consent: true` in the rights
   record.

   **Male is the hard default dub voice for every language.** Dub voices are chosen from a
   company-owned, gender-tagged registry (`dubbing.voices[<lang>][male|female]` in
   `company.default.json` + real `.onnx` paths in gitignored `company.local.json`), **not** by
   hand-passing `--model`. `dub run` with no `--model` resolves the voice as
   `--gender` (CLI) → project `dubbing.voice_gender` → company `default_voice_gender` (**male**),
   and **fails loudly** (`DubbingError`, staging message) when that gender isn't staged for the
   language — never a silent wrong-gender dub. An explicit `dub run --model <path>` still wins
   (operator override). The chosen project gender is set once at onboarding
   (`project init --dub-voice-gender`, default male; captured by the `/new-video` skill). The
   **source speaker's** own gender is a separate human observation recorded at LANGUAGE_ID
   (`langid set --source-voice-gender`, default male; `state.source_voice_gender`); a **female**
   source is the only trigger for asking whether dubs should also go female — otherwise dubs stay
   male. Cloning **intent** captured at init (`project init --voice-clone-requested` →
   `dubbing.voice_clone_requested`) is advisory only; it never flips the rights record, and actual
   `voice_clone_consent` is still recorded solely at the human-only `rights-check` gate. `doctor`
   lists which gendered voices are staged (`dub-voice:<lang>:<gender>`).

   **Authorized override (2026-08-07) — cloning ON + rights pre-recorded at init.** By explicit
   human authorization, company policy now **defaults voice cloning ON** and auto-records the
   rights posture at project init, so the token-thrifty scripted/manual route runs gate-to-gate
   with less friction. The company `rights` block in `company.default.json`
   (`default_voice_clone: true`, `auto_consent_at_init: true`, `default_rights_status:
   "self-authored"`, `default_reviewer: "company-standing-authorization"`) drives it: `project
   init` resolves `dubbing.voice_clone` from `default_voice_clone` and, when clone is on and
   `auto_consent_at_init` is set, records `voice_clone_consent: true` + `rights_status` **through
   the sanctioned `rights.set_rights` CLI path** (rule 1 — never a hand-written record; a
   `RIGHTS_SET` event is appended). This deliberately relaxes this rule's neutral default; it is
   **not a silent bypass** — the `/new-video` interview offers a one-click **Neutral voice**
   opt-out (`project init --no-clone`, which records no consent and leaves the rule-5 neutral path
   intact), the whole behavior is opt-out company-wide via `default_voice_clone: false`, and a
   human can still revise at the human-only `rights-check` gate (`set_rights` overwrites, so a
   later gate decision fully supersedes the init-written record). The three
   `disable-model-invocation: true` outward-facing skills (`rights-check`,
   `video-release-authorize`, `video-promote-approve`) keep their human-only posture regardless —
   init pre-populating the record does not touch them. Because the init consent is real, the
   old "clone intent never flips the rights record" caveat above applies only to the `--no-clone`
   / explicit-intent-flag path; the *default* path does record consent at init.
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
    **The catalog `next_command` is convenience, not a bypass.** `catalog list` / `catalog show`
    surface a derived, copy-paste/`!`-runnable `next_command` (and, at a gate step, a `review_files`
    array of the artifacts under review) computed fresh on read from `state.plan()` — never
    persisted. It is a power-user shortcut for a human who wants to run the command themselves; at
    a `STOP_AT_GATE` step it emits the approve command **with a `disclose+confirm first` reminder**.
    It does NOT relax this rule: when *you* drive a gate in-conversation you still do
    surface→options→disclose→confirm→execute, and the three outward-facing gates stay human-run.

14. **Constant audio speed — the picture is always re-timed to the audio (mandatory; rule 5 /
    TASK 2).** Dubbed audio is **NEVER time-stretched** to fit a caption slot — the per-cue
    rubber-banding (speeding/slowing each cue) sounded terrible, so it is **removed**. Every cue
    plays at its natural TTS length and the **picture** absorbs 100% of the mismatch via
    freeze/trim. The old `quality_bars.audio.max_time_stretch` / `freeze_stretch_cap` /
    `freeze_frame_enabled` bars are retained only for **reporting/back-compat** and **no longer
    gate audio speed** — freeze-planning is now unconditional: `dub run` **always** writes a
    per-language **freeze plan** (`audio/freeze-plan.<lang>.json`, a registered artifact tracing to
    the dub-wav) and `package mux` **always** rebuilds the picture from a non-empty plan. (An empty
    plan — audio happened to match the slots — takes the plain `-c:v copy` path.) `media.normalize_wav`
    is the per-cue path: format-normalize only, duration identity. `dub import` still produces no plan
    (`freeze_plan_skipped_reason`) — it has no per-cue natural durations. **The plan is computed on a *running gap*, not
    each cue's own overflow** — because `dub run` lays cue audio **back-to-back** (lead silence
    only when the audio is *early*), a long cue's overrun propagates to every following cue until a
    natural pause, so per-cue-own-overflow freezing left the propagated backlog uncancelled (it
    FAILed on real data). The plan holds the picture by `gap_i = rendered_start_ms - (caption_start_ms
    + cum_freeze - cum_trim)` at each cue boundary where the audio is later than the (already
    re-timed) picture. **Two modes, chosen per language by the `quality_bars.audio.
    freeze_trim_languages` array (a company bar, default `[]`; no CLI flag):**
    - **Hold-only (Model B, the default for languages *not* in the list):** only freeze/hold, never
      drop source frames. Where the audio *underruns* the picture in aggregate (after a gap) a
      one-sided residual remains — the picture lags the audio — and is surfaced **honestly** as
      signed `drift_ms`; you reach PASS by accepting it under a raised `per_cue_drift_tolerance_ms`
      (a disclosed, recorded bar relaxation), suitable when the residual is small (≤ a few seconds).
    - **Freeze + trim (Model A, for languages *in* `freeze_trim_languages`):** additionally **trims**
      the picture (skips `-gap_i` ms of source at the cue boundary) where the audio runs earlier
      than the picture, so residual reaches **0 by construction** (`trims[]` + `total_trim_ms` in the
      plan; `trim_planned`/`trim_ms` per cue in the sync report). Use this when hold-only would leave
      an unacceptably large lag (e.g. an 11.6s ar residual on the al-'Asr project → `ar` is in the
      list). Trimming drops real source frames on cue boundaries — an accepted trade vs a large
      desync.
    In `_build_sync_report` the post-adjust residual is `rendered_start - (caption_start + cum_freeze
    - cum_trim)`; a freeze/trim-planned cue's `drift_ms` becomes 0 (Model A) or the honest one-sided
    residual (Model B) and drops out of the over-cap list **by construction** — so
    `analyze_sync`'s unchanged blocker/major checks read truthful post-adjust numbers and the track
    reaches **PASS honestly** (Model A) or PASS-under-raised-tolerance (Model B). Each freeze is an informational `audio-freeze-planned` note;
    a hold beyond `max_freeze_ms_per_cue` (default 4000ms) escalates to an `audio-freeze-excessive`
    **major** so a person sees a frame that would linger. At `package mux` the picture is rebuilt on
    the post-freeze timeline (source slices interleaved with frozen-frame inserts, re-encoded
    H.264/AAC) and **both** the embedded soft-subs and the standalone `captions.<lang>.vtt/.srt`
    deliverables are re-timed onto that timeline — the **canonical approved caption doc is never
    edited** (rules 6/12); retimed subs are derived mux/package outputs. **Scope limit:**
    freeze-planning is the **real-TTS `dub run` path only**; `dub import` has no per-cue natural
    durations, so it produces no freeze plan (its result carries a `freeze_plan_skipped_reason`).
    Freezes and trims always land on cue boundaries (`at_ms` == the corrected cue's
    `caption_start_ms`), never mid-cue. The earlier band-aid overrides (`max_time_stretch`,
    `cumulative_drift_ceiling_ms`) are dead as speed gates — freeze-frame is the only mechanism.

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
    **Which tracks join the fan-out.** `en` (the human-reviewed track) **is included** as a
    fan-out agent on `@bedrock-eus2/us.anthropic.claude-opus-4-8` / `high` (its render is
    high-stakes and it's the one that later stops at the human gate); it still **STOPS at the
    per-language `translation_qa` human gate** after import — the fan-out produces the draft, it
    never crosses the gate (rules 2/13 hold). Every `auto_translate` target joins too
    (Opus/high for editorial-religious/political sources like tafsir/sermons/political speech,
    else Sonnet/high for ordinary volume; no human gate). The `skip_translation` source track
    never joins (verbatim, no worksheet).
    **Canonical pattern.** (a) `translate export` every worksheet first so cue ids/timing exist;
    (b) build one compact shared **pivot** the agents render from — for a tafsir/Quran video that
    is `{cue: {source, en_gloss, en_with_verses}}` so every language renders the same meaning +
    the same canonical verse set (Arabic script + translation only, no transliteration in either
    captions or audio for any language except `ar`'s own narration + numeric surah:ayah citation,
    rule 11 / the verse-and-notes policy), written to a scratch path; (c) `parallel(langs.map(...))` with one
    `agent()` per language, each given a `schema` that forces a validated
    `{cues:[{id,target_text}]}` return — the agent reads the pivot, renders **all** cues, returns
    the map; (d) back in the main thread, merge each returned map into
    `captions/<iso>.worksheet.json` by cue id with a throwaway merge-by-id helper (scaffolding,
    deleted after), then `translate import` each track through the normal CLI chokepoint. Timing
    is the contract: never change `id`/`start_ms`/`end_ms`/`source_text`, never merge/split cues.
    The worksheet is a fill-in artifact the fan-out only *fills*; the CLI still *imports* it
    (rule 1), auto-split (rule 12) and the deterministic `translation-qa`+`glossary` QA still run
    unchanged. This is the standing procedure in the `create-closed-captions` translation stage.

    **Standalone helper verbs (no project needed):**
    - `vid_cli.py size w 1042` / `size h 583` — from one axis, compute the full 16:9 (or
      `--aspect W:H`) frame, even-rounded for H.264 (e.g. `1042x586`). Sizes the still-image canvas
      and is a general operator helper. Pure math, no I/O.
    - `vid_cli.py speed 1.25 <any.mp4> [--out <path>]` — uniformly re-time **any** video (audio+video
      together), **source untouched**, output beside the source as `<stem>_<factor><suffix>` (e.g.
      `my-file_1.25.mp4`) when `--out` is omitted. Works on files this tool didn't produce; refuses
      to overwrite the source.
    - `vid_cli.py cmd <video-id-or-url>` / `vid_cli.py nextcmd <project-id>` — **read-only command
      printers**: print (never execute) the copy-paste-ready terminal block for onboarding/ingesting
      a video/playlist (`cmd`) or a project's current next step (`nextcmd`). Where a step maps to both
      a real external tool *and* a CLI verb, both are emitted as labelled options, each with its own
      `cd` + the exact env preamble that flavor needs (raw `yt-dlp` → `export VIDTRANS_FETCH_ENABLED=1`
      only; `ingest run` → full `source .env.local`; playlist enumeration → none, rule 3); a pure state
      op falls back to just the CLI verb. At a `STOP_AT_GATE` step `nextcmd` prints only the human-gate
      explanation + the approve line **as reference** (never runnable — rule 13). Bare-terminal form by
      default; `--for-claude` re-adds `!` on runnable lines only (never on a gate reference line). It is
      a **view** over state like the catalog `next_command` — template-driven off the same argv builders
      the real downloader uses so the printed and executed commands can't drift; it never downloads or
      mutates anything (rule 1).

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

To **turn on dubbing for a language that's already translated** ("add es dubbing for <id>"),
use `project enable-dub <id> --targets <codes>` — it flips `dub_enabled` on the existing track(s)
and reuses their captions (a non-clone dub has no source-video dependency); no re-translate, no
re-init. `--disable` is the inverse (turn a dub back into captions-only; refuses to strand a
produced `dub-wav` artifact unless `--force`, rule 6). If the track doesn't exist yet,
`add-languages` first. A `--clone` dub or `package mux` that finds the `source/` media deleted will
re-fetch it via `ingest ensure` (flag-gated).

To **re-render one dub track after it has advanced past the audio gate** (e.g. a freeze/
trim-plan fix on just `ar` when en/ur/zh are already correct and approved), use `project redub
<id> --targets <codes>` — *not* `project reset` (which wipes ALL downstream work for every
track). It pulls the top `current_state` **backward** to `AUDIO_SYNC_ADJUST` (a dub-allowed
state) only if the project had advanced past it, resets **only** the named dubbed track(s) to
the dubbing stage, and leaves every other track + all artifacts + all approvals untouched. It
refuses captions-only tracks (→ `enable-dub` first) and never pushes state forward. After it
runs, re-`dub run` the named language (a fresh `dub-wav@<lang>` supersedes the old one; rule 6
auto-invalidates the stale `audio_qa` approval bound to the superseded hash), `dub qa`, then
re-surface the per-language `audio_qa` gate (rule 13). Logs `TRACK_REDUB_REQUESTED`.

**Running `dub run` for several languages — one ffmpeg-heavy job at a time; NO network env.**
`dub run` is **per-language** (`--language <iso>`); when multiple languages need dubbing (redub en
+ first-dub es/ru, etc.) run them **one command at a time**, each fully returning before the next.
Don't run two `dub run`s (or a dub + a `package mux`) **concurrently**, and don't chain them in an
overlapping shell loop. **Root cause (verified 2026-08-07):** the dub's final concat used to open
one ffmpeg input handle **per cue** (400+ on a long speech) via an N-way `filter_complex`; under
concurrent ffmpeg load that exhausted process/FD resources and ffmpeg died emitting only its
version banner (`rc 232`), surfaced as a misleading `ffmpeg concat failed (232)`. Synthesis,
tempo-fit, and concat each pass in isolation — contention, not data. **Fixed in code:**
`media.concat_wavs` now uses ffmpeg's **concat demuxer** (single `-i playlist.txt`, one handle
regardless of cue count), so a single long dub is robust; and every ffmpeg error in `media.py` now
shows the stderr *tail* (the real error) instead of the `[:400]` banner head. Still run one
ffmpeg-heavy job at a time (two concurrent can contend regardless). A **non-clone** `dub run` needs
**no env** — no `source .env.local`, `VIDTRANS_FETCH_ENABLED`, or CA bundle (it's local piper TTS +
ffmpeg over the already-built captions); the fetch flag + CA bundle are only for media downloads
(`ingest run`/`ensure`, a `--clone` dub, or a `package mux` that re-fetches deleted source, rule 3).
`dub qa` takes **no** `--language` — run it once; it QAs all dubbed tracks together.

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
