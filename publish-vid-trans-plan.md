# Video Translation House — Architecture & Implementation Plan

A repository-native, file-based **video-translation production company** for Claude Code, modeled on the same operating pattern as `publish-book` project at `/Users/qaiser.abbas/Dev/my-repos/publish-book` 

Working name: **`publish-vid-trans`**

## Models:

specialist Sonnet 4.6 / Opus 4.8 agents, deterministic state, hash-verified artifacts, human-bound approvals, and no external publishing without explicit human authorization.

Add specific anthropic model where appropriate. Only use the following models

- `@bedrock-eus2/us.anthropic.claude-opus-4-8`
- `@bedrock-eus1/us.anthropic.claude-sonnet-5`
- `@bedrock-eus1/us.anthropic.claude-haiku-4-5-20251001-v1:0`

Suggest both effort (reasoning depth) and model (capability class) where applicable, they are separate axes — e.g. proofreader is xhigh effort but a bounded comparison task, so Sonnet, not Opus.

## About `publish-book` project

This repo https://github.com/PPCCo/publish-book is a file-based book-production company for Claude Code. The framework coordinates specialist Claude Opus 4.8 agents to research, plan, write, review, illustrate, design, validate, package, and maintain books any database, website, or Claude Agent SDK.

It generates audio and video books by using different tools.

## About ths project

I would like to create a similar framework that uses Claude Sonnet 4.6 and Claude Opus 4.8 agents to translate Persian, Arabic and Urdu Videos speeches into English Audio and Closed Captions in multiple languages in an automated and autonomous way.

---

## 1. Operating model (mirrors `publish-book`)

- `.claude/` holds company policy: agents, skills, workflows, hooks, standards, JSON schemas, templates, and deterministic Python scripts. No business logic lives only inside a prompt — every irreversible or quality-gating decision is backed by a script the agent calls, the same way `ph_cli.py` backs `publish-book`.
- `projects/<video-id>/` is one self-contained translation project (one source video → all target-language deliverables).
- `catalog/videos.json` is the discovery index (all downloaded videos + metadata + language field), populated by the download skill. It is not a source of truth for transcript/translation content.
- Each project has `state.json` (current lifecycle stage), `events.ndjson` (append-only event log), and `artifacts/manifest.json` (SHA-256 hash of every transcript, caption file, audio file, and final video, so nothing downstream can silently drift from what was reviewed).
- Human approvals bind an approver identity to an exact artifact hash and a scope ("approved Farsi→English translation for chapters 1–4"), not to "the file" in the abstract.
- **External publication/upload is out of scope for the default framework** — the pipeline produces upload-ready packages (captioned MP4 + SRT/VTT bundle + audio-dubbed MP4) but does not push to YouTube, social platforms, or anywhere else without a separate, explicitly authorized extension. This matters a lot here given the source material is downloaded from YouTube (see §7 on rights).

## 2. Repository layout

```
publish-vid-trans/
├── .claude/
│   ├── agents/              # specialist agent definitions
│   ├── skills/               # /commands, one per stage
│   ├── workflows/            # the autonomous orchestration graph
│   ├── hooks/                # pre/post stage hooks (validation, cost guards)
│   ├── standards/            # quality bars, style guides per language
│   ├── schemas/               # JSON Schemas for every file type below
│   ├── scripts/               # th_cli.py + supporting deterministic tools
│   └── config/
│       ├── company.default.json   # target languages, voice map, quality bars
│       └── tools.local.json       # local model paths, API keys (gitignored)
├── catalog/
│   └── videos.json           # index of all downloaded source videos
├── projects/<video-id>/
│   ├── project.yaml           # source URL, channel, requested target langs
│   ├── state.json
│   ├── events.ndjson
│   ├── source/
│   │   ├── video.mp4
│   │   ├── audio.wav          # extracted, normalized source track
│   │   └── metadata.json      # title, duration, uploader, detected language
│   ├── transcript/
│   │   ├── source.<lang>.json # word-level timestamped source transcript
│   │   └── qa-report.json
│   ├── captions/
│   │   ├── en.srt / en.vtt
│   │   ├── ar.srt / ar.vtt ...
│   │   └── translation-qa/<lang>.json
│   ├── audio/
│   │   ├── en/dub.wav
│   │   ├── <lang>/dub.wav
│   │   └── sync-report.json
│   ├── video/
│   │   └── <lang>-final.mp4
│   ├── reviews/
│   ├── artifacts/manifest.json
│   └── outputs/
└── tests/
```

## 3. Canonical lifecycle

```
INGEST                     # URL list -> download -> catalog entry
LANGUAGE_ID                # confirm/detect source language
TRANSCRIPTION               # source-language ASR, word-level timestamps
TRANSCRIPT_QA_GATE          # human or high-confidence auto gate
TRANSLATION                 # per target language
TRANSLATION_QA_GATE
CAPTION_TIMING              # sentence-level SRT/VTT with synced timestamps
CAPTION_VALIDATION          # format + reading-speed + line-length checks
DUBBING                     # captions -> TTS audio, per language
AUDIO_SYNC_ADJUST           # time-stretch / re-segment to match video
AUDIO_QA_GATE
VIDEO_MUX                   # merge audio + video + burned/soft captions
FINAL_QA_GATE
PACKAGE                     # bundle outputs per language
READY_FOR_REVIEW            # terminal state; no auto-publish
```

The state transitions are enforced by a deterministic script (`th_cli.py project transition`), the same way `publish-book`'s state script controls its lifecycle. Agents may recommend a transition; only the script can execute it, and gate stages require a human-bound approval record.

## 4. Skills (the four you named, plus supporting ones)

### 4.1 `download-videos` skill

Input: a list of YouTube URLs (file or inline).
Steps:

1. Call `yt-dlp` (see §5.1) to fetch best-quality video+audio, plus all available metadata and any YouTube-provided caption tracks (`--write-auto-sub --write-sub --sub-langs all`).
2. Extract a normalized mono 16kHz WAV alongside the original for ASR (`ffmpeg -i in.mp4 -ac 1 -ar 16000 audio.wav`).
3. Run a lightweight language-ID pass (see §5.3) on ~60s of extracted audio to prefill the `language` field.
4. Emit/append to `catalog/videos.json`:

```json
{
  "video_id": "yt-abc123",
  "url": "https://youtube.com/watch?v=...",
  "title": "...",
  "channel": "...",
  "duration_seconds": 1834,
  "upload_date": "2026-05-01",
  "language": "fa", // ISO 639-1; null + "language_confidence": null if undetected
  "language_confidence": 0.87,
  "language_source": "auto-detect | youtube-metadata | manual",
  "has_youtube_captions": ["fa"],
  "downloaded_at": "...",
  "local_paths": { "video": "...", "audio": "..." },
  "rights_status": "unreviewed" // see §7
}
```

5. Never overwrite an existing catalog entry silently — new runs merge and log a diff event.

### 4.2 `create-closed-captions` skill

1. If `language` is unset or low-confidence, run/confirm language ID first (own sub-skill, `detect-language`) — this can be a human-fillable gate rather than blocking automation, per your requirement.
2. **Transcribe** the source audio in its original language with word-level timestamps (§5.4).
3. Run a **transcript QA pass**: confidence-score flags, silence/music segment detection, profanity/sensitive-term flags relevant to religious or political speech (important given the likely subject matter — Islamic lectures, Quranic recitation, etc. — where mistranscription of terminology is high-stakes).
4. Segment the transcript into **sentence-level cues** with timestamps (not just word timestamps) — this is the unit captions and dubbing both key off.
5. **Translate** each cue into every configured target language (§5.5), preserving the sentence-to-timestamp mapping.
6. Emit SRT **and** VTT per language, plus a machine-readable `captions.<lang>.json` (cue id, start, end, source text, translated text, translator confidence) that downstream skills consume — the SRT/VTT are a rendering of this JSON, not the source of truth.
7. Run **caption validators** (line length, cues-per-second reading speed, min/max duration) before marking the stage complete.

### 4.3 `closed-captions-to-audio` skill

1. For each target language with dubbing enabled, walk the `captions.<lang>.json` cues in order.
2. Synthesize speech per cue with the configured TTS engine (§5.6).
3. **Fit each synthesized clip to its cue's timestamp window**: if generated audio runs longer than `end - start`, either (a) time-stretch losslessly with a phase-vocoder (rubberband/ffmpeg `atempo`, capped at ~1.15–1.3x to avoid unnatural pitch/pace), or (b) flag the cue for re-segmentation/translation trim if stretch would exceed the quality cap — this is a real tension, not a solved problem; see §6.
4. Concatenate cue audio into one continuous per-language track with silence padding to preserve absolute timing against the source video.
5. Loudness-normalize the full track (EBU R128 / -16 LUFS integrated, matching common dub/podcast standards).
6. Emit `sync-report.json`: per-cue drift in ms, stretch factor applied, and any cues that exceeded tolerance and need human attention.

### 4.4 `merge-audio-video-channels` skill

1. Given `video.mp4`, a dubbed audio track, and (optionally) burned-in or soft-muxed captions, invoke `ffmpeg` to produce the final deliverable.
2. Two output modes per language: (a) soft captions + original or dubbed audio as a selectable track (best for accessibility, keeps file small, lets viewer choose), (b) burned-in captions for platforms that don't reliably render soft subs.
3. Verify output with `ffprobe`: stream count, audio/video duration match within tolerance, no dropped frames.
4. Register the output in `artifacts/manifest.json` with its SHA-256 and the source artifact hashes it was built from (video hash + audio hash + caption hash) — full provenance chain, same principle as `publish-book`'s artifact registry.

### 4.5 Supporting skills (not explicitly requested but needed for "highest possible quality")

- `detect-language` — isolated so it can be re-run or manually overridden.
- `qa-transcript` — WER-style self-consistency check (re-transcribe a sample with a second model/pass, diff).
- `qa-translation` — back-translation + terminology-glossary check.
- `qa-caption-sync` — reading-speed and duration validators.
- `qa-audio-sync` — drift measurement between dub track and video timeline.
- `rights-check` — records source license/ToS posture per video (see §7); this is a human-reviewed gate, not an auto-pass.

## 5. Tools, libraries, and models

### 5.1 Download — **yt-dlp**

Confirmed as of mid-2026 still the standard, actively-maintained, open-source choice — a fork of `youtube-dl` with faster updates and better handling of YouTube's frequent delivery changes; the terminal/scriptable nature is actually a plus here since this is meant to run headlessly inside a skill, not interactively. Use `--write-info-json` for metadata and `--write-auto-sub` to opportunistically grab any existing YouTube captions as a transcription cross-check.

### 5.2 Audio/video extraction and manipulation — **FFmpeg**

Universal, open source, and already a prerequisite in `publish-book`. Used for extraction, normalization, time-stretching (`atempo`, or `rubberband` via the `librubberband` filter for higher-quality stretch), muxing, and `ffprobe`-based validation.

### 5.3 Language identification

Two layers, cheapest-first:

- Fast pass: a lightweight LID model (e.g. `fastText` `lid.176`) or Whisper's own built-in language-detection head on a short audio window — cheap, good enough to prefill the catalog field.
- If low-confidence or the video plausibly mixes Persian/Arabic/Urdu (script overlap and loanwords make these three genuinely confusable at the LID level, especially with Quranic Arabic recitation embedded in Persian/Urdu religious speech), fall back to full transcription with a multilingual ASR model and use its own per-segment language tagging as the higher-confidence source.

### 5.4 Transcription (ASR) — **Whisper family, word-level timestamps**

- Recommended: `faster-whisper` (CTranslate2 reimplementation of OpenAI Whisper — same open weights, much lower latency/VRAM) running `large-v3` or `large-v3-turbo`, combined with **WhisperX** for forced alignment, which gives accurate word-level timestamps (Whisper's native timestamps drift, especially on non-English audio) and built-in voice-activity-detection segmentation and speaker diarization.
- All three target languages (Persian/Farsi, Arabic, Urdu) are within Whisper's trained language set, but quality varies with dialect: Modern Standard Arabic transcribes well; heavily dialectal Arabic (Gulf, Egyptian, Levantine) degrades. Persian and Urdu generally perform reasonably but proper-noun and religious-terminology accuracy needs the glossary QA step below.
- Fallback vendor option (generous free tier, if local compute is a bottleneck): Groq's hosted Whisper endpoints or AssemblyAI, both of which have free/low-cost tiers well-suited to a low-volume personal pipeline.

### 5.5 Translation

Two-track approach, since MT and LLM translation have different strengths:

- **Open-source MT baseline**: Meta's **NLLB-200** (No Language Left Behind) — open weights, covers Persian, Arabic, and Urdu well, cheap to run locally, good for a fast first-pass draft or for cost-capping high-volume content.
- **LLM-based translation for quality**: Claude (Sonnet 4.6 for volume, Opus 4.8 for final-pass/high-stakes segments — e.g. religious terminology, direct Quranic quotation, poetry) — better at register, idiom, and context than pure NMT, and can be prompted with a **glossary/terminology file** (transliteration conventions for names, standard English renderings of religious terms, honorifics) to keep consistency across a whole video and across a whole channel's back-catalog.
- Practical pattern: NLLB draft → Claude revises against source + glossary + prior-video terminology memory → automated back-translation spot-check (translate the English back into the source language with a different model and diff against the original for meaning drift) → human spot-check on flagged segments only, not every line.

### 5.6 Text-to-speech (dubbing)

This is the least settled part of the stack and needs a tiered fallback, since no single open-source model cleanly covers all of English + Persian + Arabic + Urdu with production-grade quality and a commercial-friendly license:

- **Coqui XTTS-v2**: broadest multilingual voice-cloning coverage among open models and still widely regarded as the strongest general-purpose open TTS for this kind of multilingual work, but its weights are under the **Coqui Public Model License — non-commercial only**. Fine for personal/research use; a blocker if this pipeline is ever monetized. The original Coqui company shut down, but the model and an actively maintained community fork (`coqui-tts`, `idiap/coqui-ai-TTS`) remain usable.
- **Chatterbox / Chatterbox-Turbo (Resemble AI, MIT license)**: permissive commercial license, multilingual cloning claims, and has reportedly beaten ElevenLabs in blind listening tests — worth evaluating first for the commercially-safe path, but verify current-language coverage against Persian/Urdu specifically before committing (its strongest documented results are on higher-resource languages).
- **Kokoro-82M**: extremely lightweight, permissive Apache-style license, excellent for English, but limited non-English/no-cloning support — good candidate for the **English track specifically**, less so for native-language dubs.
- **Piper**: fully open, CPU-friendly, dozens of languages including Arabic/Persian/Urdu variants, but flatter/less expressive prosody than the neural cloning models above — a reasonable low-cost fallback where naturalness matters less than coverage and cost.
- **Vendor fallback with generous free tier**: ElevenLabs (free tier + pay-as-you-go, strong multilingual quality, easiest integration) or cloud-provider TTS (Google Cloud TTS, Azure Neural TTS) which have free monthly character quotas and solid Arabic/Persian/Urdu voice coverage, useful when open-source quality isn't sufficient for a specific language/register.
- Recommendation: configure per-language, per-project overrides in `company.default.json` (e.g. `en → Kokoro`, `fa/ar/ur → Piper baseline, XTTS or vendor for hero content`) rather than hard-coding one engine.

### 5.7 Caption formats & tooling

- Author `captions.<lang>.json` as the canonical structured artifact; render SRT and WebVTT from it with a small deterministic script rather than hand-writing timing math per format.
- Validate with `webvtt-py`/`pysrt` for structural correctness and a custom checker for CEA-608/WCAG-style readability rules (see §8).

### 5.8 Orchestration

- **Claude Code** as the execution environment, agents/skills/hooks in `.claude/`, exactly mirroring `publish-book`'s pattern: deterministic Python CLI (`th_cli.py`) owns state/validation/artifact logic; agents call it rather than hand-rolling file writes, so an agent can never silently corrupt state.
- **MCP server**: a local, read-mostly `publish-vid-trans` MCP server (same posture as `publish-book`'s) exposing state, validation, and gate tools to any MCP-capable client — no live YouTube-upload or external-publishing tool exposed by default, keeping the "no autonomous external publication" boundary enforceable at the tool-surface level, not just by prompt instruction.
- Network access confined to one sanctioned module (`translate_house/net/fetch.py` equivalent) for `yt-dlp` invocation and any vendor API calls — GET/POST to an explicit allowlist only, credential-scoped, disabled unless an env flag is set, exactly like `publish-book`'s `RESEARCH_FETCH_ENABLED` pattern. This makes "what can reach the network and why" auditable in one place.

## 6. Intricacies and challenges (and their mitigations)

**Timestamp fit between languages of different lengths.** Arabic/Persian/Urdu text is often 15–30% longer or shorter than the equivalent English when spoken at natural pace, so a translated cue frequently doesn't fit its source timestamp window. Mitigation: cap TTS time-stretch at a bounded factor (~1.15–1.3x) to avoid unnatural pitch/pace artifacts; beyond that cap, prefer re-segmenting the cue boundary (borrowing a beat of silence from an adjacent cue) or flagging for a human-reviewed shorter paraphrase, rather than forcing an audibly unnatural stretch. Track this explicitly in `sync-report.json` so it's measurable, not just "hoped for."

**Dialect and register variance.** "Arabic" spans MSA and multiple mutually-less-intelligible dialects; Persian spoken in Iran vs. Dari; Urdu shares heavy vocabulary and script overlap with Hindi and religious Arabic loanwords. ASR and MT quality both degrade on dialectal or code-switched speech. Mitigation: capture a per-video dialect tag (not just language) during language ID; maintain a growing **terminology/glossary file per channel or speaker** so proper nouns, honorifics, and recurring religious/technical terms are translated consistently rather than re-derived per video; route low-confidence dialectal segments to the higher-quality Opus translation pass rather than the fast NLLB draft.

**Religious/technical terminology accuracy.** Given the likely subject matter (Quranic recitation, classical Arabic grammar, religious lectures), mistranslation of specific terms is higher-stakes than generic content — both for accuracy and for respectfulness. Mitigation: a standing glossary (transliteration standard, e.g. one consistent romanization scheme for Arabic/Persian/Urdu terms; canonical English renderings for commonly-used religious vocabulary) that's injected into every translation prompt, plus a dedicated QA pass on any cue containing a flagged glossary term or a direct Quranic quotation, since those have known, checkable correct translations rather than being open to translator discretion.

**Background music, overlapping speech, multiple speakers.** Q&A-format lectures or panel discussions complicate both ASR and caption cue segmentation. Mitigation: WhisperX's built-in VAD + diarization to separate speaker turns before transcription; flag overlapping-speech segments for human review rather than silently picking one speaker's audio. Allow setting background music to null.

**Voice cloning consent/ethics.** If dubbing aims to preserve the original speaker's voice via cloning (XTTS/Chatterbox), that raises consent questions distinct from using a neutral stock voice — this is someone else's likeness. Mitigation: default to **neutral, non-cloned voices** for dubbing unless the source video's own rights holder/uploader has explicitly consented to voice cloning of that speaker; keep this as an explicit config flag per project rather than an implicit default, and never clone a real person's voice for a project without that consent recorded in `rights-check`.

**Cumulative drift across a long video.** Per-cue time-stretching can compound into audible drift over a 30–60 minute lecture even when each individual cue is within tolerance. Mitigation: track cumulative offset in `sync-report.json`, not just per-cue drift, and insert periodic silence-padding "reset points" at natural pauses so drift never compounds past a hard ceiling (e.g. ±500ms).

**Cost and rate limits.** Even with free tiers, translating/dubbing a large back-catalog at scale will hit API limits. Mitigation: local-first defaults (NLLB, faster-whisper, Piper/XTTS) with vendor APIs reserved for a config-flagged "hero content" tier, plus a cost-guard hook (mirroring `publish-book`'s spend boundary) that blocks any external-spend-incurring call without an explicit human-set budget ceiling.

## 7. Rights and legal posture (adapt `publish-book`'s safety boundaries)

The default framework SHALL NOT:

- treat "downloadable via yt-dlp" as equivalent to "cleared for redistribution" — downloading and re-publishing someone else's YouTube video, even translated/dubbed, implicates the original uploader's copyright and YouTube's ToS;
- auto-publish or upload any output to YouTube, social platforms, or any third-party service;
- clone a real speaker's voice without recorded consent (see §6);
- assert that a translated/dubbed derivative is cleared for public distribution without a completed, human-reviewed `rights-check` record per video.

Every project carries a `rights_status` field (`unreviewed → self-authored | licensed | fair-use-claimed | do-not-distribute`) that a human sets explicitly; the pipeline runs happily through `READY_FOR_REVIEW` regardless, since translation/captioning for personal study, archival, or accessibility use is generally on much firmer ground than redistribution — but nothing in `outputs/` should be treated as safe to publish externally until that field is set and a human has looked at it.

## 8. Quality standards, review, and audit

**Transcription:** target WER (word error rate) informally validated via a second-pass re-transcription diff on a sample of segments per video; flag any segment where two independent passes disagree by more than a threshold for human listening.

**Translation:** chrF/COMET-style automated scoring against the back-translation, glossary-term hit-rate (100% of flagged glossary terms must appear correctly, this one is a hard gate not a soft score), and a required human spot-check on any segment scoring below the automated threshold.

**Captions (accessibility standards, not just correctness):**

- ≤ 32–42 characters per line, ≤ 2 lines per cue;
- reading speed capped around 160–180 words/minute (slower for younger or general audiences);
- minimum cue duration ~1 second, maximum ~7 seconds, so viewers can actually read it;
- structural validation against WebVTT/SRT spec plus a CEA-608/708-style style check for anything intended for broadcast-style platforms.

**Audio/dub:**

- loudness normalized to -16 LUFS integrated (a common cross-platform dub/podcast target), true-peak capped to avoid clipping;
- per-cue sync tolerance (e.g. ≤150ms drift) and cumulative-drift ceiling as in §6;
- a required native/fluent-speaker listening spot-check on a sampled percentage of each dubbed track before final approval — automated QA catches structural problems, not naturalness or mispronunciation of names.

**Gate structure (mirrors `publish-book`'s bounded review loops):** three full revision cycles and a handful of targeted repairs per artifact by default; unresolved disagreement or repeated failure escalates to a human rather than looping indefinitely. Final approval requires: zero blocker findings, all glossary-term checks passing, all caption/audio validators green, and a rights-status other than `unreviewed`.

## 9. Suggested `th_cli.py` command surface (mirroring `ph_cli.py`)

```
th_cli.py doctor                                   # env, model weights, ffmpeg/yt-dlp version check
th_cli.py catalog ingest --urls urls.txt            # runs download-videos skill
th_cli.py catalog list / show <video-id>
th_cli.py project init <video-id> --targets en,ar,fa,ur
th_cli.py project status / transition / validate <id>
th_cli.py transcript run <id> [--model large-v3]
th_cli.py transcript qa <id>
th_cli.py translate run <id> --lang <code>
th_cli.py translate qa <id> --lang <code>
th_cli.py captions build <id> --lang <code> --format srt,vtt
th_cli.py captions validate <id> --lang <code>
th_cli.py dub run <id> --lang <code> --engine xtts|piper|elevenlabs
th_cli.py dub sync-check <id> --lang <code>
th_cli.py mux run <id> --lang <code> --mode soft-subs|burned-in
th_cli.py rights check <id> / set <id> --status licensed --evidence <path>
th_cli.py artifact list / register <id>
th_cli.py approval grant <id> --gate <name> --approver <email>
th_cli.py package <id> --lang <code>
th_cli.py event tail <id>
```

## 10. Open decisions worth resolving before build

1. Commercial vs. personal-use intent — determines whether XTTS-v2 (non-commercial license) is usable or you need Chatterbox/Piper/vendor TTS from day one.
2. Whether dubbing should clone the original speaker's voice (needs consent posture, §6) or use a fixed neutral voice per language.
3. Target language list beyond English (the prompt says "multiple languages" for captions but "English is a must" for audio) — worth fixing this explicitly per project rather than per framework default.
4. Local GPU availability — drives the local-model-vs-vendor-API balance throughout §5.

## 11. Language expansion — Mandarin Chinese, French, Spanish, Portuguese, Russian

Adding these five target languages does **not** require a new category of tool — the translation, captioning, and orchestration stack from §5 carries over unchanged. Three adjustments matter:

**Translation:** no change. NLLB-200 and Claude both treat these as high-resource languages; if anything expect _higher_ quality than for Farsi/Arabic/Urdu. Extend the glossary/terminology file to cover proper-noun and technical-term consistency in all five, and keep the back-translation QA pass.

**Dubbing/TTS — correction from earlier guidance and a real improvement.** XTTS-v2's actual 17-language list is: en, es, fr, de, it, pt, pl, tr, ru, nl, cs, ar, zh-cn, ja, hu, ko, hi. That means **Mandarin, French, Spanish, Portuguese, and Russian are all natively covered by XTTS-v2 with voice cloning**, while **Persian and Urdu are not covered by XTTS at all** (only Arabic was, of your original three). Recommended engine assignment:

- `zh, fr, es, pt, ru` → XTTS-v2 (or Chatterbox-Turbo if its per-language quality checks out — worth a side-by-side eval, since it's newer and less proven specifically on Mandarin).
- `fa, ur` → Piper or a vendor engine (ElevenLabs / Azure / Google Cloud TTS), since no strong open cloning model currently covers them well.
- `en` → Kokoro-82M (lightweight, permissive license, strong English quality) or XTTS.

**Captions — one real addition.** Mandarin caption rules differ structurally from your other languages: measure line length in **characters, not words** (~13–16 CJK characters per line vs. 32–42 for alphabetic scripts) and reading speed in **characters/minute, not words/minute**; Chinese also doesn't use spaces, so line-wrap logic needs its own segmenter rather than reusing the whitespace-based wrapper. French/Spanish/Portuguese/Russian fit the existing Latin/Cyrillic caption rules with no changes.

**Infra note:** when burning in captions via ffmpeg, use a font with full CJK + Cyrillic glyph coverage (e.g. Noto Sans CJK + Noto Sans) or Mandarin/Russian burned-in captions will render as missing-glyph boxes.

## 12. Addendum: Distribution — YouTube upload and promotion

**Moved to a separate, separately-authorized extension plan:** see [`publishing-and-promotion-extentsion-plan.md`](publishing-and-promotion-extentsion-plan.md).

The core framework (this document, §1–§11) stops at `READY_FOR_REVIEW` and produces upload-ready packages without touching any external platform. YouTube upload and cross-platform promotion add a different risk class (live OAuth credentials, platform ToS, API quota) and are therefore built as **Phase 6**, only after Phases 0–5 are complete and stable and external writes are explicitly authorized.
