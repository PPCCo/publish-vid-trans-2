# STD-APPROVAL — Human Approval Contract

An approval record SHALL carry `approval_id`, `project_id`, `gate`, `language` (for the
per-language gates — `translation_qa`, `audio_qa`), `approver`, `approver_role`, `decision`,
`artifact_hashes`, `scope`, `exclusions`, `notes`, `created_at`, and optional `expires_at` /
`invalidation_at` / `invalidation_reason` (see `schemas/approval.schema.json`).

A grant records a **human's** decision. The bright line is not *which process types the command*
but that **an explicit human decision is on record before the grant is written**. An agent may
therefore run `vid_cli.py approval grant` and the following gated `project transition` **only** as
the mechanical execution of a human confirmation it has just obtained in-conversation, under the
mandatory disclosure-and-confirm protocol below. Absent that confirmation, an agent may only
recommend and prepare — it may never grant, transition, or set `rights_status` on its own initiative.

A gate opens only when both halves agree: the deterministic report for that gate's report type is
`PASS`, **and** a matching human approval is bound to the exact artifact hash(es) currently on
disk. Editing an approved artifact (new content, same logical file) supersedes its hash and
auto-invalidates the approval — the gate re-closes until a human re-approves the new hash.

## Gate UX — how the human decision is expressed and executed

Gates are *decide-not-operate* for the human (CLAUDE.md rule 13): the human's job is to **decide in
natural language**, never to run CLI plumbing. An agent SHALL:

1. **Surface the gate on request.** Whenever asked about a project's status (even in a fresh
   session), if it sits at a human gate the agent MUST say so plainly — "at human approval, the
   `<gate>` gate" — and lead into the review, not bury it.
2. **Present the decision as selectable options** (via `AskUserQuestion`): the concrete fixes for
   any issue *and* an explicit **"approve & advance"** option — approve is an option, never a
   command handed to the human.
3. **Disclose before granting.** Once the human picks approve, the agent MUST echo back, and get a
   single explicit confirmation of: (a) the **gate**; (b) the exact **artifact SHA-256** it will
   bind (the active, non-superseded hash on disk); (c) the **relative path(s)** being reviewed/
   approved; (d) any **issues/concerns** the agent wants the human to look at, each with a brief
   plain-language explanation. This disclosure-and-confirm is the recorded human decision.
4. **Execute the confirmed decision.** After that confirmation the agent MAY itself run
   `approval grant` (recording `--approver "<human>"`, the bound `--artifact` hash, and the decision
   + disclosed concerns in `--notes`) and the gated `project transition` (`--actor human`). No `!`
   block and no human-typed command are required. Flags are verified against `--help` first
   (`approval grant` → `--approver`; `project transition` → `--actor`) — the agent never pushes
   flag-checking or hash-hunting onto the human.

An agent MUST NOT grant or transition **without** the explicit post-disclosure confirmation from a
human, and MUST NOT approve to unblock its own work. The human may still choose to run the command
themselves via an `!` block if they prefer; the agent offers, but the default is agent-executed on
confirmation.
