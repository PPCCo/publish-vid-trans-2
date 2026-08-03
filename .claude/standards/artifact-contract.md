# STD-ARTIFACT — Artifact Contract

Every produced file (transcript, worksheet, captions, dub WAV, muxed video, package) SHALL be
registered via `vid_cli.py artifact register` before anything downstream depends on it, and its
manifest record SHALL carry: `artifact_id`, `project_id`, `type`, `language` (when
language-scoped), `stage`, `version`, `path`, `sha256`, `size_bytes`, `created_at`, `created_by`,
`source_artifact_ids`, `provenance`, `rights_status`, `validation_status`, `supersedes`, `frozen`
(see `schemas/artifact.schema.json`).

Approvals bind to an artifact's exact `sha256`, never to "the file" in the abstract. Re-registering
a path that already has an approved artifact creates a new version (`supersedes` the old hash) and
auto-invalidates any approval scoped to the superseded hash — the approval record is not deleted,
it is marked invalidated with a reason, so the audit trail stays intact.

No agent hand-writes `artifacts/manifest.json`. The CLI is the only writer.
