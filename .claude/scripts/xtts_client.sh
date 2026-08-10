#!/usr/bin/env bash
# Drop-in replacement for the Coqui `tts` CLI at the engines/tts.py binary_overrides.xtts seam.
# Runs the stdlib-only client under the .venv-xtts interpreter (which has TTS/torch for the daemon
# it spawns; the client itself imports only the stdlib). Forwards all argv verbatim.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${here}/../.." && pwd)"
exec "${root}/.venv-xtts/bin/python3" \
  "${root}/.claude/scripts/video_translation_house/engines/_xtts_client.py" "$@"
