#!/usr/bin/env bash
#
# run_pipeline.sh — one-command scripted transcribe->translate->dub run (TASK 1 / token-thrifty
# manual route). Drives `vid_cli.py project autopilot`, which runs every DETERMINISTIC pipeline
# step back-to-back with NO Claude tokens, and stops the moment a human gate / blocker is hit.
#
# What it does NOT do (by design, mirrors the standing rules):
#   * It never crosses a human gate or grants an approval — it stops AT the gate (rules 2/13).
#   * It never uploads / publishes (rule 3). The pipeline halts at packaging / READY_FOR_REVIEW.
#   * With --mt it fills translation worksheets + the English gloss via the MT engine (no Claude);
#     without --mt it stops at TRANSLATION and tells you to use --mt or the Claude route.
#
# When it stops, tell your Claude session:  "the manual process for <id> is done"
# and Claude runs the editorial QA + walks you through each pending human gate (the /process-manual
# skill, resume mode). That QA is the ONLY place Claude spends tokens on this route.
#
# Usage:
#   .claude/scripts/run_pipeline.sh <project-id> [--mt] [--until <STATE>] [--dry-run]
#
# Idempotent: safe to re-run — it resumes from wherever state.json currently sits.

set -euo pipefail

# --- resolve paths -----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CLI="$SCRIPT_DIR/vid_cli.py"

# Prefer the project venv (has jsonschema/yaml etc.); fall back to python3 on PATH.
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PY="$REPO_ROOT/.venv/bin/python"
else
  PY="$(command -v python3 || true)"
fi
if [[ -z "${PY:-}" ]]; then
  echo "run_pipeline: no python interpreter found (.venv/bin/python or python3)" >&2
  exit 2
fi

# --- args --------------------------------------------------------------------
if [[ $# -lt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "usage: run_pipeline.sh <project-id> [--mt] [--until <STATE>] [--dry-run]" >&2
  exit 2
fi
PROJECT_ID="$1"; shift
AUTOPILOT_ARGS=("$PROJECT_ID")
for a in "$@"; do AUTOPILOT_ARGS+=("$a"); done

# --- network posture ---------------------------------------------------------
# Media steps (ingest/ensure/clone-dub/mux-refetch) need VIDTRANS_FETCH_ENABLED=1 (rule 3). We
# enable it here because a full scripted run may include ingest; announce it plainly. Playlist
# enumeration is flag-free but that's a separate verb, not this pipeline.
export VIDTRANS_FETCH_ENABLED=1

# Corporate TLS inspection (Prisma Access): the working CA bundle for egress here is
# ~/certs/aipe-certs.pem (project memory: proxy-ca-bundle). Only set if unset and present, so an
# already-configured environment is respected.
_CA="$HOME/certs/aipe-certs.pem"
if [[ -f "$_CA" ]]; then
  export SSL_CERT_FILE="${SSL_CERT_FILE:-$_CA}"
  export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-$_CA}"
fi

echo "run_pipeline: project=$PROJECT_ID  (VIDTRANS_FETCH_ENABLED=1 — media downloads permitted)"
echo "run_pipeline: driving deterministic steps until the next human gate / blocker ..."
echo

# --- run ---------------------------------------------------------------------
# Autopilot itself loops to the next gate; one invocation is enough. Capture JSON, pretty-print,
# then extract the stop kind/reason for a human-readable footer.
OUT="$("$PY" "$CLI" project autopilot "${AUTOPILOT_ARGS[@]}")"
echo "$OUT"
echo

# --- footer: what stopped it + the resume hint ------------------------------
# Pass pid + the JSON as argv — `python - <<EOF` would claim stdin for the program text, so we
# use a temp program file and feed the data through arguments instead.
FOOTER_PY="$(mktemp -t autopilot_footer.XXXXXX.py)"
trap 'rm -f "$FOOTER_PY"' EXIT
cat > "$FOOTER_PY" <<'PYEOF'
import json, sys
pid = sys.argv[1]
data = json.loads(sys.argv[2])
kind = data.get("stop_kind", "?")
reason = data.get("stop_reason", "")
state = data.get("current_state", "?")
print(f"── stopped: {kind} @ {state}")
print(f"   {reason}")
gate = data.get("required_gate")
if kind == "GATE" or gate:
    print()
    print("   A human gate is waiting. In your Claude session, say:")
    print(f'      "the manual process for {pid} is done"')
    print("   Claude will run editorial QA and walk you through the gate (rule 13).")
elif kind == "BLOCKED":
    print()
    print("   Blocked — resolve the blocker above (e.g. set rights, install an MT engine), then re-run.")
elif kind == "TERMINAL":
    print()
    print(f"   {pid} reached a terminal state — nothing more for the scripted route to do.")
elif kind == "HALT":
    print()
    print("   Halted before a gate (see reason). Fix it (e.g. add --mt, confirm source language), then re-run.")
PYEOF
"$PY" "$FOOTER_PY" "$PROJECT_ID" "$OUT"
