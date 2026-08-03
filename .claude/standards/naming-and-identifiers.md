# STD-NAMING — Identifiers, Paths, and Language Codes

**Project / video ID.** Lowercase, filesystem-safe slug matching `^[a-z0-9][a-z0-9._-]{1,63}$`
(`paths.VIDEO_ID_RE`). YouTube-derived ingests use `yt-<11-char-video-id>`; other sources may use
any slug meeting the same pattern. This ID is the directory name under `projects/` and is
immutable for the life of the project.

**Artifact ID.** Assigned by `vid_cli.py artifact register`; never hand-authored. Stable for the
life of that artifact version — a new version gets a new `artifact_id`, linked back via
`supersedes`.

**Language codes.** ISO 639-1 (`en`, `fa`, `ar`, `ur`, `zh`, …) everywhere: `state.json`
`language_tracks` keys, `captions.<lang>.json` / `captions.<lang>.vtt` filenames, `audio/<lang>/`,
`video/<lang>/`, `packages/<lang>/`. `util.py` holds the known-code table plus the CJK/RTL/Cyrillic
script classification (`captions.script_class`) that drives caption line-length rules and burned-in
subtitle font selection — extend that table rather than special-casing a language elsewhere.

**File layout inside a project.** Fixed, not configurable per-project: `source/`, `transcripts/`,
`captions/`, `audio/<lang>/`, `video/<lang>/`, `packages/<lang>/`, `artifacts/manifest.json`,
`events.ndjson`, `state.json`, `rights/record.json`. Skills and engines read/write through
`ProjectPaths`, never by string-concatenating a path by hand.
