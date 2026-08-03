# STD-RIGHTS — Rights Ledger Contract

Every project SHALL have exactly one `rights/record.json` (see
`schemas/rights-record.schema.json`), carrying `rights_status`, `voice_clone_consent`,
`consent_evidence`, `evidence_paths`, `reviewer`, `notes`, `updated_at`, `updated_by`.

`rights_status` starts at `unreviewed` on project init and only a human can move it — via
`vid_cli.py rights set`, never by an agent hand-editing the file. Valid distributable values are
`self-authored`, `licensed`, and `fair-use-claimed`; `unreviewed` and `do-not-distribute` block
`PACKAGE → READY_FOR_REVIEW` regardless of how clean every upstream gate is. The pipeline still
runs the full lifecycle to `READY_FOR_REVIEW` under `unreviewed` rights — rights gate distribution,
not production — but nothing under `outputs/` is safe to hand to a human for publishing until this
gate clears.

`voice_clone_consent` defaults to `false`. Dubbing with a cloned source-speaker voice
(`dub run --clone`) is refused by `dubbing.py` unless this flag is `true` in the current rights
record; the default (and only fallback) is a neutral TTS voice. An agent may draft a recommended
`rights_status` and gather evidence (channel, stated license, YouTube caption availability) into
`rights/evidence/`, but setting the record itself is `disable-model-invocation: true`.
