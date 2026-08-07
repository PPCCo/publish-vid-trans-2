# TODO

Add one more framework enhancement. The frame should accept a playlist link, and for a playlist the framework should add all the files in the playlist to the index. I also want the index to have a next appropriate command as a property for human convenience, so that this index containing the playlist videos, each individual video will have the next command property which the user can just copy and paste to start the process. This next command will apply to all the phases so that for example at if a file is at QA gate that specific command for approving the gate would be the next command. In case of the QA gate the files to be reviewed should also be in an array of file relative parts that are to be reviewed. If a file ID within the playlist being added is already processed or is being processed, that file along with all its status will move to the playlist.

## zh, fr, es, pt, ru

can the process/workflow be modified so that even when some of the translation (in this case, ar/en/ur) have already been created, I want to add more translations, in this case, zh, fr, es, pt, ru

Modify/Enhance the `.claude/` framework to behave this way:

- Translation and dubbing will be for all selected languages. The worksheet and things other than translation and dubbing would only be for the source language and English.
- At the start of a new video translation present user with a checkbox list (selectable options for `AskUserQuestion`) to select which language they want for translation and another AskUserQuestion for dubbing. This will allow for example, if the user selects `es` for translation but not for dubbing, the user at a later stage can ask `add es dubbing for "yt-YP0FDR7Wc-8"` and Claude will use the already generated translation to generate the dubbed video, after re-downloading the video only in case the video has been deleted (please that the framework allows for that).

---

Create a python script that I can run outside Claude, that would take a config (with all the needed info) allow my to upload the youtube 

---

## Manual Commands 

- What each mode produces (soft-subs = toggleable caption track + picture copied bit-for-bit; burned-in = captions painted into the video,
re-encodes; no-subs = dub audio only, captions as sidecar files).
- Whether it's per-language — it's a single project-wide setting, so one value applies to all six dubbed tracks; there's no built-in way to give,
say, zh burned-in and en soft-subs without a per-run --mode override at each package mux.
- Your target platform — if you're uploading to YouTube (which keeps soft subs), soft-subs is ideal; if a platform strips them, burned-in guarantees
they show.
- Cost — burned-in re-encodes each language's video (slower, larger); soft-subs copies the picture.

What's on your mind?

❯ I want all languages en/ar/ur/zh/es/ru dubbed but I'd also like soft-subs for all languages, what's the best option for that? 

⏺ For exactly what you want — all six languages dubbed with soft (toggleable) captions on each — soft-subs is the best option. That's precisely what it
does:

- Dub audio for the language is muxed in as the audio track.
- Captions are embedded as a toggleable mov_text subtitle track (viewer can turn on/off).
- Picture is copied bit-for-bit (no re-encode — fast, no quality loss).
- Standalone .vtt/.srt sidecars are still produced alongside.

The other two don't fit: burned-in paints captions permanently into the picture (not toggleable, and re-encodes), and no-subs gives no embedded
captions at all.

One clarification on scope so I set it correctly: mux_mode is a single project-wide value — setting it to soft-subs applies to all six dubbed tracks
(en/ar/ur/zh/es/ru) at once, which is exactly your goal. No per-language config needed.

Shall I set mux_mode: soft-subs on the project now, then give you the en mux command to try?

.venv/bin/python .claude/scripts/vid_cli.py package mux yt-YP0FDR7Wc-8 --language en

---

The reliable template for next time

Sequential, one at a time, no env:
cd /Users/qaiser.abbas/Dev/my-repos/pub/publish-vid-trans
.venv/bin/python .claude/scripts/vid_cli.py dub run <id> --language en   # let it fully finish
.venv/bin/python .claude/scripts/vid_cli.py dub run <id> --language es   # then this
.venv/bin/python .claude/scripts/vid_cli.py dub run <id> --language ru   # then this
.venv/bin/python .claude/scripts/vid_cli.py dub qa  <id>                 # once, aggregate — no --language

---

There are three changes I'd like you to fix:

## TASK 1 - Reduce tokens consumption  ✅ DONE (2026-08-07)

Implemented the token-thrifty scripted/manual route (both options coexist with the unchanged
Claude route): `/process-manual <id>` skill+command, `project autopilot <id> [--mt]
[--until][--dry-run]`, and `run_pipeline.sh <id> --mt` one-command wrapper. Deterministic steps
run with zero Claude tokens; the one non-deterministic step (translation worksheet + English
gloss fill) uses a new opt-in MT engine adapter (`engines/mt.py`, `translate machine-fill`).
Autopilot stops at every human gate and can never grant/cross one. Resume contract: run the
script → it halts at a gate → say "the manual process for <id> is done" → Claude does editorial
QA + gate walk (the only token spend). Docs in CLAUDE.md ("Where to start") + OPERATING-GUIDE.md
§4/§6; project memory `manual-scripted-route`. Pre-existing note: piper needs a staged `en`
voice model (`--model`) — engine-config gap, tracked separately from this feature.

The video generation (transcribe/translate + dubbing) is eating up too many tokens. Since we have fixed most of the scripts that were generating wrong output, is it possible to have parts of the workflow/process done manually, by handoffs, where you pause the workflow, and tell me to run the scripts that can be run outside of ClaudeCode, whether it's the python scripts or tools like whisper/huggingface. And once I have successfully executed the scripts, I will let the ongoing video-processing claude session, and it can continue from there until the next manual step. So ClaudeCode should be used only for the activities where using ClaudeCode is the only option. Keep the current Claude-based workflow intact (as an alternate route), so either create a new command `/process-manual <PROJECT-ID>` or add a `--manual` flag in existing workflow, but the idea is, both options should be possible, either run the whole process using Claude, or doing some parts manually. Ideally, if possible, I'd like the whole transcribe-translate-dub process be done using only scripts, without using Claude. Also, would be best if the whole transcribe-translate-dub process can be done in one go, and at the end, I can tell Claude that the manual process for video <VIDEO-ID> is done, so tha Claude can do the appropriate QA on transcription, translation, and dubbing (unless this would greatly impact the quality or the cost)

## TASK 2 — Male voice is the hard default  ✅ DONE (framework) / ⏳ operator staging (2026-08-07)

Made dub-voice gender a framework concept: a company-owned, gender-tagged registry
`dubbing.voices[<lang>][male|female]` (`company.default.json` shape + gitignored
`company.local.json` abs `.onnx` paths). `dub run` (no `--model`) auto-selects the voice by
`--gender → project dubbing.voice_gender → company default_voice_gender (MALE)` and **fails loudly**
if that gender isn't staged — never a silent wrong-gender dub. Gender captured once at onboarding
(`project init --dub-voice-gender`, default male; `/new-video` asks it) and the **source** speaker's
gender at LANGUAGE_ID (`langid set --source-voice-gender`, default male → `state.source_voice_gender`;
`detect-language` asks it, and a female source prompts "dub female too?"). Cloning **intent**
(`--voice-clone-requested`) is advisory only — real consent stays the `rights-check` gate (rule 5).
`doctor` lists staged voices (`dub-voice:<lang>:<gender>`). Docs: CLAUDE.md rule 5 area +
OPERATING-GUIDE.md §6 "Dub voices & gender"; memory `male-voice-default`.
**Remaining (operator):** stage a **male** en/zh voice, then re-`dub run` en+zh for `yt-YP0FDR7Wc-8`
(project is AT the audio gate → fresh dub-wav supersedes, rule 6 invalidates the stale approval);
Claude then does the `audio_qa` gate walk. zh male on piper is thin — if none, keep zh female as a
disclosed exception.

## TASK 3 - Add es and ru  ✅ DONE (defaults) / ⏳ operator staging (2026-08-07)

es and ru are **already company default** `target_languages` + `audio_languages` (9 langs: en, ur,
ar, fa, zh, fr, es, pt, ru), so "add to default languages" is satisfied. For `yt-YP0FDR7Wc-8` they
exist as caption-only tracks; enabling **dubbing** is `project enable-dub yt-YP0FDR7Wc-8 --targets
es,ru` + `dub run` — part of the operator handoff (needs staged es/ru **male** voices first).

## IMPORTANT

TASK 2 and 3 should be done after TASK 1, so that less tokens are consumed

---
API Error: 412 Portkey Usage Limits Policy 9551434e-12fd-462e-83d7-ee27f5e86a5d Exceeded for value key metadata._user
---



---

project add-languages <id> 

Present the rights decision as selectable options for `AskUserQuestion`

---

Some corrections in what I asked earlier:

1. Playlist ingest — relax the VIDTRANS_FETCH_ENABLED rule for this.
2. `next_command` doesn't need to be natural language. It can be a specific command, so ! command is acceptable here (I believe that would be more efficient). But in all other cases, AskUserQuestion and natural language is preferred.
3. At QA gates, also a `review_files` array (relative paths of the artifacts to review, alongside the `next_command`) where applicable, remove it if `review_files` doesn't apply to the `next_command` being set.
4. Playlist grouping — the index groups videos under their playlist. If a video being added via a playlist is already processed / in progress, that existing video (with its full status) moves under the playlist rather than being duplicated.

Let me also reconcile with the two-axis translate/dub work already in flight (those Explore agents are still running). Let me ask the clarifying
questions now so the whole design is coherent.

---

Modify/Enhance the `.claude/` framework to behave this way:

- While approval via commands is good and should be there, relax the restriction of having the user to "have to" run the command, and in most cases, present the user with AskUserQuestion and natural language with reasonable options (for example, the next task/action) and Recommended option (where applicable). Relax the restrictive rules where needed for this.
- Also, at the approvals at different human gates, add a `next_command` containing the command that the human needs to run next (the ! command where applicable, otherwise, natural language prompt).
- At human gates, also add a `review_files` array (relative paths of the artifacts to review, alongside the `next_command`) where applicable. Set `review_files` to null if no files are applicable to the `next_command` being set.

---

## Front-load consents/options at project init  ✅ DONE (2026-08-07)

Authorized override of rule 5: voice cloning now defaults **ON** company-wide, with
`voice_clone_consent: true` + `rights_status: self-authored` auto-recorded at `project init`
through the sanctioned `rights.set_rights` path (rule 1; appends `RIGHTS_SET`). Opt out per project
with `project init --no-clone` (neutral voice, no consent); revert company-wide with
`company.default.json rights.default_voice_clone:false`. Also front-loaded at init as
`AskUserQuestion`s in `/new-video`: dub gender, voice-cloning (Clone Recommended / Neutral),
rights-status (default self-authored), **mux subtitle mode** (`--mux-mode`
soft-subs/burned-in/no-subs, consumed by `package mux` when no `--mode`), and **glossary**
(`--glossary`, options from the new `vid glossary list` verb + None). The three outward-facing
disable-model-invocation gates stay human-only. Docs: CLAUDE.md rule 5 "Authorized override
(2026-08-07)" + OPERATING-GUIDE.md §3; memory `clone-on-default-and-init-consent`.
