"""Repo-global spend guard for billed vendor engine calls: budget/ledger.json.

Plan §6 calls for "a cost-guard hook that blocks any external-spend-incurring call
without an explicit human-set budget ceiling." Local engines (kokoro/piper/xtts/
chatterbox, mlx-whisper/faster-whisper) never touch this — only *vendor* providers
(elevenlabs/azure/google TTS today; any future billed integration) are guarded.

The ceiling is a human-set config value (`company.default.json`'s `budget.
vendor_spend_ceiling_usd`, overridable per-repo in `company.local.json`) — an agent can
read it but never raise it, same posture as rights_status/voice_clone_consent. Spend is
an estimate (`budget.vendor_call_estimated_cost_usd[provider]`), not a metered vendor
invoice; it exists to make runaway vendor usage impossible by default (ceiling is 0.0
unless a human raises it), not to be pfennig-accurate.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import BudgetExceededError
from .util import atomic_write_json, load_company_config, load_json, project_lock, utc_now

# Providers that incur third-party spend. Kept alongside engines/tts.py's KNOWN_PROVIDERS
# rather than imported from it so this module has no dependency on any engine adapter.
VENDOR_PROVIDERS = {"elevenlabs", "azure", "google"}


def ledger_path(root: Path) -> Path:
    return root / "budget" / "ledger.json"


def _lock_path(root: Path) -> Path:
    return root / "budget" / ".lock"


def _load(root: Path) -> dict[str, Any]:
    data = load_json(ledger_path(root), {"schema_version": "1.0", "calls": [], "spent_usd": 0.0})
    data.setdefault("calls", [])
    data.setdefault("spent_usd", 0.0)
    return data


def is_vendor_provider(provider: str) -> bool:
    return provider.strip().lower() in VENDOR_PROVIDERS


def ceiling_usd(root: Path) -> float:
    return float(load_company_config(root).get("budget", {}).get("vendor_spend_ceiling_usd", 0.0))


def estimated_cost_usd(root: Path, provider: str) -> float:
    costs = load_company_config(root).get("budget", {}).get("vendor_call_estimated_cost_usd", {})
    return float(costs.get(provider.strip().lower(), 0.0))


def spent_usd(root: Path) -> float:
    return float(_load(root).get("spent_usd", 0.0))


def status(root: Path) -> dict[str, Any]:
    """Read-only summary: ceiling, running total, and remaining headroom."""
    spent = spent_usd(root)
    ceiling = ceiling_usd(root)
    return {
        "vendor_spend_ceiling_usd": ceiling,
        "spent_usd": spent,
        "remaining_usd": round(ceiling - spent, 6),
        "vendor_providers": sorted(VENDOR_PROVIDERS),
    }


def check_and_record(
    root: Path,
    provider: str,
    *,
    project_id: str | None = None,
    language: str | None = None,
    actor: str = "agent",
) -> dict[str, Any]:
    """Refuse a vendor call that would exceed the human-set ceiling; else record its
    estimated cost against the ledger and return the updated status.

    A no-op for non-vendor providers (local engines are never spend-guarded).
    """
    if not is_vendor_provider(provider):
        return {"guarded": False, "provider": provider}

    cost = estimated_cost_usd(root, provider)
    ceiling = ceiling_usd(root)
    with project_lock(_lock_path(root)):
        ledger = _load(root)
        spent = float(ledger["spent_usd"])
        if spent + cost > ceiling:
            raise BudgetExceededError(
                f"vendor provider {provider!r} would cost an estimated ${cost:.2f}, taking "
                f"spend to ${spent + cost:.2f} against a ${ceiling:.2f} ceiling. A human must "
                f"raise budget.vendor_spend_ceiling_usd in company.local.json before this "
                f"vendor call is permitted; local engines remain unguarded."
            )
        ledger["spent_usd"] = round(spent + cost, 6)
        ledger["calls"].append({
            "time": utc_now(), "provider": provider, "estimated_cost_usd": cost,
            "project_id": project_id, "language": language, "actor": actor,
        })
        ledger_path(root).parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(ledger_path(root), ledger)
    return {"guarded": True, "provider": provider, "estimated_cost_usd": cost,
            "spent_usd": ledger["spent_usd"], "ceiling_usd": ceiling}
