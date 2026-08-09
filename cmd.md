cd /Users/qaiser.abbas/Dev/my-repos/pub/publish-vid-trans

# --- Cloned (XTTS-v2, slow: ~1.3 min/cue on CPU/MPS) ---
.venv/bin/python3 .claude/scripts/vid_cli.py dub run yt-MFuUIoF5PSc --language en --clone --actor agent
.venv/bin/python3 .claude/scripts/vid_cli.py dub run yt-MFuUIoF5PSc --language ar --clone --actor agent
.venv/bin/python3 .claude/scripts/vid_cli.py dub run yt-MFuUIoF5PSc --language zh --clone --actor agent

# --- Piper male (fast, staged voices) ---
.venv/bin/python3 .claude/scripts/vid_cli.py dub run yt-MFuUIoF5PSc --language fr --actor agent
.venv/bin/python3 .claude/scripts/vid_cli.py dub run yt-MFuUIoF5PSc --language es --actor agent
.venv/bin/python3 .claude/scripts/vid_cli.py dub run yt-MFuUIoF5PSc --language pt --actor agent
.venv/bin/python3 .claude/scripts/vid_cli.py dub run yt-MFuUIoF5PSc --language ru --actor agent
.venv/bin/python3 .claude/scripts/vid_cli.py dub run yt-MFuUIoF5PSc --language ur --actor agent

# Once all 8 are dubbed:
.venv/bin/python3 .claude/scripts/vid_cli.py dub qa yt-MFuUIoF5PSc --actor agent

No .env.local/network env needed — none of these fetch anything (clone dubs use the already-downloaded source/audio.wav as
speaker reference; captions are already built). Tell me when it's done — or if you want to run just en first and pause there so
we can listen and confirm before committing to the rest, say so and I'll adjust the block.

I've also narrowed the TTS config to the confirmed scope (en/ar/zh/es → xtts clone; fr/pt/ru/ur → their staged male piper
voices) and re-ran the full test suite (346 passed). And I've made the dub-handoff rule mandatory in memory going forward — dub
run and other long external commands (whisper, ffmpeg, ingest) will always be handed to you as a copy-paste block rather than
run as a background task inside the session.

---

The audio generated in @projects/yt-MFuUIoF5PSc/audio/ folder are all messed up wherever there is Arabic verses during the source (Persian) speech. I believe this is caused by the arabic text or transliteration being included in the `target_text` or the captions. For `en` as an example, `«أَعُوذُ بِاللهِ مِنَ الشَّيْطَانِ الرَّجِيمِ، بِسْمِ اللهِ الرَّحْمٰنِ الرَّحِيمِ»` is appearing in all files in `projects/yt-MFuUIoF5PSc/captions/`: captions.en.json, captions.en.srt, captions.en.vtt

I think to make this simple and avoid further problems, we should make sure even arabic verses are in the target language only, and arabic characters should never go into `captions.<lang>.srt` or `captions.<lang>.vtt`. For `captions.<lang>.json`, the `target_text` should never have any arabic text.The only exception to this is `ar` because the target language is the same as Quran language (Arabic).

This might mean removing the special treatment added for Quran/Arabic added earlier in the `.claude/` framework.
