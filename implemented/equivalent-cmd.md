# command equivalent

This is a raw idea, so I'd like you to improve the it further:

I'm thinking of adding either two commands or two new verbs. One would give me the command equivalent  (for example `cmd`) runnable outside of Claude (in a normal macOS terminal). For example if I give `/new-video MFuUIoF5PSc` or `/cmd "/new-video PLDvVOFNMIIG3snBzVLV65D9YHbmkN8s2Z"` as input to this command or verb, it would give me the  `yt-dlp` equivalent to run in a macOS terminal (along with the path where to run it from).

The other command or verb (for example `nextcmd`) will give me the external command equivalent of `next_command` (if applicable).

---

⏺ All done and verified. Here's what shipped.

Summary

Two new pure, read-only CLI verbs that print the raw external shell command (runnable in a plain macOS terminal) equivalent
to what a pipeline step would otherwise run for you:

- vid_cli.py cmd <video-id-or-url> — the onboarding/ingest equivalent (the yt-dlp command) for a video URL, bare id,
playlist, or existing catalog id.
- vid_cli.py nextcmd <project-id> — the external-command equivalent of the project's current next step (the next_command
analog).

Both default to bare-terminal form; --for-claude re-adds the ! prefix.

Behavior, exactly as you asked:
- When both a raw external command and a vid_cli.py verb exist → both shown as Option A / Option B.
- No external tool (pure state ops like langid set) → falls back to the vid_cli.py verb, labelled "CLI-only state op".
- Human gates → no runnable command, an explanation, and the disclose+confirm-first approval line as reference (never gets
!, even with --for-claude).
- Template-driven: one _STEP_TEMPLATES table keyed like catalog._STATE_NEXT_VERB; adding another raw-external state later is
one dict entry.
- Each emission is a full runnable block (cd <dir> + correct env preamble + command). I found and honored a subtlety:
brew/PATH yt-dlp needs only export VIDTRANS_FETCH_ENABLED=1, while vid_cli.py ingest run (Python net layer) needs source
.env.local; playlist enumeration needs nothing (flag-free carve-out).

Files:
- New: cmdgen.py, tests/test_cmdgen.py (21 tests, all pass).
- Edited net/fetch.py — factored pure build_ytdlp_download_argv / build_ytdlp_playlist_argv / is_playlist_url out of the
executors so the printed argv can never drift from what actually runs (there's a regression test proving it); re-exported
via net/__init__.py.
- Edited catalog.py — factored the kickoff/gate/rights command strings into shared helpers so both surfaces reuse them.
- Edited cli.py — parsers, dispatch, and a narrow raw-text print special-case in main().

Test status: all 21 new tests pass; full suite is 330 passed, 1 skipped, 4 failed — and I verified those 4 are pre-existing
(rights/consent tests unrelated to this change; they fail identically with my changes stashed out). First-cut scope is
INGEST/onboarding for the raw external command, per the plan; other states fall back to their vid_cli.py verb.

Nothing was committed — let me know if you'd like me to commit this (on a branch), or add the CLAUDE.md/OPERATING-GUIDE.md
doc entries and a /cmd skill wrapper (noted as out-of-scope follow-ups in the plan).