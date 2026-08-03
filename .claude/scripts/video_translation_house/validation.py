from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .util import load_json


def schemas_dir(root: Path) -> Path:
    return root / ".claude" / "schemas"


@cache
def _load_schema(root_str: str, schema_name: str) -> dict[str, Any]:
    path = Path(root_str) / ".claude" / "schemas" / schema_name
    if not path.is_file():
        raise ValidationError(f"Unknown schema: {schema_name}")
    return load_json(path)


@cache
def _registry(root_str: str) -> Any:
    """A referencing Registry of every local schema, keyed by both filename and $id, so
    cross-file `$ref`s (e.g. review -> finding.schema.json) resolve offline instead of
    triggering a network fetch."""
    from referencing import Registry, Resource

    sdir = Path(root_str) / ".claude" / "schemas"
    resources: list[tuple[str, Any]] = []
    for sf in sorted(sdir.glob("*.schema.json")):
        schema = load_json(sf)
        resource = Resource.from_contents(schema)
        resources.append((sf.name, resource))
        sid = schema.get("$id")
        if sid and sid != sf.name:
            resources.append((sid, resource))
    return Registry().with_resources(resources)


def validate_data(root: Path, data: Any, schema_name: str) -> list[str]:
    """Validate ``data`` against a named schema. Returns a list of error strings (empty = valid)."""
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover
        raise ValidationError("jsonschema is required; install framework dependencies") from exc
    schema = _load_schema(str(root), schema_name)
    validator = jsonschema.Draft202012Validator(schema, registry=_registry(str(root)))
    errors = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        location = "/".join(str(p) for p in err.path) or "(root)"
        errors.append(f"{location}: {err.message}")
    return errors


def validate_file(root: Path, path: Path, schema_name: str) -> list[str]:
    try:
        data = load_json(path)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        return [f"could not load {path}: {exc}"]
    return validate_data(root, data, schema_name)


def require_valid(root: Path, data: Any, schema_name: str) -> None:
    errors = validate_data(root, data, schema_name)
    if errors:
        raise ValidationError(f"{schema_name} validation failed: " + "; ".join(errors))


def validate_framework(root: Path) -> dict[str, Any]:
    """Self-check the framework: state machine coherence + schemas load.

    Mirrors publish-book's `framework validate`: the state machine's own topology is
    validated (every gate_edges entry is a declared transition; every gate resolves).
    """
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "status": "pass" if ok else "fail", "detail": detail})

    # 1. All schemas parse as JSON.
    sdir = schemas_dir(root)
    schema_files = sorted(sdir.glob("*.schema.json")) if sdir.is_dir() else []
    for sf in schema_files:
        try:
            load_json(sf)
            add(f"schema:{sf.name}", True, "valid JSON")
        except Exception as exc:  # noqa: BLE001
            add(f"schema:{sf.name}", False, str(exc))
    add("schemas-present", bool(schema_files), f"{len(schema_files)} schema files")

    # 2. State machine coherence.
    try:
        wf = load_json(root / ".claude" / "config" / "workflow_states.json")
        states = set(wf.get("states", []))
        transitions = wf.get("transitions", {})
        gate_edges = wf.get("gate_edges", {})
        gate_map = wf.get("human_gate_to_state", {})

        add("initial-state-known", wf.get("initial_state") in states, str(wf.get("initial_state")))
        for terminal in wf.get("terminal_states", []):
            add(f"terminal-known:{terminal}", terminal in states, terminal)

        # Every transition source/target is a declared state.
        bad_states = []
        for src, targets in transitions.items():
            if src not in states:
                bad_states.append(src)
            for tgt in targets:
                if tgt not in states:
                    bad_states.append(tgt)
        add("transitions-reference-known-states", not bad_states, f"unknown: {sorted(set(bad_states))}")

        # Every gate edge is a declared transition and names a known gate.
        bad_edges = []
        for edge, gate in gate_edges.items():
            src, _, tgt = edge.partition("->")
            if tgt not in transitions.get(src, []):
                bad_edges.append(f"{edge} (not a transition)")
            if gate not in gate_map:
                bad_edges.append(f"{edge} (gate '{gate}' not in human_gate_to_state)")
        add("gate-edges-valid", not bad_edges, f"{bad_edges}")

        # Every gate maps to a declared state.
        bad_gate_states = [g for g, s in gate_map.items() if s not in states]
        add("gate-states-known", not bad_gate_states, f"{bad_gate_states}")

        # Every gate-report edge is a declared transition (Phase 6 added several).
        bad_report_edges = []
        for edge in wf.get("gate_reports", {}):
            src, _, tgt = edge.partition("->")
            if tgt not in transitions.get(src, []):
                bad_report_edges.append(edge)
        add("gate-report-edges-valid", not bad_report_edges, f"{bad_report_edges}")
    except Exception as exc:  # noqa: BLE001
        add("workflow-states", False, str(exc))

    # 3. Distribution config coherence (Phase 6). Configs are optional; validate when present.
    cfg_dir = root / ".claude" / "config"
    channels_path = cfg_dir / "channels.config.json"
    if channels_path.is_file():
        try:
            channels_cfg = load_json(channels_path)
            errs = validate_data(root, channels_cfg, "channels-config.schema.json")
            add("channels-config-schema", not errs, "; ".join(errs) or "valid")
            # Reject two default:true channels for the same (platform, language).
            seen_defaults: dict[tuple[str, str], int] = {}
            for ch in channels_cfg.get("channels", []):
                if ch.get("default"):
                    key = (ch.get("platform", ""), ch.get("language", ""))
                    seen_defaults[key] = seen_defaults.get(key, 0) + 1
            dupes = [f"{p}/{lang}" for (p, lang), n in seen_defaults.items() if n > 1]
            add("channels-single-default-per-language", not dupes,
                f"multiple default channels for: {dupes}" if dupes else "one default per platform/language")
        except Exception as exc:  # noqa: BLE001
            add("channels-config", False, str(exc))

    promo_path = cfg_dir / "promotion.config.json"
    if promo_path.is_file():
        try:
            promo_cfg = load_json(promo_path)
            errs = validate_data(root, promo_cfg, "promotion-config.schema.json")
            add("promotion-config-schema", not errs, "; ".join(errs) or "valid")
            # An automatable platform marked enabled must name a credentials_ref.
            missing_creds = [
                name for name, p in promo_cfg.get("platforms", {}).items()
                if p.get("automatable") and p.get("enabled") and not p.get("credentials_ref")
            ]
            add("promotion-enabled-have-credentials", not missing_creds,
                f"enabled automatable platforms missing credentials_ref: {missing_creds}"
                if missing_creds else "all enabled platforms reference credentials")
        except Exception as exc:  # noqa: BLE001
            add("promotion-config", False, str(exc))

    valid = all(c["status"] == "pass" for c in checks)
    return {"valid": valid, "checks": checks}
