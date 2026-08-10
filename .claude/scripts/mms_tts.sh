#!/usr/bin/env bash
# Drop-in MMS-TTS synthesizer at the engines/tts.py binary_overrides.mms seam.
# Runs the single-cue synthesizer under .venv-xtts (which has torch/transformers). Forwards argv
# verbatim. HF is policy-blocked here, so force the OFFLINE staged cache (OPERATING-GUIDE §6):
# models are staged under HF_HOME with HF_HUB_OFFLINE=1 so no network is touched.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${here}/../.." && pwd)"
export HF_HOME="${HF_HOME:-$HOME/Dev/my-repos/pub/.cache/huggingface}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
exec "${root}/.venv-xtts/bin/python3" "${root}/.venv-xtts/mms_tts.py" "$@"
