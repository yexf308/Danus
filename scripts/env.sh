#!/usr/bin/env bash
# =============================================================================
# Danus environment — source this in any shell that talks to the system.
#
#   source <repo>/scripts/env.sh
#
# Resolves all paths from config/codex.env + config/danus.env + runtime/
# runtime.env, then exports sane defaults and puts bin/ + the provisioned node +
# venv on PATH. The bin/ wrappers source this for you, so `danus`, `consult`,
# `codex` work without you sourcing it manually. Sourcing twice is harmless
# (idempotent).
# =============================================================================

# Self-locate the repo root (this file lives at <repo>/scripts/env.sh).
DANUS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
export DANUS_ROOT

# 0) codex backend (BYO OpenAI-compatible endpoint + key; gitignored)
if [ -f "$DANUS_ROOT/config/codex.env" ]; then
  set -a; . "$DANUS_ROOT/config/codex.env"; set +a
fi

# 1) user config (accounts / models / ports / toggles) — overrides the codex file
if [ -f "$DANUS_ROOT/config/danus.env" ]; then
  set -a; . "$DANUS_ROOT/config/danus.env"; set +a
fi

# 2) machine-derived paths written by bootstrap.sh (node, codex.js, venv)
if [ -f "$DANUS_ROOT/runtime/runtime.env" ]; then
  set -a; . "$DANUS_ROOT/runtime/runtime.env"; set +a
fi

# 3) defaults for anything still unset
export DANUS_RUNTIME="${DANUS_RUNTIME:-$DANUS_ROOT/runtime}"
export DANUS_AGENTS_ROOT="${DANUS_AGENTS_ROOT:-$DANUS_RUNTIME/projects}"
export VERIFIER_RESULTS_DIR="${VERIFIER_RESULTS_DIR:-$DANUS_RUNTIME/verify-runs}"
export CODEX_HOME="${CODEX_HOME:-$DANUS_RUNTIME/codex-home}"
export VERIFY_PORT="${VERIFY_PORT:-8091}"
export DASHBOARD_PORT="${DASHBOARD_PORT:-8099}"
export DANUS_VERIFY_URL="${DANUS_VERIFY_URL:-http://127.0.0.1:${VERIFY_PORT}/verify}"
export DANUS_CODEX_MODEL="${DANUS_CODEX_MODEL:-${CODEX_API_MODEL:-gpt-5.6-sol}}"   # neutral default model for every codex call (defers to the api backend model)
export DANUS_CODEX_EFFORT="${DANUS_CODEX_EFFORT:-xhigh}"   # neutral default reasoning effort
export DANUS_CONSULT_TRANSPORT="${DANUS_CONSULT_TRANSPORT:-off}"   # optional: off | gpt_pro | claude_api | claude_code; browser is recommendation+owner-only
export DANUS_CHROME_BIN="${DANUS_CHROME_BIN:-}"        # headless Chrome/Chromium for human-summary PDF (empty = auto-detect)
export CODEX_BACKEND="${CODEX_BACKEND:-api}"            # api (BYO key) | chatgpt (your login)

# 4) PATH: bin wrappers first, then the provisioned node + venv (if bootstrapped)
_danus_path="$DANUS_ROOT/bin"
[ -n "${DANUS_NODE_BIN:-}" ] && [ -d "$DANUS_NODE_BIN" ] && _danus_path="$_danus_path:$DANUS_NODE_BIN"
[ -n "${DANUS_VENV:-}" ]     && [ -d "$DANUS_VENV/bin" ] && _danus_path="$_danus_path:$DANUS_VENV/bin"
case ":$PATH:" in *":$_danus_path:"*) : ;; *) export PATH="$_danus_path:$PATH" ;; esac

# 5) the python the engine runs on (venv if bootstrapped, else system python3)
if [ -n "${DANUS_VENV:-}" ] && [ -x "$DANUS_VENV/bin/python" ]; then
  export DANUS_PY="$DANUS_VENV/bin/python"
else
  export DANUS_PY="${DANUS_PY:-$(command -v python3 || true)}"
fi

# silent unless DANUS_ENV_VERBOSE=1
if [ "${DANUS_ENV_VERBOSE:-0}" = "1" ]; then
  echo "DANUS_ROOT=$DANUS_ROOT"
  echo "DANUS_PY=$DANUS_PY"
  echo "DANUS_AGENTS_ROOT=$DANUS_AGENTS_ROOT"
  echo "DANUS_VERIFY_URL=$DANUS_VERIFY_URL"
  echo "CODEX_HOME=$CODEX_HOME"
  echo "consult transport=$DANUS_CONSULT_TRANSPORT"
fi

# 6) verify-service guardian + health probe (shared by doctor/services/recover).
# The Python guardian authenticates a bounded 0600 Unix-socket control response,
# then requires a <=4 KiB HTTP 200 JSON body with the same random instance nonce,
# child PID, verifier protocol and pinned bundle digest.  No shell PID parsing,
# sed extraction, unbounded curl body, or numeric signal is involved.
#
#   ours (0), foreign (3), unsafe/cleanup_in_progress (4), down (5)
danus_verify_health(){
  local url="http://127.0.0.1:${VERIFY_PORT}/health"
  local pf="$DANUS_RUNTIME/run/verify.pid"
  local helper="$DANUS_ROOT/scripts/service-identity.py"
  local result rc state
  result="$("$DANUS_PY" -I -B "$helper" verify-health "$pf" "$url" 2>/dev/null)"
  rc=$?
  state="$(printf '%s' "$result" | "$DANUS_PY" -I -c \
    'import json,sys
try: print(json.load(sys.stdin).get("state", "unsafe"))
except Exception: print("unsafe")' 2>/dev/null || echo unsafe)"
  case "$state" in
    ours) echo ours; return 0 ;;
    foreign) echo foreign; return 3 ;;
    cleanup_in_progress) echo cleanup_in_progress; return 4 ;;
    down) echo down; return 5 ;;
    *) echo unsafe; return "${rc:-4}" ;;
  esac
}
