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
3. **Init**: `vid_cli.py project init <id> --url <url> --targets <t1,t2,…> --audio <d1,d2,…>`.
   - The two-axis review scope is applied automatically at track creation: `en` (and the source
     language, once confirmed) stay human-reviewed; every other target is marked
     `auto_translate` (AI-translated, deterministic QA only, no human gate). You do not set this
     by hand.
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
- This skill only starts projects — it never touches a human gate. Rights stay `unreviewed`
  until a human sets them; nothing here publishes.

## Completion contract
Single video: the project exists with the confirmed translate/dub sets, and (flag permitting)
rests at `LANGUAGE_ID` with source media cataloged. Playlist: every video is indexed under the
playlist with a derived `next_command`; no media was downloaded and no project was initialized.
