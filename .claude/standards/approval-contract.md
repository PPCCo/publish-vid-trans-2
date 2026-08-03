# STD-APPROVAL — Human Approval Contract

An approval record SHALL carry `approval_id`, `project_id`, `gate`, `language` (for the
per-language gates — `translation_qa`, `audio_qa`), `approver`, `approver_role`, `decision`,
`artifact_hashes`, `scope`, `exclusions`, `notes`, `created_at`, and optional `expires_at` /
`invalidation_at` / `invalidation_reason` (see `schemas/approval.schema.json`).

Only a human (or a human-invoked skill with `disable-model-invocation: true`) may grant an
approval. An agent may recommend a transition and assemble the gate packet (deterministic report
+ artifact hashes + a plain-language summary) but may never call `vid_cli.py approval grant`
itself, set `rights_status`, or force a transition through a human gate.

A gate opens only when both halves agree: the deterministic report for that gate's report type is
`PASS`, **and** a matching human approval is bound to the exact artifact hash(es) currently on
disk. Editing an approved artifact (new content, same logical file) supersedes its hash and
auto-invalidates the approval — the gate re-closes until a human re-approves the new hash.
