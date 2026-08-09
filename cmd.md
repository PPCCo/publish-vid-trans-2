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