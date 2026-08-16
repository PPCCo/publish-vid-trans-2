"""Load and merge yt-app configuration.

Two config surfaces:
  * ``yt-app/config/defaults.json`` (+ an optional gitignored
    ``yt-app/config/defaults.local.json`` deep-merged over it) — the batch defaults
    applied to every video unless a per-video ``overrides`` block replaces them.
  * The framework company/tools config (``load_company_config`` / ``load_tools_config``),
    read live so machine-local voices / clone_languages / quality bars are honored — we
    never hardcode those (they live in the gitignored company.local.json).

Per-video effective config = ``deep_merge(defaults, entry.get("overrides", {}))`` using
the framework's own ``deep_merge`` (dict-merge recursively, list/scalar replace) so
list-valued keys like ``targetLangs`` are REPLACED wholesale, not concatenated.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Directory holding this package's own config files.
_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def defaults_path() -> Path:
    return _CONFIG_DIR / "defaults.json"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    from video_translation_house.util import deep_merge  # lazy: needs sys.path wired

    return deep_merge(base, override)


def load_defaults() -> dict[str, Any]:
    """Load ``config/defaults.json``, deep-merging an optional ``defaults.local.json``."""
    from video_translation_house.util import load_json  # lazy

    default = load_json(defaults_path())
    local = _CONFIG_DIR / "defaults.local.json"
    if local.exists():
        return _deep_merge(default, load_json(local))
    return default


def effective_config(defaults: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Merge a batch-list entry's ``overrides`` over the shared defaults.

    Uses the framework ``deep_merge`` so nested dicts (e.g. ``per_language_provider``,
    ``playback_speed``) merge key-by-key while lists/scalars replace. ``id`` and ``url``
    on the entry are carried onto the result for convenience.
    """
    overrides = entry.get("overrides") or {}
    merged = _deep_merge(defaults, overrides)
    if entry.get("id"):
        merged["id"] = entry["id"]
    if entry.get("url"):
        merged["url"] = entry["url"]
    return merged


def company_config(root: Path) -> dict[str, Any]:
    from video_translation_house.util import load_company_config  # lazy

    return load_company_config(root)


def tools_config(root: Path) -> dict[str, Any]:
    from video_translation_house.util import load_tools_config  # lazy

    return load_tools_config(root)
