#!/usr/bin/env bash
# yt-app launcher — the standalone (non-Claude) A/V pipeline CLI.
#
# Resolves the repo root from this script's own location (yt-app/run.sh -> repo root),
# then execs the repo venv's python on cli.py. Using the venv interpreter directly is
# mandatory: a bare `python3` resolves to the system interpreter that lacks the venv's
# deps (jsonschema/yaml/piper/…) and is hook-blocked besides.
#
# lib/env.py::bootstrap() self-configures the fetch flag / proxy CA bundle / HF-offline
# env at import time, so no env preamble is needed here — but an operator's explicit env
# always wins (bootstrap only sets a var when it is unset and its target exists).
#
# Usage:  ./run.sh <verb> [args]      e.g.  ./run.sh probe clip.mp4
#                                            ./run.sh batch <id>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PY="${REPO_ROOT}/.venv/bin/python3"

if [[ ! -x "${PY}" ]]; then
  echo "run.sh: venv python not found at ${PY}" >&2
  echo "        (expected the publish-vid-trans repo venv one level up from yt-app/)" >&2
  exit 1
fi

# Make the repo root explicit for env.bootstrap() (it can also auto-discover it).
export VIDTRANS_REPO_ROOT="${REPO_ROOT}"

exec "${PY}" "${SCRIPT_DIR}/cli.py" "$@"
