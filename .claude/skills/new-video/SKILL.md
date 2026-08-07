---
name: new-video
description: >-
  Start a new translation project from a video URL (or add a whole playlist): confirm the
  translate-language set and the dub subset with the operator via two quick checklists, then
  init the project and kick off ingest. A playlist URL routes to catalog add-playlist (index
  each video, flag-free enumeration) instead. Use to onboard a new source video or playlist.
allowed-tools: Bash, Read, AskUserQuestion
argument-hint: <url> [video-id]
user-invocable: true
disable-model-invocation: false
model: "@bedrock-eus1/us.anthropic.claude-sonnet-5"
effort: medium
---

# new-video

## Goal
Take a video URL from the operator, confirm **which languages to translate** and **which of
those to dub** (two-axis scope), then `project init` + `ingest run` so the project lands at
`LANGUAGE_ID` ready for the pipeline. If the URL is a **playlist**, index every video under it
instead (metadata-only, no per-video kickoff yet).

## Args
- `$1` — the source URL (a single video or a playlist).
- `$2` (optional) — an explicit `video-id` (e.g. `yt-<id>`). If omitted, derive `yt-<id>` from
  the URL's video id (YouTube ids are case-sensitive — do NOT lowercase).

## Decide the route first
- **Playlist URL** (`list=…`, `/playlist`, a channel's "Videos" tab, etc.) → this is the
  **add-playlist** path. Run `vid_cli.py catalog add-playlist <url>` (enumeration is the
  sanctioned flag-free carve-out — metadata only, no media). Then show each resulting entry's
  derived `next_command` from `vid_cli.py catalog playlist <playlist-id>` so the operator can
  kick off individual videos later. **Do not** init projects or download media here.
- **Single video URL** → the language-selection + init flow below.

## Procedure (single video)
1. **Translate set** — ask via `AskUserQuestion` (multiSelect) which languages to translate to.
   **Pre-select ALL** company languages by default (currently `en, ur, ar, fa, zh, fr, es, pt, ru`
   — read `.claude/config/company.default.json → defaults.target_languages` for the live value)
   and let the operator trim/extend it. Always keep `en` (the human-reviewed hero track) unless the
   operator explicitly drops it.
2. **Dub subset** — ask (multiSelect) which of the chosen translate languages should also be
   **dubbed** (audio). Dub ⊆ translate. **Pre-select `en, ar, zh, ur`** by default — **but never
   the source language** (the source track is transcript/caption-only and is never dubbed, even if
   it happens to be one of en/ar/zh/ur; drop it from the default selection once `source_language`
   is known). A language translated but not dubbed produces captions only.
2.5. **Init interview — front-load the decisions** so the scripted/manual route can run
   gate-to-gate with less friction. Ask each as a quick `AskUserQuestion` (skip the two dub-voice
   questions if the dub subset is empty):
   - **Dub voice gender** (single-select) — **Male** *(Recommended, default)* / **Female**. One
     choice for the whole project; **male is the hard company default** for every dubbed language
     (the voice is auto-selected from the company `dubbing.voices[lang][gender]` registry — you
     never hand-pass `--model`). The source speaker's own gender is captured separately later at
     LANGUAGE_ID (`detect-language`); a female source is where the operator decides whether to
     match with female dubs.
   - **Voice cloning** (single-select) — **Clone the source speaker** *(Recommended, company
     default)* / **Neutral voice (opt out)**. Cloning is the **authorized company default** (a
     deliberate, human-approved override of rule 5's neutral default): when chosen, `project init`
     auto-records `voice_clone_consent: true` + `rights_status` in the rights record under standing
     company authorization, so `--clone` dubbing is unblocked and `PACKAGE → READY_FOR_REVIEW` is
     pre-cleared. Choosing **Neutral** passes `--no-clone`, restoring the rule-5 neutral default for
     this project (no consent recorded, cloning blocked). A human can still revise later at the
     human-only `rights-check` gate.
   - **Rights status** (single-select, default **self-authored**) — **self-authored** *(Recommended,
     default)* / **licensed** / **fair-use-claimed** / **do-not-distribute**. Recorded at init
     (only when cloning is on — the auto-consent path writes it). The first three are distributable;
     `do-not-distribute` keeps `READY_FOR_REVIEW` blocked. (There is no CLI flag for this yet —
     `init` uses the company `default_rights_status`; if the operator picks a non-default value here,
     record it immediately after init with `vid_cli.py rights set <id> --status <s> --reviewer
     "<operator>" --voice-clone-consent` per rule 13 disclose+confirm.)
   - **Mux subtitle mode** (single-select) — **soft-subs** *(default — captions as a toggleable
     track, picture copied bit-for-bit)* / **burned-in** *(captions permanently painted into the
     video, re-encoded)* / **no-subs** *(clean video; captions only as sidecar .vtt/.srt files)*.
     Passed as `--mux-mode`; used by `package mux` unless a per-run `--mode` overrides.
   - **Glossary** (single-select) — run `vid_cli.py glossary list` and build one option per returned
     glossary (label = its `name`/`id`, description = `<terms>` term count) **plus a `None` option**
     *(default — no glossary)*. A glossary is a per-channel/speaker terminology file that pins how
     recurring source terms (names, honorifics, technical/Quranic terms) are rendered per language.
     Pass `--glossary <id>` for the chosen one; omit it for None. If the list is empty, skip the
     question (None implicitly).
   - **Still image per language** (multiSelect over the **dub** set) — ask which dubbed languages
     should show one fixed image for the whole runtime (dub over a static frame) instead of the
     source video; leave unselected to keep the source video. For each selected language, collect an
     image path (free text; must exist, `.jpg/.jpeg/.png/.webp/.bmp`). Build `--images
     en=/p/a.jpg,ur=/p/b.png`. Skip entirely if none. (Only meaningful for dub-enabled languages —
     there must be a dub to lay over the image. Tip: `vid_cli.py size w <px>` gives the 16:9 canvas
     to size an image to.)
   - **Playback speed** (free text / single-select, default **1x**) — "default 1x; e.g. '1.25x for
     en/ur'". A deliberate uniform whole-video speedup (audio+video together, stays in sync) applied
     at mux — the fix for a slow source that drags in en/ur. Parse the natural-language answer to
     `--speeds en=1.25,ur=1.25` (only languages that change; omit the flag entirely for all-1x).
3. **Init**: `vid_cli.py project init <id> --url <url> --targets <t1,t2,…> --audio <d1,d2,…>
   --dub-voice-gender <male|female> --mux-mode <mode> [--glossary <id>] [--no-clone]
   [--images <lang=path,…>] [--speeds <lang=factor,…>]`.
   - Pass `--no-clone` iff the operator chose **Neutral voice** in step 2.5; otherwise cloning is on
     by company default and consent + rights are auto-recorded at init (report this to the operator).
   - Omit `--dub-voice-gender` to accept the male default. Omit `--glossary` for no glossary.
   - Omit `--images` / `--speeds` when no language uses a still image / a non-1x speed. Both are
     per-language `lang=value` maps; a language absent keeps the default (source video / 1.0x). They
     can also be set/changed later with `project set-image` / `project set-speed`.
   - The two-axis review scope is applied automatically at track creation: `en` (and the source
     language, once confirmed) stay human-reviewed; every other target is marked
     `auto_translate` (AI-translated, deterministic QA only, no human gate). You do not set this
     by hand.
   - The chosen gender lands in `project.yaml dubbing.voice_gender`; `dub run` later auto-selects the
     matching gendered voice and **fails loudly** if that gender isn't staged for a language (never a
     silent wrong-gender dub).
4. **Ingest** (media download — flag-gated): only if `VIDTRANS_FETCH_ENABLED=1` is set for the
   session, run `vid_cli.py ingest run <id>` to download + extract + catalog and advance to
   `LANGUAGE_ID`. If the flag is unset, stop after init and tell the operator to set it (or run
   `! vid_cli.py ingest run <id>` themselves once set).
5. Report the project id, the translate/dub sets, and the resulting state.

## Natural-language capture (routing, no scripted parser)
Operators phrase onboarding in prose; map it to the sanctioned verbs:
- "translate/onboard this video <url>" → this skill (single-video flow).
- "add this playlist <url>" / "onboard the whole playlist" → `catalog add-playlist`.
- "add <lang> for <id>" (widen languages) → `vid_cli.py project add-languages <id> --targets <lang>`.
- "add <lang> dubbing for <id>" (existing track, just turn on audio) →
  `vid_cli.py project enable-dub <id> --targets <lang>` (creates nothing; reuses that track's
  captions). If the track doesn't exist yet, `add-languages` first.

## Stop / Escalate
- Never download media (single video ingest) without `VIDTRANS_FETCH_ENABLED=1`. Playlist
  **enumeration** is exempt (metadata only); per-video **ingest** is not.
- This skill only starts projects — it never touches a QA/release human gate. Rights are
  **pre-recorded at init per company policy** (cloning on → `self-authored` + consent auto-recorded
  via the sanctioned `rights set` path); the operator opted into this by leaving cloning on, and a
  human can still revise at the human-only `rights-check` gate. Nothing here publishes. The three
  outward-facing gates (`rights-check`, `release_authorization`, `promotion_review`) stay human-run.

## Completion contract
Single video: the project exists with the confirmed translate/dub sets, dub voice gender (default
male), mux mode, and glossary; when cloning is on (default) the rights record carries consent +
the chosen `rights_status`; and (flag permitting) the project rests at `LANGUAGE_ID` with source
media cataloged. Playlist: every video is indexed under the playlist with a derived `next_command`;
no media was downloaded and no project was initialized.
