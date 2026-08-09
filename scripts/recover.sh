#!/usr/bin/env bash
# =============================================================================
# recover.sh — bring Danus back after a host restart, near-losslessly.
#
#   bash scripts/recover.sh
#
# All memory/state lives under this repo (+ runtime/): codex auth, every
# project's fact graph + global memory, OPERATOR.md. Recovery only (1) rebuilds
# the toolchain (notably the venv, whose base interpreter can go dangling if the
# host python moved) and (2) restarts the services that were running. Idempotent;
# safe to run anytime.
# =============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/env.sh"

echo "== [1/4] rebuild toolchain (bootstrap: validates/recreates the venv, codex provider) =="
bash "$HERE/bootstrap.sh" || { echo "recover: bootstrap failed — fix that first"; exit 1; }

echo "== [2/4] reconcile service identities =="
recover_rc=0
bash "$HERE/services.sh" recover-stale || {
  echo "recover: an unsafe guardian identity needs manual reconciliation" >&2
  recover_rc=1
}

echo "== [3/4] restart the services that were running =="
AUTO_SNAPSHOT="$(bash "$HERE/services.sh" manifest-snapshot)" || {
  echo "recover: could not read a consistent safe autostart snapshot" >&2
  AUTO_SNAPSHOT=""
  recover_rc=1
}
if [ -n "$AUTO_SNAPSHOT" ]; then
  while IFS='|' read -r entry generation; do
    [ -n "$entry" ] || continue
    echo "  -> services.sh recover-up $entry"
    case "$entry" in
      verify) bash "$HERE/services.sh" recover-up "$entry" "$generation" || recover_rc=1 ;;
      "dashboard "*)
        project="${entry#dashboard }"
        case "$project" in
          *[!A-Za-z0-9._-]*|""|[!A-Za-z0-9]*)
            echo "recover: unsafe dashboard autostart entry" >&2
            recover_rc=1
            continue
            ;;
        esac
        bash "$HERE/services.sh" recover-up "$entry" "$generation" || recover_rc=1
        ;;
      *) echo "recover: unsafe autostart entry" >&2; recover_rc=1 ;;
    esac
  done <<< "$AUTO_SNAPSHOT"
else
  echo "  (no autostart manifest — nothing was recorded as running)"
  echo "  the verify service is required before workers can submit facts:"
  echo "     bash scripts/services.sh up verify"
fi

echo "== [4/4] health =="
if [ "${CODEX_BACKEND:-api}" = "api" ]; then
  bash "$HERE/check-codex.sh" 2>/dev/null | sed 's/^/  codex: /' || true
else
  env CODEX_HOME="$CODEX_HOME" "$DANUS_ROOT/bin/codex" login status >/dev/null 2>&1 \
    && echo "  codex: login ok (chatgpt, $CODEX_HOME)" \
    || echo "  codex: NOT logged in (scripts/setup-codex.sh login)"
fi
bash "$HERE/services.sh" status || recover_rc=1
bash "$HERE/services.sh" test || recover_rc=1
if [ "$recover_rc" -ne 0 ]; then
  echo "recover: completed with one or more reconciliation/start/health failures" >&2
  exit "$recover_rc"
fi
echo "done — recovery complete and health-gated."
