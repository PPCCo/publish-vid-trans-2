# yt-app — Standalone non-Claude A/V CLI — Build Plan & Progress Tracker

**Single source of truth** for the yt-app build. Read this first in any fresh context.
Update the checkboxes and the "Status" column as work lands.

Last updated: 2026-08-16

---

## 0. What this is (one paragraph)

A standalone, non-Claude CLI toolkit under `yt-app/` that runs the same audio/video
transformation chain the framework runs (download → split → transcribe → translate → dub →
mux) **without invoking Claude at all** and **without the state machine / gates / approvals /
event log**. macOS only. It **imports the framework's proven leaf functions in place**
(`.claude/scripts/video_translation_house/{media, engines.*, captions, net.fetch, size,
speed, util}`) and **thin-reimplements** the state-coupled orchestration (`dubbing.run_dub`,
`packaging.run_mux`) as pure functions over plain dicts. One `./cli.py` with per-verb
subcommands + a resumable, parallel batch runner driven by `config/defaults.json` + a video
list.

## 1. Hard requirements (from the user — do not drift)

- **R1** Single-file processing verbs runnable independently outside Claude:
  - split audio/video channels
  - transcribe with **configurable `--max-cue`**
  - translate `my-audio.mp3`-style caption JSON into `en|fa|ar|es` **in parallel**
  - dub a video with a given audio
  - very flexible in/out **format flags** on every verb
- **R2** A helper command: playlist ID/URL → JSON list file that feeds the batch runner.
- **R3** A resumable batch runner `./cli.py`:
  - accepts `--list <list.json>` (array of videos + per-video overrides) **OR** a bare
    YouTube id/url (e.g. `./cli.py J8GzIPwYfeg`).
  - reads `yt-app/config/defaults.json` for defaults applied to all videos unless overridden
    per-video.
  - per-video **`status`** enables **resume-after-interruption**.
  - "do everything with minimal or no human input."
- **R4** Comprehensive user guide at `docs/non-claude-user-guide.md` (many commands/examples,
  low verbosity).
- **R5 (code quality)** Modular, with **reusable functions shared by scripts**. Shared code
  may live in `.claude/scripts/`, but most new code/tests/config/docs live under `yt-app/`.
- **R6 (validation)** `ur:xtts` (and `fa:xtts`) MUST **throw an error** (piper-only langs, no
  XTTS coverage). A lang == `sourceLang` is **ignored** for translation (never self-translate).
  A dub/cc lang **not in `targetLangs`** is **ignored** (with a note).

## 2. Confirmed design decisions (locked — from plan-mode Q&A)

- **Import in place** — `sys.path` insert of `.claude/scripts`; import the leaf modules only.
  Never import state-coupled modules (`ingest/dubbing/transcript/packaging/project/paths/state`).
- **Sidecar `status.json`** per video for resume — independent of framework `state.json`.
- **Self-configuring env + `run.sh`** — set `VIDTRANS_FETCH_ENABLED=1`,
  `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE=~/certs/aipe-certs.pem`,
  `HF_HOME=<repo-parent>/.cache/huggingface`, `HF_HUB_OFFLINE=1` **only when unset-and-present**
  (mirrors `run_pipeline.sh`; never clobbers an operator's explicit env).
- **Full freeze/trim parity** — port the running-gap freeze plan, keep-source-audio splice,
  citation strip, clone-ref head-slice, picture rebuild faithfully.
- **Parallelism across videos** via `parallel_executions` (default 3; `0` = sequential)
  **plus a global dub lock**: `threading.Semaphore(1)` serializes every **entire** dub-render
  **and** every **entire** mux ffmpeg sequence across all videos/langs. Transcribe/translate
  parallelize freely. (User instruction: "don't allow concurrent dubs if possible, otherwise
  remove `parallel_executions` completely" → dub lock is mandatory.)
- **Subcommands of `cli.py`** — one entrypoint, shared arg/config/env bootstrap.
- **No consent gate for cloning** — yt-app is a local operator tool with no rights record;
  clone defaults on for clone-languages, `--no-clone` opt-out. **Deliberate deviation from
  framework rule 5 — document loudly** in the user guide.

## 3. Concurrency model (do not change without a reason)

Threads (`concurrent.futures.ThreadPoolExecutor`) — every unit of work is a blocking
`subprocess.run` (ffmpeg, whisper, piper, xtts client, yt-dlp) which releases the GIL.
Module-global `DUB_LOCK = threading.Semaphore(1)` wraps the **entire** dub-render call and
the **entire** mux call (each a multi-subprocess sequence), never just one `subprocess.run`.
Rationale (verified in framework): concurrent ffmpeg dubs exhaust FDs → ffmpeg dies rc 232,
surfaced as a misleading "concat failed (232)".

## 4. File layout (all under `yt-app/` unless noted)

```
cli.py                      # argparse dispatcher; FIRST line calls lib.env.bootstrap()   [ ]
run.sh                      # env bootstrap + exec <repo>/.venv/bin/python3 cli.py "$@"    [ ]
config/defaults.json        # batch defaults                                              [x]
config/defaults.local.json  # optional gitignored override (deep-merged)                  [ ] (optional)
lib/__init__.py             # package docstring + import-order rule                       [x]
lib/env.py                  # repo-root discovery, sys.path insert, env self-configure    [x]
lib/config.py               # load defaults(+local) via util.deep_merge; company/tools     [x]
lib/langspec.py             # pure language-spec validation (R6 rules)                     [x]
lib/channels.py             # NEW leaf: split-channels via ffmpeg channelsplit/pan         [ ]
lib/downloader.py           # net.fetch.ytdlp_download / ytdlp_playlist_entries wrappers   [ ]
lib/transcriber.py          # extract_wav → asr.transcribe → caption doc (+split_long_cues)[ ]
lib/translator.py           # mt.translate_text per cue, parallel across langs             [ ]
lib/dubber.py               # PORT of dubbing.run_dub inner loop + pure helpers            [ ]
lib/muxer.py                # PORT of packaging._build_retimed_video/run_mux               [ ]
lib/status.py               # status.json schema + atomic read/write (tmp + os.replace)    [ ]
lib/ytpipe.py               # single-video stage sequencer; reads/updates status.json      [ ]
lib/batch.py                # resumable runner: ThreadPoolExecutor + DUB_LOCK              [ ]
projects/list-1.json        # example batch list                                          [x]
tests/                      # pytest, pure-logic only (no ffmpeg/engine subprocess)        [ ]
docs/non-claude-user-guide.md  # at REPO ROOT: docs/non-claude-user-guide.md              [ ]
```

Import-order rule: `lib/env.py::bootstrap()` runs before any `video_translation_house`
import. Every `lib/*.py` does its framework imports **lazily inside functions**.

## 5. Subcommands (each a single-file verb; flexible in/out flags)

| Verb | Signature | Reused function | Status |
|---|---|---|---|
| `split-channels` | `IN [--out-dir D] [--layout mono\|stereo\|all]` | `lib/channels.py` (NEW) | [x] |
| `probe` | `IN` | `media.probe_summary` | [x] |
| `extract-audio` | `IN [--out W.wav] [--sr 16000] [--ch 1]` | `media.extract_wav` | [x] |
| `transcribe` | `AUDIO [--provider] [--model] [--lang] [--max-cue MS] [--no-condition-on-previous-text] [--hallucination-silence-threshold S]` | `asr.transcribe` (+ `captions.split_long_cues`) | [x] |
| `translate` | `SRC.json --from fa --to en,fa,ar,es [--provider] [--parallel N]` | parallel `mt.translate_text` → `captions.<lang>.json` | [x] |
| `dub` | `VIDEO --captions C.json --lang L [--provider] [--gender] [--clone/--no-clone] [--still-image IMG] [--speed F]` | `lib/dubber.py` + `lib/muxer.py` | [x] |
| `mux` | `VIDEO AUDIO.wav [--subs S] [--burn] [--speed F]` | `media.mux_video`/`mux_video_burned_in` (+ `respeed_video`) | [x] |
| `size` | `w\|h N [--aspect 16:9]` | `size.compute_dimensions` | [x] |
| `speed` | `F VIDEO [--out P]` | `speed.respeed` | [x] |
| `playlist` | `URL\|ID [--out list.json]` | `net.fetch.ytdlp_playlist_entries` → batch list JSON | [x] |
| `batch` | `(ID\|URL \| --list list.json)` | `lib/batch.py` | [x] |

## 6. Single-video pipeline (`lib/ytpipe.py`) — stage → reused function

1. **download** — `net.fetch.ytdlp_download(url, source_dir)` (fetch flag set by env.py)
2. **extract_audio** — `media.extract_wav(video, audio.wav, sample_rate=ASR_SAMPLE_RATE, channels=ASR_CHANNELS)`
3. **transcribe** — `asr.transcribe(audio, out_dir, provider, model, language=sourceLang)`;
   assemble caption doc; `captions.split_long_cues(cues, max_ms=max_cue_ms, language=sourceLang)`
4. **translate** (per target ≠ sourceLang, parallel) — `mt.translate_text` per cue →
   `captions.<lang>.json`; source lang → verbatim captions (`target_text == source_text`)
5. **dub** (per dubLang, each under `DUB_LOCK`) — `lib/dubber.py` → `audio/dub.<lang>.wav` +
   `audio/freeze-plan.<lang>.json` (freeze plan skipped for still-image langs)
6. **mux** (per dubLang, each under `DUB_LOCK`) — `lib/muxer.py` → `video/dubbed.<lang>.mp4`

Each stage: set `running` → run → set `done`(+artifact) / `failed`(+error), atomically.

## 7. Freeze/trim dub algorithm (port from `dubbing.py` + `packaging.py`)

Pure helpers to copy verbatim (they live in state-coupled modules but ARE pure):
- `_strip_citations` — `re.sub(r"\s*\(Quran[^)]*\)", "", text)` + collapse whitespace;
  applied only to the **TTS copy**, never the caption.
- `_natural_pause_before(cues, idx, gap_ms=700)`
- `_lead_silence_ms(cues, idx, timeline_ms, is_still_image)`
- `_plan_freezes(cue_measures, bars, trim, source_duration_ms)` — running-gap:
  `gap_i = rendered_start - (caption_start + cum_freeze - cum_trim)`. `gap>0` → freeze;
  `gap<0 & trim` → trim; `gap<0 & hold-only` → clamp (honest residual). Tail reconciliation
  makes picture end == audio end.
- `resolve_dub_voice(root, lang, gender)`, `clone_languages(root)`,
  `company_default_voice_gender(root)` (default **male**).
- `_clone_ref_trim` — head-slice `source/audio.wav` to `tts.xtts_worker.clone_ref_trim_seconds`
  (≈25s), cached by mtime.

Per-cue loop (`sr,ch = media.DUB_SAMPLE_RATE=24000, media.DUB_CHANNELS=1`):
- lead silence via `_lead_silence_ms` → `media.silent_wav`
- `keep-source-audio` flag → `media.slice_wav(source_audio, start/dur)`; else non-empty text →
  `engines.tts.synthesize_cue(strip_citations(text), provider, language, model, voice,
  clone_ref, root, project_id)`; else 1ms silence
- **NEVER time-stretch** — `media.normalize_wav` only; append; track `rendered_start/end` +
  `caption_start/end`
- concat via `media.concat_wavs` (demuxer), then `media.loudnorm(-16 LUFS)`
- still-image lang → `freeze_plan = None`; else `_plan_freezes(..., trim=(lang in
  bars.freeze_trim_languages))`

Voice resolution: `gender` param → project override → company default (**male**); raise loud
if `(lang, gender)` not staged (e.g. `fa` has NO staged piper voice → fails, as framework
does). Skipped when `model` given or `clone=True`. Clone-langs (live from company config,
currently `en,zh,ar,es,ru,fr,pt`) → `provider=xtts, clone=True` off the trimmed source ref.

Picture rebuild (`lib/muxer.py`, port of `packaging._build_retimed_video`/`run_mux`): walk
sorted freeze+trim events → `media.slice_video` spans + `media.freeze_segment` holds (trims
advance cursor without emitting) → `media.concat_videos`; still-image → `media.still_image_video`
then mux dub over it; empty plan → source video as-is. Then `media.mux_video` (soft-subs) /
`media.mux_video_burned_in` (burn), then **`media.respeed_video` LAST** if
`playback_speed[lang] != 1.0`.

## 8. Language-spec validation (`lib/langspec.py`) — fail fast, before any subprocess

- `sourceLang` in `targetLangs` → dropped from effective translate set (verbatim captions
  only; never self-translate).
- `dubLangs`/`closedCaptions` lang not in produced set → **ignored** with a note.
- `per_language_provider[lang]` forcing non-piper for a piper-only lang (`fa`,`ur`) → **hard
  error** (`LangSpecError`).
- clone-lang default: `xtts+clone` unless clone disabled or explicit piper override.
- Read `clone_languages`/voices/`freeze_trim_languages` **live** from company config.
- **Clone resolution precedence (most specific first):** (1) `per_language_provider[lang]
  == "piper"` → no clone; (2) explicit `voice_clone[lang]` (`true`/`false`) overrides both
  the global `clone.enabled_by_default` and the company `clone_languages` default;
  (3) otherwise clone iff `clone.enabled_by_default` **and** lang ∈ company clone set. A
  piper-only lang (`fa`/`ur`) with `voice_clone[lang]=true` **or** a non-piper
  `per_language_provider` is a hard `LangSpecError`.

## 9. Config schemas

**`config/defaults.json`** (implemented, [x]):
```json
{ "schema_version":"1.0","sourceLang":"fa","targetLangs":["en","fa","ar","es"],
  "dubLangs":["en","ar"],"closedCaptions":["en","fa","ar","es"],
  "default_provider":null,"per_language_provider":{"en":"xtts","ar":"xtts","fa":"piper","ur":"piper"},
  "parallel_executions":3,"clone":{"enabled_by_default":true},
  "voice_clone":{"en":true,"zh":true,"ar":true,"fa":false,"ur":false,"fr":true,"es":true,"pt":true,"ru":true},
  "xtts_worker":{"clone_ref_trim_seconds":25,"idle_timeout_seconds":1800},
  "asr":{"provider":"mlx-whisper","model":"mlx-community/whisper-large-v3-turbo","max_cue_ms":7000},
  "mux_mode":"soft-subs","playback_speed":{},"images":{} }
```

**`projects/list-1.json`** (implemented, [x]):
```json
{ "schema_version":"1.0","videos":[
  {"id":"abc123","url":"https://www.youtube.com/watch?v=abc123"},
  {"id":"def456","url":"...","overrides":{"targetLangs":["en","ar"],"dubLangs":["en"]}} ]}
```
Bare id/url → single-video mode using defaults verbatim; `--list` → per-entry `overrides`
deep-merged over defaults (via framework `util.deep_merge`: dict-merge recursive, list/scalar
replace).

**`status.json`** (per video, resume): `stages.{download,extract_audio,transcribe,translate,
dub,mux}` each `{status: pending|running|done|failed, started_at, finished_at, artifact,
error}`; language-scoped stages (translate/dub/mux) carry a `per_language` map (independent
resume per lang). Resume rule: skip a stage only if `done` **and** artifact exists non-empty;
a `running` found at start (crash) → treated as `pending`; `failed`/`pending`/missing-artifact
re-runs. Never delete partial artifacts. Idempotent.

## 10. Tests (`yt-app/tests/`, pytest — pure logic, no ffmpeg/engines)

- `test_langspec.py` — `ur:xtts` & `fa:xtts` throw; piper/unset pass; source-in-targets dropped;
  dub/cc-not-in-targets ignored; clone-lang auto-detect. [x]
- `test_freeze_plan.py` — running-gap freeze; hold-only never trims; trim mode trims on gap<0;
  tail reconciliation; still-image lead-silence uses relative gap only. [x]
- `test_citation_strip.py` — `(Quran s:a)` removed w/o double space; no-citation identity. [x]
- `test_status_resume.py` — done+artifact skips; done+missing re-runs; running→pending re-run;
  per-language independence. [x]
- `test_config_merge.py` — defaults + overrides deep-merge matches `util.deep_merge`. [x]
- `test_batch_concurrency.py` — fake sleepy jobs: dub-lock allows only 1 concurrent dub;
  non-dub jobs up to `parallel_executions`. [x]
- Use a **synthetic** company/tools config fixture (never the gitignored real file).

## 11. Docs — `docs/non-claude-user-guide.md` (repo root)

Comprehensive, example-dense, low-verbosity: every verb w/ sample invocations; the four
example flows (split channels; transcribe w/ `--max-cue`; translate → en|fa|ar|es in parallel;
dub a video w/ given audio); playlist → list JSON → batch; `defaults.json` + list schema
reference; env/`run.sh` note; **LOUD callout** that cloning is un-gated here (`--no-clone` to
opt out) and that `fa` has no staged piper voice. [x] (`docs/non-claude-user-guide.md`)

## 12. Verification checklist (run before declaring done)

- [ ] Unit: `<repo>/.venv/bin/python3 -m pytest yt-app/tests -q` passes (pure-logic).
- [ ] Verbs on a tiny sample: `run.sh probe <file>`, `extract-audio <file>`,
      `split-channels <stereo.mov>`, `size w 1920`, `speed 1.25 <clip.mp4>`.
- [ ] Playlist: `run.sh playlist <url> --out yt-app/projects/list-gen.json` → inspect JSON.
- [ ] Batch resume: run `batch <id>`, interrupt mid-run, re-run → completed stages skip.
- [ ] End-to-end (one short clip, one dub lang): `batch <id>` → `video/dubbed.<lang>.mp4`
      exists, `ffprobe` shows audio+video, dub not time-stretched, picture retimed.
- [ ] Negative: list entry w/ `per_language_provider={"ur":"xtts"}` fails fast with
      `LangSpecError` before any download.

---

## 13. PROGRESS TRACKER

### Task status (mirror of the harness task list)
- **T2 — Scaffold (env, config, langspec):** ✅ DONE
- **T3 — Leaf verbs + downloader + channels:** ✅ DONE
- **T4 — Port dubber + muxer (freeze/trim parity):** ✅ DONE
- **T5 — status + ytpipe + batch + cli + run.sh:** ✅ DONE
- **T6 — Tests + user guide:** ✅ DONE

**ALL TASKS COMPLETE (verified on disk 2026-08-16).** `pytest yt-app/tests -q` → 45 passed.
`cli.py` dispatches all 11 verbs; every referenced framework symbol resolves; all `lib/*`
modules import cleanly under `env.bootstrap()`.

### Files implemented (verified on disk 2026-08-16)
- ✅ `yt-app/lib/__init__.py` — package docstring, import-order rule.
- ✅ `yt-app/lib/env.py` — `_discover_root`, `_set_if_unset_and_present`,
  `_self_configure_env`, `bootstrap()`, `repo_root()`. Idempotent `_STATE`.
- ✅ `yt-app/lib/config.py` — `defaults_path`, `load_defaults` (+ optional local),
  `effective_config` (deep-merge overrides, carry id/url), `company_config`, `tools_config`.
- ✅ `yt-app/lib/langspec.py` — `LangSpec` dataclass, `LangSpecError`, `PIPER_ONLY_LANGUAGES`
  = {fa, ur}, `build_langspec(cfg, root)` implementing all R6 rules; clone resolution reads
  `clone_languages` live.
- ✅ `yt-app/lib/channels.py` — `split_channels` (mono/stereo/all via channelsplit); NEW leaf.
- ✅ `yt-app/lib/downloader.py` — `normalize_url`, `is_playlist`, `download_video` (flag-gated),
  `playlist_entries` (flag-free).
- ✅ `yt-app/lib/transcriber.py` — `extract_audio`, `transcribe` (ASR → caption doc, auto-split).
- ✅ `yt-app/lib/translator.py` — `translate_all` (verbatim source + per-cue parallel MT).
- ✅ `yt-app/lib/dubber.py` — ported `run_dub` (keep-source splice, citation-strip, clone-ref
  trim, running-gap freeze plan, silent-span QA) + `build_sync_report`, all pure.
- ✅ `yt-app/lib/muxer.py` — ported `run_mux` (still-image / retimed / plain picture, subs modes,
  playback-speed last) + `freeze_windows`, `build_retimed_video`.
- ✅ `yt-app/lib/status.py` — `status.json` schema, atomic write, resume (`should_run`),
  per-language roll-up.
- ✅ `yt-app/lib/ytpipe.py` — `VideoPipeline` / `run_video`: 6-stage sequencer, dub/mux under lock.
- ✅ `yt-app/lib/batch.py` — module-global `DUB_LOCK = Semaphore(1)`, `run_batch`
  (ThreadPoolExecutor, per-video failure isolation, single-ref or `--list`).
- ✅ `yt-app/cli.py` — argparse dispatcher (11 verbs), first line `lib.env.bootstrap()`.
- ✅ `yt-app/run.sh` — repo-root resolution + exec venv python on cli.py (executable).
- ✅ `yt-app/config/defaults.json`, `yt-app/projects/list-1.json` — schema §9.
- ✅ `yt-app/tests/{conftest,test_langspec,test_freeze_plan,test_citation_strip,
  test_status_resume,test_config_merge,test_batch_concurrency}.py` — 45 passing, synthetic fixture.
- ✅ `docs/non-claude-user-guide.md` — comprehensive guide with the LOUD consent/`fa`-voice callouts.

## 14. Captured framework leaf signatures (reference — avoid re-reading source)

**`engines/asr.py`** — `transcribe(audio_path, out_dir, *, provider=None, model=None,
language=None, word_timestamps=True, timeout=3600, condition_on_previous_text=True,
hallucination_silence_threshold=None, temperature=None) -> ASRResult`. `ASRResult.cues` =
list of `TranscriptCue(id,start_ms,end_ms,text,confidence,no_speech_prob,words)`.
`KNOWN_PROVIDERS=("mlx-whisper","faster-whisper")`.

**`engines/mt.py`** — `translate_text(text, src_lang, tgt_lang, *, provider=None, model=None,
root=None, timeout=600) -> str`. Empty/whitespace → "". `KNOWN_PROVIDERS=("argos","ctranslate2",
"nllb","opus-mt")`.

**`engines/tts.py`** — `synthesize_cue(...)` (per-cue synth; used by dubber). Confirm arg
order when wiring dubber.

**`size.py`** — `compute_dimensions(axis, value, *, aspect="16:9") -> {width,height,aspect,
label,given}`; `parse_aspect`.

**`speed.py`** — `respeed(root, factor, source_path, *, dest=None) -> dict`; refuses to
overwrite source; output `<stem>_<factor><suffix>`.

**`util.py`** — `deep_merge(base, override)` (recursive dict-merge; list/scalar replace),
`load_company_config(root)`, `load_tools_config(root)`, `load_json(path, default=None)`,
`atomic_write_json`, `atomic_write_text`, `executable(name)`, `repo_root(start=None)`,
`parse_csv`, `KNOWN_LANGUAGES`, `CJK_LANGUAGES`, `RTL_LANGUAGES`.

**`captions.py`** — `split_long_cues(cues, *, max_ms, language) -> list` (proportional split,
renumber id 0..M); `max_cue_ms(root)` (default 7000); `render(caption_doc, fmt)`; `render_srt`/
`render_vtt`; `script_class(language)`; `validate_captions`. Caption doc =
`{language, cues:[{id,start_ms,end_ms,source_text,target_text,...}]}`.

**`net/fetch.py`** — `ytdlp_download(url, dest_dir, ...) -> {info, video_path, info_json_path,
stdout_tail}`; `ytdlp_playlist_entries(url, *, timeout=300) -> [{id,url,title,channel,
duration_seconds,playlist_id,playlist_title}]` (flag-free); `normalize_youtube_url`,
`is_playlist_url`.

**`media.py`** — `probe_summary(media_path) -> {duration_seconds, video:{codec,width,height,
fps,pix_fmt}, audio:{codec,sample_rate,channels}}`; `extract_wav(source,dest,*,sample_rate=16000,
channels=1)`; `slice_wav(source,dest,*,start_seconds=0.0,duration_seconds=60.0,sample_rate,
channels)`; `audio_duration_ms(media_path)->int`; `silent_wav(dest,*,duration_ms,
sample_rate=24000,channels=1)`; `normalize_wav(source,dest,*,sample_rate=24000,channels=1)`;
`concat_wavs(parts,dest,...)`; `loudnorm(source,dest,*,sample_rate=24000,channels=1)`;
`mux_video(video_source,audio_wav,dest_mp4,*,subs=None,audio_bitrate="192k")`;
`mux_video_burned_in(video_source,audio_wav,subs,dest_mp4,*,font_name=None,video_crf=18)`;
`slice_video(source,dest,*,start_seconds,duration_seconds,reencode=True)`;
`freeze_segment(source,dest,*,at_seconds,duration_ms,audio_sample_rate=44100,audio_channels=2,
pix_fmt="yuv420p")`; `concat_videos(parts,dest,...)`; `still_image_video(image,dest,*,
duration_ms,width=None,height=None,fps=25)` (returns Path, no audio — caller muxes dub over
it); `respeed_video(source,dest,*,factor,...)`. Constants: `ASR_SAMPLE_RATE`, `ASR_CHANNELS`,
`DUB_SAMPLE_RATE=24000`, `DUB_CHANNELS=1` (confirm exact names when wiring).

**`dubbing.py` / `packaging.py`** — state-coupled; DO NOT import. Port the pure helpers listed
in §7. Full source too large to inline; re-read `dubbing.py` (`run_dub` ~lines 527-668) and
`packaging.py` (`_freeze_windows`/`_build_retimed_video`/`run_mux`) when porting T4.

### Machine facts to honor
- Bare `python3` is **hook-blocked** → always run via `<repo>/.venv/bin/python3` (or `run.sh`).
- `clone_languages` live = `["en","zh","ar","es","ru","fr","pt"]`; `fa`/`ur` piper-only;
  **`fa` has NO staged piper voice** (dub fails loud — expected).
- Proxy CA bundle at `~/certs/aipe-certs.pem`; HF is policy-blocked (offline cache at
  `<repo-parent>/.cache/huggingface`).
