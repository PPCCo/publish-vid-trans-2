# video-speed and Image as Video Display

DO THESE THREE TASKS:

## TASK 1: Accept Image to display during the Audio track

There are some videos where I would like to keep an image shown throughout the video, and dub the audio track on it, so the same image will be displayed throughout the video playtime, along with the specific language. The image would be different for each language. It is also possible that some languages keep the source video while others have image displayed, so the configuration would be per-language. Since in those languages, only an image is displayed, it might be faster to use image instead of video.

For the best proportions for the Image to be used for a youtube video, create a script (or add a verb to `vid_cli.py`) that would take either width or height of the image to be used and give the full dimensions. for instance, if I give the command `.venv/bin/python .claude/scripts/vid_cli.py size w 1042` or `.venv/bin/python .claude/scripts/vid_cli.py size h 583`, it might give the same result `1042x583`. This is not accurate, just as an example.

## TASK 2: Varying audio speed is sounding terrible

The audio speed going slow and fast sounds terrible. I want to set this as a hard requirement that the audio speed should stay the same. The video speed can be changed to fast or slow, but the audio speed should stay constant, ALWAYS. This should be very easy if the video to be dubbed in a language (see the above section).

## TASK 3: Speed up any video+audio

Some of the persian speeches (source) are quite slow. While in Persian language, it sounds fine, in ur and en, they seem a bit too slow. Can you ask at project initialization stage (at `/new-video`) if default audio+video speed of the dubbed video should be changed, where a human response can be in natural language, for example, "1.25x for en/ur" or something like that. The speed would be 1x (no change in speed) by default.

Can you also create a python script that I can run outside Claude, that would keep the source-video-file untouched, and create a new file with the speed changed by a specific ratio. You can create a completely new python script for this, or give me the ffmpeg command to run, or add this as a speed verb, for example, `.venv/bin/python .claude/scripts/vid_cli.py speed 1.2 projects/yt-YP0FDR7Wc-8/video/en/dubbed.mp4` would speed up the whole audio+video of `projects/yt-YP0FDR7Wc-8/video/en/dubbed.mp4` by 1.2x

Note that this command would even work on any, even an existing video.

## RESPONSE

1. vid_cli.py size w 1920 → 1920x1080; size h 1080 → 1920x1080; size w 1042 → even-rounded WxH.
2. vid_cli.py speed 1.25 <any.mp4> --out /tmp/out.mp4; ffprobe: dest duration ≈ src/1.25, source
file unchanged (mtime/hash), audio+video durations equal in dest.
3. On yt-YP0FDR7Wc-8: project set-speed <id> --language en --factor 1.25; project set-image <id> --language ur --path <img>; inspect project.yaml.
4. TASK 2: re-run dub run --language ar (freeze_trim lang) and a hold-only lang; confirm no
time_stretch in the path (grep + read), sync report PASSes, freeze-plan written for BOTH.
dub qa aggregate PASS.
5. package mux --language en with speed 1.25 → dubbed.mp4 runtime ≈ (source freeze-retimed)/1.25;
A/V aligned; package mux --language ur with still image → single static frame, dub audio present,
duration == dub duration; package final-qa PASS.
6. Run existing test suite (find pytest/tests dir) + framework validate + doctor.
7. Grep for any remaining time_stretch / max_time_stretch-gated behavior to confirm removal.

Yes, and bypass permissions. Just one minor thing, for `vid_cli.py speed 1.25 <any.mp4>` command, if the `--out` is not given, the output folder would be the same folder as the source file, with file speed added to the new file's name. For example, `vid_cli.py speed 1.25 <path/to/my-file-name.mp4>` would generate `path/to/my-file-name_1.25.mp4`

---

---

# Plan: still-image display, constant audio speed, playback-speed + helper verbs

 Context

 Three operator-driven improvements to the video-translation pipeline:

 1. Still image per language (TASK 1). Some source videos are just a talking-head/backdrop; for
 certain output languages we'd rather show one fixed image for the whole runtime with the dub over
 it (different image per language; some languages keep the source video). Image-only is also faster
 to render. Need a per-language config for the image, and a size verb that computes full 16:9
 YouTube dimensions from a single given width or height.
 2. Constant audio speed (TASK 2). The current per-cue tempo-stretch (speeding/slowing each cue's
 TTS to fit its caption slot) sounds terrible — audible rubber-banding. Hard requirement: dubbed
 audio always plays at natural TTS speed, never time-stretched. The picture is re-timed
 (freeze/trim) to stay in sync. This makes the existing opt-in freeze-frame feature the mandatory,
 only behavior for every language. (User: remove the stretch path entirely.)
 3. Deliberate playback speed (TASK 3). Some Persian source speeches are slow; in en/ur they feel
 too slow. Want a per-language default playback speed (e.g. "1.25x for en/ur", default 1x) asked at
 /new-video, applied as a uniform whole-video speedup at mux (audio+video scale together — no
 rubber-banding, compatible with TASK 2). Plus a standalone speed verb that re-times any existing
 video file (source untouched → new file).

 All three follow the repo's four-point trail (CLI/config, CLAUDE.md rule, OPERATING-GUIDE section,
 affected skills). Per-language settings live in project.yaml (which is additionalProperties: true),
 NOT in state.json (per-track schema is additionalProperties: false), matching how mux_mode lives.

 ---
 TASK 2 — constant audio speed (do this first; simplest + unblocks the mental model)

 dubbing.py run_dub per-cue loop (lines ~354–396): remove the stretch math. Replace the
 stretch/over_cap/applied block + media_mod.time_stretch(...) call with a straight
 format-normalize of the raw TTS wav (natural length preserved). Reuse the existing identity path:
 media_mod._reencode(raw, fit, sample_rate=sr, channels=ch) (media.py:250) — or expose a thin public
 normalize_wav wrapper for it. rendered_ms then == natural_ms; keep recording cue_measures but
 set stretch_factor = natural/slot for reporting only and over_stretch_cap = False always.

 Always compute a freeze/trim plan (drop the if bars["freeze_frame_enabled"] guard at
 dubbing.py:418–422): with no stretch, the picture MUST absorb 100% of every mismatch, so the plan is
 mandatory. _plan_freezes already works purely from cue_measures and needs no change. trim_mode
 still per-language via freeze_trim_languages.

 _audio_quality_bars (dubbing.py:112–129): max_time_stretch/freeze_stretch_cap/
 freeze_frame_enabled become reporting-only. Keep reading them (back-compat) but they no longer gate
 behavior. Set the effective per-cue cap to 1.0 conceptually (no stretch ever).

 _build_sync_report / analyze_sync (dubbing.py ~562–855): the freeze-mode drift branch
 (measuring residual against the post-freeze timeline) becomes the only path. audio-stretch-over-cap
 majors can no longer fire (no stretch). Verify PASS logic still holds: Model A (trim) → residual 0;
 Model B (hold) → honest one-sided residual cleared under per_cue_drift_tolerance_ms.

 import_dub: unaffected (never stretched); leave as-is.

 packaging.py: _freeze_enabled (line 147) currently gates _active_freeze_plan. Since freeze is
 now always on, make _freeze_enabled return True (or drop the guard) so mux always rebuilds the
 picture when a non-empty plan exists. The retiming machinery (_build_retimed_video,
 _freeze_windows, _write_captions_for_timeline) is unchanged.

 Company config: set quality_bars.audio.freeze_frame_enabled: true in company.default.json (now
 the default, not opt-in) and document the stretch bars as legacy/reporting-only.

 ---
 TASK 1 — still image per language + size verb

 size verb (standalone, no project)

 New module size.py with compute_dimensions(axis, value, aspect="16:9"). axis in {w,h};
 returns {"width":..,"height":..,"aspect":"16:9","label":"WxH"}, rounding to even integers (H.264
 needs even dims). 16:9 default (YouTube standard); optional --aspect (e.g. 4:3, 1:1).
 - cli.py: sz = sub.add_parser("size", ...); positional axis (choices w/h), positional value
 (int), optional --aspect (default "16:9"). Dispatch: if cmd == "size": from . import size; return size.compute_dimensions(args.axis, args.value,
 aspect=args.aspect).
 - Example: vid_cli.py size w 1920 → 1920x1080; size h 1080 → 1920x1080.

 Per-language still image

 - project init flag --images "en=/abs/a.jpg,ur=/abs/b.png" (comma-separated lang=path map —
 new convention for a single-shot per-language value; documented). Stored in project.yaml under a
 new top-level images: {en: "...", ur: "..."} map. Validate each path exists + is a readable image
 (by suffix .jpg/.jpeg/.png/.webp) at init; store absolute paths.
 - New verb project set-image <id> --language <iso> --path <file> (and --clear) so an operator
 can set/change/remove per-language images on an in-flight project without re-init. Writes
 project.yaml.images, appends an event IMAGE_SET. (Mirrors the enable-dub on/off pattern.)
 - media.py new helper still_image_video(image, dest, *, duration_ms, size=None, fps=...) — build
 a silent H.264 MP4 of the looped still image for duration_ms using
 ffmpeg -loop 1 -i img -t <s> -r <fps> -pix_fmt yuv420p -vf scale/pad -an .... Even dims; pad to
 16:9 if needed (reuse size.compute_dimensions for a target canvas).
 - packaging.py run_mux: before the freeze branch, resolve the per-language image via a new
 _still_image_for(cfg, language). If set:
   - Picture source becomes a still-image video of length == dub duration (media_mod.audio_duration_ms (dub_path)), so no freeze plan is needed for
 image languages (a static frame has no motion to
 desync) — skip _build_retimed_video for these; captions attach normally (soft/burned/none) and
 are NOT retimed (timeline == dub timeline == caption timeline).
   - This is a re-encode path (looped image), so mux_video needs the picture from the generated MP4;
 mux_video's -c:v copy is fine because the generated still MP4 is already H.264.
 - packaging.py run_package / _clip_source_for_lang (lines ~589–601): image-language dubbed
 tracks already produce dubbed.mp4, so the package path is unchanged (it copies dubbed.mp4).
 Only caption-only + image would matter; out of scope (image is for dub-over-image) — document that
 image applies to dub-enabled tracks.
 - Artifact: register the generated still-image MP4 picture as provenance input to the
 dubbed-video artifact (source_artifact_ids), analogous to the freeze-plan artifact wiring.

 ---
 TASK 3 — per-language playback speed + speed verb

 speed verb (standalone, works on ANY file)

 - media.py new helper respeed_video(source, dest, *, factor, timeout=...) — uniform whole-file
 re-time: ffmpeg -i src -filter:v "setpts=PTS/{factor}" -filter:a "{atempo_chain}" -c:v libx264 -crf 18 -c:a aac dest. Reuse _atempo_chain(factor)
 (media.py:190) for the audio side so factors
 outside 0.5–2.0 chain correctly. Video via setpts=PTS/factor (factor>1 = faster). Audio+video
 scale by the same factor → stay in sync, constant speed (no rubber-banding). Keeps source file
 untouched (writes a new dest).
 - speed.py thin module: respeed(root, factor, source_path, dest=None) — resolves dest
 (default <source_stem>.<factor>x<suffix> next to source), calls media.respeed_video, returns
 {"source":..,"dest":..,"factor":..,"src_duration_ms":..,"dest_duration_ms":..}. Does NOT require a
 project; path is taken verbatim (relative to cwd/root). Refuses to overwrite source.
 - cli.py: sp = sub.add_parser("speed", ...); positional factor (float), positional path;
 optional --out. Dispatch → speed.respeed(root, args.factor, args.path, dest=args.out).
 - Example: vid_cli.py speed 1.2 projects/yt-YP0FDR7Wc-8/video/en/dubbed.mp4 → writes
 .../en/dubbed.1.2x.mp4, source intact.

 Per-language default speed at mux

 - project init flag --speeds "en=1.25,ur=1.25" (comma lang=float map, default 1.0). Stored in
 project.yaml under new top-level playback_speed: {en:1.25, ur:1.25} (langs absent → 1.0).
 - New verb project set-speed <id> --language <iso> --factor <f> (--factor 1.0 resets) so speed
 is adjustable post-init. Event SPEED_SET.
 - packaging.py run_mux: after producing dst (the muxed dubbed.mp4), if
 playback_speed[language] != 1.0, run media_mod.respeed_video(dst, tmp, factor=f) and replace
 dst (uniform speedup of the finished, in-sync file). Record playback_speed in the VIDEO_MUXED
 event + return dict + artifact provenance. This is applied AFTER freeze-retiming/still-image, so it
 uniformly scales whatever picture+audio pair was produced — audio stays internally natural-per-cue
 but the whole track is deliberately, uniformly faster (a global tempo choice, not per-cue rubber).
 - /new-video capture: the interview asks for speed in natural language ("1.25x for en/ur"); the
 skill parses it to the --speeds map. Default 1x → omit the flag.

 ---
 Interview + docs

 .claude/skills/new-video/SKILL.md — add two step-2.5 bullets:
 - Still image per language — ask (multiSelect over the dub set) which languages show a fixed image;
 for each, ask/collect an image path (free text). → --images en=/p/a.jpg,ur=/p/b.png.
 - Playback speed — free-text/single-select: "default 1x; e.g. '1.25x for en/ur'". Parse to
 --speeds en=1.25,ur=1.25. Document both flags in the step-3 project init line + "omit for
 default" notes, per the skill's flag-documentation convention.

 .claude/skills/mux-and-package/SKILL.md — add a short section noting mux auto-detects
 per-language still-image + playback-speed from project.yaml (config-driven, no new mux flag), like
 the freeze-frame precedent; and that audio is always natural-speed (picture re-timed).

 .claude/CLAUDE.md — (a) amend rule 14 / add a rule: audio speed is now ALWAYS constant; freeze-
 frame is mandatory (not opt-in); per-cue tempo-stretch removed. (b) new rule/section for per-language
 still image + playback speed, and the size/speed/set-image/set-speed verbs. (c) Note the
 --images/--speeds lang=value map convention.

 OPERATING-GUIDE.md — §3 (init flags), §8 (new recipes: set-image, set-speed, speed on any
 file, size), §9 (freeze-frame now mandatory / audio-constant).

 Memory — update freeze-frame-dubbing.md (now mandatory/always-on) and add a memory for the
 still-image + speed + size/speed verbs.

 ---
 Files touched (summary)

 - New: .claude/scripts/video_translation_house/size.py, speed.py
 - media.py — still_image_video, respeed_video helpers (+ maybe normalize_wav wrapper)
 - dubbing.py — remove per-cue stretch; always plan freezes; reporting-only bars
 - packaging.py — still-image picture source; post-mux uniform respeed; freeze always-on
 - project.py — init_project (images, playback_speed); new set_image, set_speed; validation
 - cli.py — size, speed, project set-image, project set-speed, init --images/--speeds
 - .claude/config/company.default.json — freeze_frame_enabled: true default
 - Skills: new-video, mux-and-package; .claude/CLAUDE.md; OPERATING-GUIDE.md; memory files
 - (No state.schema.json change needed — per-language settings live in project.yaml.)

 Tests

 Existing suite in tests/ (26 files, run with the venv pytest). Directly affected:
 - tests/test_dubbing.py, tests/test_freeze_frame.py — TASK 2 changes semantics (no stretch; freeze
 always on). Update expectations: assert no tempo-stretch applied, freeze plan written even without
 the old opt-in flag, over_stretch_cap never True.
 - tests/test_packaging.py — add still-image mux + post-mux respeed cases.
 Existing suite in tests/ (26 files, run with the venv pytest). Directly affected:
 - tests/test_dubbing.py, tests/test_freeze_frame.py — TASK 2 changes semantics (no stretch; freeze
 always on). Update expectations: assert no tempo-stretch applied, freeze plan written even without
 the old opt-in flag, over_stretch_cap never True.
 - tests/test_packaging.py — add still-image mux + post-mux respeed cases.
 New tests: tests/test_size.py (dimension math + even-rounding + aspect), tests/test_speed.py
 (respeed writes new file, source untouched, dest duration scales, refuses overwrite),
 tests/test_still_image.py (set-image config + image-language mux picks static frame).

 Verification

 always on). Update expectations: assert no tempo-stretch applied, freeze plan written even without
 the old opt-in flag, over_stretch_cap never True.
 - tests/test_packaging.py — add still-image mux + post-mux respeed cases.
 New tests: tests/test_size.py (dimension math + even-rounding + aspect), tests/test_speed.py
 (respeed writes new file, source untouched, dest duration scales, refuses overwrite),
 tests/test_still_image.py (set-image config + image-language mux picks static frame).

 Verification

 (respeed writes new file, source untouched, dest duration scales, refuses overwrite),
 tests/test_still_image.py (set-image config + image-language mux picks static frame).

 Verification


 1. vid_cli.py size w 1920 → 1920x1080; size h 1080 → 1920x1080; size w 1042 → even-rounded WxH.
 2. vid_cli.py speed 1.25 <any.mp4> --out /tmp/out.mp4; ffprobe: dest duration ≈ src/1.25, source
 1. vid_cli.py size w 1920 → 1920x1080; size h 1080 → 1920x1080; size w 1042 → even-rounded WxH.
 2. vid_cli.py speed 1.25 <any.mp4> --out /tmp/out.mp4; ffprobe: dest duration ≈ src/1.25, source
 file unchanged (mtime/hash), audio+video durations equal in dest.
 3. On yt-YP0FDR7Wc-8: project set-speed <id> --language en --factor 1.25; project set-image <id> --language ur --path <img>; inspect project.yaml.
 3. On yt-YP0FDR7Wc-8: project set-speed <id> --language en --factor 1.25; project set-image <id> --language ur --path <img>; inspect project.yaml.
 4. TASK 2: re-run dub run --language ar (freeze_trim lang) and a hold-only lang; confirm no
 dub qa aggregate PASS.
 5. package mux --language en with speed 1.25 → dubbed.mp4 runtime ≈ (source freeze-retimed)/1.25;
 A/V aligned; package mux --language ur with still image → single static frame, dub audio present,
     │ A/V aligned; package mux --language ur with still image → single static frame, dub audio present,                                              │
     │ duration == dub duration; package final-qa PASS.                                                                                               │
     │ 6. Run existing test suite (find pytest/tests dir) + framework validate + doctor.                                                              │
     │ 7. Grep for any remaining time_stretch / max_time_stretch-gated behavior to confirm removal.                                                   │
     ╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
     │ - tests/test_dubbing.py, tests/test_freeze_frame.py — TASK 2 changes semantics (no stretch; freeze                                             │
     │ always on). Update expectations: assert no tempo-stretch applied, freeze plan written even without                                             │
     │ the old opt-in flag, over_stretch_cap never True.                                                                                              │
     │ - tests/test_packaging.py — add still-image mux + post-mux respeed cases.                                                                      │
     │ New tests: tests/test_size.py (dimension math + even-rounding + aspect), tests/test_speed.py                                                   │
     │ (respeed writes new file, source untouched, dest duration scales, refuses overwrite),                                                          │
     │ tests/test_still_image.py (set-image config + image-language mux picks static frame).                                                          │
     │                                                                                                                                                │
     │ Verification                                                                                                                                   │
     │                                                                                                                                                │
     │ 1. vid_cli.py size w 1920 → 1920x1080; size h 1080 → 1920x1080; size w 1042 → even-rounded WxH.                                                │
     │ 2. vid_cli.py speed 1.25 <any.mp4> --out /tmp/out.mp4; ffprobe: dest duration ≈ src/1.25, source                                               │
     │ file unchanged (mtime/hash), audio+video durations equal in dest.                                                                              │
     │ 3. On yt-YP0FDR7Wc-8: project set-speed <id> --language en --factor 1.25; project set-image <id> --language ur --path <img>; inspect           │
     │ project.yaml.                                                                                                                                  │
     │ 4. TASK 2: re-run dub run --language ar (freeze_trim lang) and a hold-only lang; confirm no                                                    │
     │ time_stretch in the path (grep + read), sync report PASSes, freeze-plan written for BOTH.                                                      │
     │ dub qa aggregate PASS.                                                                                                                         │
     │ 5. package mux --language en with speed 1.25 → dubbed.mp4 runtime ≈ (source freeze-retimed)/1.25;                                              │
     │ A/V aligned; package mux --language ur with still image → single static frame, dub audio present,                                              │
     │ duration == dub duration; package final-qa PASS.                                                                                               │
     │ 6. Run existing test suite (find pytest/tests dir) + framework validate + doctor.                                                              │
     │ 7. Grep for any remaining time_stretch / max_time_stretch-gated behavior to confirm removal.                                                   │
     ╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
