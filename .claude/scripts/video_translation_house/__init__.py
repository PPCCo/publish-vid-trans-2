"""Deterministic support tooling for the publish-vid-trans framework.

The filesystem is the database; this package owns all state mutation, hashing,
validation, and lifecycle enforcement. Agents and skills call the CLI (vid_cli.py)
rather than writing project files directly, so state can never be silently corrupted.
"""

__version__ = "0.1.0"
