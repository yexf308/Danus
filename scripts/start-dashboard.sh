#!/usr/bin/env bash
# Read-only dashboard for one project (fact graph DAG + global memory + spend).
#   bash scripts/start-dashboard.sh <project_name> [port]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../scripts/env.sh"
NAME="${1:?usage: start-dashboard.sh <project_name> [port]}"
PORT="${2:-$DASHBOARD_PORT}"
case "$NAME" in
  *[!A-Za-z0-9._-]*|""|[!A-Za-z0-9]*)
    echo "invalid project name: $NAME" >&2
    exit 1
    ;;
esac
PROJ="$("$DANUS_PY" -I -B "$HERE/service-identity.py" \
  project-dir "$DANUS_AGENTS_ROOT" "$NAME")" || exit 1
echo "[dashboard] http://127.0.0.1:$PORT  project=$NAME"
exec "$DANUS_PY" -m danus.observability --project "$PROJ" --port "$PORT"
