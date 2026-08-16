"""Repo-root discovery + sys.path wiring + environment self-configuration.

``bootstrap()`` is the FIRST thing ``cli.py`` calls and the first thing every entry
point into this package should call. It:

  1. Locates the publish-vid-trans repo root (``VIDTRANS_REPO_ROOT`` or by walking up
     from this file to a ``.claude/CLAUDE.md``).
  2. Inserts ``<root>/.claude/scripts`` onto ``sys.path`` so
     ``import video_translation_house...`` works.
  3. Self-configures the environment the framework's leaf functions expect — but only
     sets a var when it is currently UNSET and the referenced file/dir actually exists,
     mirroring ``run_pipeline.sh``. It never clobbers an operator's explicit setting.

Everything is idempotent: calling ``bootstrap()`` twice is a no-op after the first.

macOS only (per the yt-app contract). The env knobs:
  * ``VIDTRANS_FETCH_ENABLED=1`` — gate for media downloads (yt-dlp). Playlist
    enumeration is flag-free and works regardless.
  * ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE=~/certs/aipe-certs.pem`` — the Prisma
    Access TLS-inspection CA bundle (egress here fails without it).
  * ``HF_HOME=<repo-parent>/.cache/huggingface`` + ``HF_HUB_OFFLINE=1`` — HuggingFace
    is policy-blocked; models are staged offline into this cache.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_STATE: dict[str, object] = {"root": None, "done": False}


def _discover_root() -> Path:
    """Locate the repo root: env override, else walk up from this file."""
    configured = os.environ.get("VIDTRANS_REPO_ROOT")
    if configured:
        root = Path(configured).expanduser().resolve()
        if (root / ".claude" / "CLAUDE.md").is_file():
            return root
        raise RuntimeError(
            f"VIDTRANS_REPO_ROOT is set but has no .claude/CLAUDE.md: {root}"
        )
    # yt-app/lib/env.py -> yt-app -> <repo root>
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / ".claude" / "CLAUDE.md").is_file():
            return candidate
    raise RuntimeError(
        "could not locate the publish-vid-trans repo root (no .claude/CLAUDE.md found "
        "walking up from yt-app/); set VIDTRANS_REPO_ROOT to point at it."
    )


def _set_if_unset_and_present(var: str, value: str, *, must_exist: bool) -> None:
    """Set ``os.environ[var]=value`` only if unset and (optionally) the path exists.

    Mirrors run_pipeline.sh: we never override an operator's explicit env, and we don't
    point a var at a file/dir that isn't there (which would just break the tool with a
    confusing error). ``must_exist=False`` is for pure flags like VIDTRANS_FETCH_ENABLED.
    """
    if os.environ.get(var):
        return
    if must_exist and not Path(value).expanduser().exists():
        return
    os.environ[var] = value


def _self_configure_env(root: Path) -> None:
    """Set the framework's expected env vars where unset-and-present (idempotent)."""
    # Media downloads are gated on this flag. yt-app is a local operator tool that DOES
    # download, so default it on (still overridable by the operator setting it to 0/"").
    _set_if_unset_and_present("VIDTRANS_FETCH_ENABLED", "1", must_exist=False)

    # Prisma Access TLS-inspection CA bundle — egress fails without it. Same path both
    # env names the framework's HTTP layers read.
    ca_bundle = str(Path("~/certs/aipe-certs.pem").expanduser())
    _set_if_unset_and_present("SSL_CERT_FILE", ca_bundle, must_exist=True)
    _set_if_unset_and_present("REQUESTS_CA_BUNDLE", ca_bundle, must_exist=True)

    # HuggingFace is policy-blocked; models are staged offline into a sibling cache of the
    # repo (i.e. <repo-parent>/.cache/huggingface, e.g. ~/Dev/my-repos/pub/.cache/...).
    hf_home = root.parent / ".cache" / "huggingface"
    _set_if_unset_and_present("HF_HOME", str(hf_home), must_exist=True)
    # Only force offline mode if we actually have a staged cache to be offline against;
    # otherwise leave it unset so a machine WITH HF access still works.
    if os.environ.get("HF_HOME"):
        _set_if_unset_and_present("HF_HUB_OFFLINE", "1", must_exist=False)


def bootstrap() -> Path:
    """Discover the repo root, wire sys.path, self-configure env. Returns the root.

    Idempotent — safe to call from every entry point; only the first call does work.
    """
    if _STATE["done"]:
        return _STATE["root"]  # type: ignore[return-value]
    root = _discover_root()
    scripts = root / ".claude" / "scripts"
    scripts_str = str(scripts)
    if scripts_str not in sys.path:
        sys.path.insert(0, scripts_str)
    _self_configure_env(root)
    _STATE["root"] = root
    _STATE["done"] = True
    return root


def repo_root() -> Path:
    """The bootstrapped repo root (bootstraps on first use)."""
    if not _STATE["done"]:
        return bootstrap()
    return _STATE["root"]  # type: ignore[return-value]
