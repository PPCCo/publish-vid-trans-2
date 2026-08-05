from __future__ import annotations

from pathlib import Path
from typing import Any

from .approvals import has_valid_approval
from .errors import StateTransitionError
from .events import append_event
from .paths import ProjectPaths
from .util import atomic_write_json, load_json, project_lock, utc_now


def workflow_config(root: Path) -> dict[str, Any]:
    return load_json(root / ".claude" / "config" / "workflow_states.json")


def allowed_transitions(root: Path, current: str) -> list[str]:
    return list(workflow_config(root).get("transitions", {}).get(current, []))


def required_gate(root: Path, current: str, target: str) -> str | None:
    """Resolve the human gate for an edge, if any. Gate topology is data in workflow_states.json."""
    edges = workflow_config(root).get("gate_edges", {})
    return edges.get(f"{current}->{target}")


def required_reports(root: Path, current: str, target: str) -> list[str]:
    """Report types whose latest gate report must be PASS for this edge."""
    reports = workflow_config(root).get("gate_reports", {})
    return list(reports.get(f"{current}->{target}", []))


def _report_decision(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        data = load_json(path)
        return str(data.get("decision") or "").upper() or None
    except Exception:  # noqa: BLE001
        return "INVALID"


def _rights_status(root: Path, project_id: str) -> str:
    paths = ProjectPaths(root, project_id)
    if not paths.rights_record.exists():
        return "unreviewed"
    try:
        return str(load_json(paths.rights_record).get("rights_status", "unreviewed"))
    except Exception:  # noqa: BLE001
        return "unreviewed"


def transition_blockers(root: Path, project_id: str, current: str, target: str) -> list[str]:
    paths = ProjectPaths(root, project_id).require()
    blockers: list[str] = []

    # 1. Human-gate approval requirement (per-language gates require an approval per active track).
    gate = required_gate(root, current, target)
    if gate:
        state = load_json(paths.state)
        per_language_gates = {"translation_qa", "audio_qa"}
        if gate in per_language_gates:
            tracks = _active_track_langs(state)
            lt = state.get("language_tracks", {})
            # The source-language track skips TRANSLATION (verbatim captions, nothing to
            # translate), so there is no translation to approve for it — don't demand one.
            if gate == "translation_qa":
                tracks = [lang for lang in tracks if not lt.get(lang, {}).get("skip_translation")]
            # audio_qa only applies to dub-enabled tracks; caption-only tracks carry no dub to
            # approve, so they must not demand (an impossible) audio approval.
            if gate == "audio_qa":
                tracks = [lang for lang in tracks if lt.get(lang, {}).get("dub_enabled")]
            missing = [lang for lang in tracks if not has_valid_approval(root, project_id, gate, lang)]
            if missing:
                langs = ", ".join(sorted(missing))
                blockers.append(f"valid {gate} approval required for language(s): {langs}")
        elif not has_valid_approval(root, project_id, gate, None):
            blockers.append(f"valid {gate} approval required")

    # 2. Gate-report PASS requirements (deterministic, non-human validators).
    for report_type in required_reports(root, current, target):
        if report_type == "rights":
            status = _rights_status(root, project_id)
            if status in {"unreviewed", "do-not-distribute"}:
                blockers.append(f"rights_status must be reviewed and distributable (is: {status})")
            continue
        decision = _report_decision(paths.gate_report(report_type))
        if decision != "PASS":
            blockers.append(f"{report_type} gate report must be PASS (is: {decision or 'missing'})")

    return blockers


def _active_track_langs(state: dict[str, Any]) -> list[str]:
    tracks = state.get("language_tracks", {})
    return [lang for lang, t in tracks.items() if t.get("status") != "failed"]


def transition(root: Path, project_id: str, target: str, actor: str, reason: str = "") -> dict[str, Any]:
    paths = ProjectPaths(root, project_id).require()
    config = workflow_config(root)
    if target not in config["states"]:
        raise StateTransitionError(f"Unknown target state: {target}")
    with project_lock(paths.lock):
        state = load_json(paths.state)
        current = state["current_state"]
        if target not in allowed_transitions(root, current):
            raise StateTransitionError(f"Transition not allowed: {current} -> {target}")
        blockers = transition_blockers(root, project_id, current, target)
        if blockers:
            raise StateTransitionError("; ".join(blockers))
        before = dict(state)
        state["previous_state"] = current
        state["current_state"] = target
        state["blocked_reasons"] = []
        state["updated_at"] = utc_now()
        state["updated_by"] = actor
        atomic_write_json(paths.state, state)
        append_event(
            paths.events, project_id, "STATE_TRANSITIONED", actor,
            {"from": current, "to": target, "reason": reason},
        )
        return {"before": before, "after": state}


def next_actions(root: Path, project_id: str) -> dict[str, Any]:
    """Enumerate candidate transitions from the current state with permitted/blocked status."""
    paths = ProjectPaths(root, project_id).require()
    state = load_json(paths.state)
    current = state["current_state"]
    candidates = []
    for target in allowed_transitions(root, current):
        gate = required_gate(root, current, target)
        blockers = transition_blockers(root, project_id, current, target)
        candidates.append({
            "target": target,
            "required_gate": gate,
            "required_reports": required_reports(root, current, target),
            "permitted": not blockers,
            "blockers": blockers,
        })
    return {"project_id": project_id, "current_state": current, "candidates": candidates}


def plan(root: Path, project_id: str) -> dict[str, Any]:
    """Deterministic recommendation for the autonomous run loop.

    autonomy_action is one of PROCEED / STOP_AT_GATE / BLOCKED / TERMINAL.
    """
    config = workflow_config(root)
    actions = next_actions(root, project_id)
    current = actions["current_state"]
    terminal = set(config.get("terminal_states", []))

    if current in terminal:
        return {**actions, "autonomy_action": "TERMINAL", "is_human_gate": False, "recommended_target": None}

    # Forward progress is defined by position in the linear `states` list. Backward edges
    # (revision paths like TRANSCRIPT_QA_GATE->TRANSCRIPTION) are only taken on gate
    # rejection, never autonomously — so they are excluded from PROCEED candidates.
    order = {name: i for i, name in enumerate(config.get("states", []))}
    hold = {"PAUSED", "CANCELLED", "ERROR"}
    here = order.get(current, -1)

    def _forward(candidate: dict[str, Any]) -> bool:
        return order.get(candidate["target"], -1) > here and candidate["target"] not in hold

    human_gate_targets = [c for c in actions["candidates"] if c["required_gate"] and _forward(c)]
    non_gate_permitted = [c for c in actions["candidates"]
                          if not c["required_gate"] and c["permitted"] and _forward(c)]

    if non_gate_permitted:
        return {**actions, "autonomy_action": "PROCEED", "is_human_gate": False,
                "recommended_target": non_gate_permitted[0]["target"]}
    if human_gate_targets:
        gate = human_gate_targets[0]["required_gate"]
        return {**actions, "autonomy_action": "STOP_AT_GATE", "is_human_gate": True,
                "required_gate": gate, "recommended_target": human_gate_targets[0]["target"]}
    return {**actions, "autonomy_action": "BLOCKED", "is_human_gate": False, "recommended_target": None}
