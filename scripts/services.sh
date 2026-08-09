#!/usr/bin/env bash
# Danus resident-service control.  All lifecycle authority lives in the Python
# guardian: this shell never sends a signal to a numeric PID or process group.
#
#   bash scripts/services.sh up verify
#   bash scripts/services.sh up dashboard <project>
#   bash scripts/services.sh down verify|dashboard|all
#   bash scripts/services.sh status|test
#   bash scripts/services.sh logs <verify|dashboard-project>  # bounded snapshot
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/env.sh"
RUN="$DANUS_RUNTIME/run"
LOG="$DANUS_RUNTIME/logs"
GUARDIAN="$HERE/service-identity.py"
AUTO="$RUN/autostart"
AUTO_LOCK="$RUN/autostart.lock"

"$DANUS_PY" -I -B "$GUARDIAN" prepare "$RUN" "$LOG" || exit 1

_safe_segment(){
  [ -n "${1:-}" ] && [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]
}
_validate_project(){
  _safe_segment "${1:-}" || {
    echo "invalid project name: ${1:-}" >&2
    return 1
  }
}
_validate_service(){
  case "${1:-}" in
    verify) return 0 ;;
    dashboard-*) _validate_project "${1#dashboard-}" ;;
    *) echo "invalid service name: ${1:-}" >&2; return 1 ;;
  esac
}
_record(){ _validate_service "$1" >/dev/null || return 1; echo "$RUN/$1.pid"; }
_lock(){ _validate_service "$1" >/dev/null || return 1; echo "$RUN/$1.lock"; }
_socket(){ _validate_service "$1" >/dev/null || return 1; echo "$RUN/$1.sock"; }
_log(){ _validate_service "$1" >/dev/null || return 1; echo "$LOG/$1.log"; }

_json_field(){
  local field="$1"
  "$DANUS_PY" -I -c \
    'import json,sys; v=json.load(sys.stdin); x=v.get(sys.argv[1]); print("" if x is None else x)' \
    "$field"
}

_manifest_add(){
  "$DANUS_PY" -I -B "$GUARDIAN" manifest add "$AUTO" "$AUTO_LOCK" "$1"
}
_manifest_del(){
  "$DANUS_PY" -I -B "$GUARDIAN" manifest del "$AUTO" "$AUTO_LOCK" "$1"
}
_manifest_rollback(){
  "$DANUS_PY" -I -B "$GUARDIAN" manifest rollback "$AUTO" "$AUTO_LOCK" "$1" "$2"
}
_manifest_lines(){
  "$DANUS_PY" -I -B "$GUARDIAN" manifest lines "$AUTO" "$AUTO_LOCK"
}

_entry_for(){
  case "$1" in
    verify) echo verify ;;
    dashboard-*) echo "dashboard ${1#dashboard-}" ;;
    *) return 1 ;;
  esac
}

_verifier_contract(){
  "$DANUS_PY" -I -B "$GUARDIAN" verifier-contract "$DANUS_ROOT"
}

_start_guardian(){
  local name="$1" recovery_entry="$2" recovery_generation="$3"
  local timeout="${DANUS_SERVICE_START_TIMEOUT:-15}"
  local health_kind health_url protocol digest contract project output rc
  local attempt max_attempts status_output status_state status_generation
  case "$timeout" in
    *[!0-9]*|"") timeout=15 ;;
  esac
  [ "$timeout" -ge 1 ] && [ "$timeout" -le 60 ] || timeout=15
  case "$name" in
    verify)
      contract="$(_verifier_contract)" || return 1
      protocol="$(printf '%s' "$contract" | _json_field protocol)" || return 1
      digest="$(printf '%s' "$contract" | _json_field digest)" || return 1
      health_kind=verify
      health_url="http://127.0.0.1:${VERIFY_PORT}/health"
      set -- bash "$DANUS_ROOT/scripts/start-verify.sh"
      ;;
    dashboard-*)
      project="${name#dashboard-}"
      _validate_project "$project" || return 1
      protocol=-
      digest=-
      health_kind=dashboard
      health_url="http://127.0.0.1:${DASHBOARD_PORT}/health"
      set -- bash "$DANUS_ROOT/scripts/start-dashboard.sh" "$project"
      ;;
    *) return 1 ;;
  esac
  attempt=0
  max_attempts=$((timeout * 10 + 10))
  while [ "$attempt" -lt "$max_attempts" ]; do
    output="$("$DANUS_PY" -I -B "$GUARDIAN" start \
      "$(_record "$name")" "$(_lock "$name")" "$(_socket "$name")" \
      "$name" "$(_log "$name")" "$timeout" "$health_kind" "$health_url" \
      "$protocol" "$digest" "$AUTO" "$AUTO_LOCK" \
      "${recovery_entry:--}" "${recovery_generation:--}" -- "$@")"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      case "$(printf '%s' "$output" | _json_field result 2>/dev/null || true)" in
        started)
          echo "[$name] up (guardian-authenticated; log: runtime/logs/$name.log)"
          return 0
          ;;
        skipped)
          echo "[$name] recovery skipped (manifest intent absent or replaced)"
          return 0
          ;;
      esac
    fi
    if [ "$rc" -ne 75 ]; then
      break
    fi
    status_output="$("$DANUS_PY" -I -B "$GUARDIAN" status \
      "$(_record "$name")" 2>/dev/null || true)"
    status_state="$(printf '%s' "$status_output" | _json_field state 2>/dev/null || true)"
    status_generation="$(printf '%s' "$status_output" | \
      _json_field intent_generation 2>/dev/null || true)"
    if [ "$status_state" = ready ] && \
       [ "$status_generation" = "${recovery_generation:--}" ]; then
      echo "[$name] already up under the same intent generation"
      return 0
    fi
    # A live guardian for an older generation must yield first.  Retrying is
    # bounded and each attempt re-enters through the guardian-held lock.
    sleep 0.1
    attempt=$((attempt + 1))
  done
  echo "[$name] FAILED to become ready: $output" >&2
  "$DANUS_PY" -I -B "$GUARDIAN" read-log "$(_log "$name")" 2>/dev/null || true
  return 1
}

_up(){
  local name="$1" entry manifest_result manifest_state generation
  _validate_service "$name" || return 1
  entry="$(_entry_for "$name")" || return 1
  # Durable desired-state intent is fsynced before any child is launched.
  manifest_result="$(_manifest_add "$entry")" || return 1
  manifest_state="$(printf '%s' "$manifest_result" | _json_field state)" || return 1
  generation="$(printf '%s' "$manifest_result" | _json_field generation)" || return 1
  # Even an explicit up revalidates this exact intent generation while its new
  # guardian owns the service lock.  Thus add -> concurrent down -> launch
  # cannot resurrect a service whose desired state is already absent.
  if _start_guardian "$name" "$entry" "$generation"; then
    return 0
  fi
  # Roll back only the generation created by this invocation.  A pre-existing
  # or concurrently replaced intent is never deleted by a failed launch.
  if [ "$manifest_state" = added ]; then
    _manifest_rollback "$entry" "$generation" >/dev/null || {
      echo "[$name] launch failed and its intent rollback failed; recovery intent retained" >&2
      return 1
    }
  fi
  return 1
}

_down_one(){
  local name="$1" entry rc=0 manifest_result generation stop_result stop_state
  _validate_service "$name" || return 1
  entry="$(_entry_for "$name")" || return 1
  # Desired state becomes durably absent before a stop request can be sent.
  manifest_result="$(_manifest_del "$entry")" || return 1
  generation="$(printf '%s' "$manifest_result" | _json_field generation)" || return 1
  [ -n "$generation" ] || generation=-
  stop_result="$("$DANUS_PY" -I -B "$GUARDIAN" stop \
    "$(_record "$name")" "$(_lock "$name")" "${DANUS_SERVICE_STOP_TIMEOUT:-8}" \
    "$generation")" || rc=1
  if [ "$rc" -eq 0 ]; then
    stop_state="$(printf '%s' "$stop_result" | _json_field state 2>/dev/null || true)"
    case "$stop_state" in
      superseded) echo "[$name] newer intent generation preserved" ;;
      not_running) echo "[$name] not running" ;;
      *) echo "[$name] stopped" ;;
    esac
  else
    echo "[$name] guardian stop failed closed; no external signal was sent" >&2
  fi
  return "$rc"
}

_recover_up(){
  local name="$1" entry="$2" generation="$3"
  _validate_service "$name" || return 1
  _start_guardian "$name" "$entry" "$generation"
}

_status_one(){
  local name="$1" result state child
  result="$("$DANUS_PY" -I -B "$GUARDIAN" status "$(_record "$name")" 2>/dev/null)" || {
    printf "  unsafe %-18s (guardian/control authentication failed)\n" "$name"
    return 1
  }
  state="$(printf '%s' "$result" | _json_field state)" || return 1
  case "$state" in
    absent) printf "  down   %-18s\n" "$name" ;;
    cleanup_in_progress)
      printf "  stop   %-18s (owned cleanup still holds lifecycle authority)\n" "$name"
      ;;
    starting|ready|stopping)
      child="$(printf '%s' "$result" | _json_field child_pid)"
      printf "  up     %-18s child %s (%s)\n" "$name" "$child" "$state"
      ;;
    *) printf "  unsafe %-18s (unknown guardian state)\n" "$name"; return 1 ;;
  esac
}

_health_state(){
  danus_verify_health
}

case "${1:-}" in
  up)
    case "${2:-}" in
      verify) _up verify ;;
      dashboard)
        project="${3:?usage: services.sh up dashboard <project>}"
        _validate_project "$project" || exit 1
        _up "dashboard-$project"
        ;;
      *) echo "usage: services.sh up verify|dashboard <project>" >&2; exit 1 ;;
    esac
    ;;
  down)
    case "${2:-}" in
      verify) _down_one verify ;;
      dashboard|all)
        down_mode="$2"
        down_rc=0
        processed_names=$'\n'
        snapshot="$(_manifest_lines)" || exit 1
        # Records first, then the durable snapshot.  Track exact safe service
        # names so a record+manifest service is linearized only once; an `up`
        # after that delete therefore wins rather than being erased by pass 2.
        for record_path in "$RUN"/*.pid; do
          [ -e "$record_path" ] || [ -L "$record_path" ] || continue
          name="$(basename "$record_path" .pid)"
          _validate_service "$name" >/dev/null || { down_rc=1; continue; }
          [ "$down_mode" = all ] || [[ "$name" == dashboard-* ]] || continue
          processed_names+="${name}"$'\n'
          _down_one "$name" || down_rc=1
        done
        while IFS='|' read -r entry generation; do
          [ -n "$entry" ] || continue
          case "$entry" in
            verify) name=verify ;;
            "dashboard "*) name="dashboard-${entry#dashboard }" ;;
            *) down_rc=1; continue ;;
          esac
          [ "$down_mode" = all ] || [[ "$name" == dashboard-* ]] || continue
          case "$processed_names" in
            *$'\n'"$name"$'\n'*) continue ;;
          esac
          processed_names+="${name}"$'\n'
          _down_one "$name" || down_rc=1
        done <<< "$snapshot"
        exit "$down_rc"
        ;;
      *) echo "usage: services.sh down verify|dashboard|all" >&2; exit 1 ;;
    esac
    ;;
  manifest-snapshot)
    _manifest_lines
    ;;
  manifest-has)
    entry="${2:?usage: services.sh manifest-has ENTRY [GENERATION]}"
    if [ -n "${3:-}" ]; then
      "$DANUS_PY" -I -B "$GUARDIAN" manifest has "$AUTO" "$AUTO_LOCK" \
        "$entry" "$3"
    else
      "$DANUS_PY" -I -B "$GUARDIAN" manifest has "$AUTO" "$AUTO_LOCK" "$entry"
    fi
    ;;
  recover-up)
    entry="${2:?usage: services.sh recover-up ENTRY GENERATION}"
    generation="${3:?usage: services.sh recover-up ENTRY GENERATION}"
    case "$entry" in
      verify) name=verify ;;
      "dashboard "*)
        project="${entry#dashboard }"
        _validate_project "$project" || exit 1
        name="dashboard-$project"
        ;;
      *) echo "unsafe recovery entry" >&2; exit 1 ;;
    esac
    _recover_up "$name" "$entry" "$generation"
    ;;
  recover-stale)
    reconcile_rc=0
    for record_path in "$RUN"/*.pid; do
      [ -e "$record_path" ] || [ -L "$record_path" ] || continue
      name="$(basename "$record_path" .pid)"
      _validate_service "$name" >/dev/null || { reconcile_rc=1; continue; }
      "$DANUS_PY" -I -B "$GUARDIAN" reconcile \
        "$record_path" "$(_lock "$name")" >/dev/null || reconcile_rc=1
    done
    exit "$reconcile_rc"
    ;;
  status)
    echo "== Danus services =="
    found=0
    status_rc=0
    for record_path in "$RUN"/*.pid; do
      [ -e "$record_path" ] || [ -L "$record_path" ] || continue
      found=1
      name="$(basename "$record_path" .pid)"
      _validate_service "$name" >/dev/null || {
        printf "  unsafe %-18s (invalid record filename)\n" "$name"
        status_rc=1
        continue
      }
      _status_one "$name" || status_rc=1
    done
    [ "$found" -eq 0 ] && echo "  (none started via services.sh)"
    health="$(_health_state 2>/dev/null || true)"
    case "$health" in
      ours) echo "verify: up on :$VERIFY_PORT (ours)" ;;
      foreign) echo "verify: FOREIGN responder on :$VERIFY_PORT" ;;
      cleanup_in_progress) echo "verify: stopping/owned cleanup in progress" ;;
      unsafe) echo "verify: unsafe guardian/control state on :$VERIFY_PORT" ;;
      *) echo "verify: down on :$VERIFY_PORT" ;;
    esac
    exit "$status_rc"
    ;;
  test)
    echo "== probing services =="
    health="$(_health_state 2>/dev/null || true)"
    case "$health" in
      ours) echo "  ok   verify  http://127.0.0.1:$VERIFY_PORT (ours)"; exit 0 ;;
      foreign) echo "  FAIL verify  foreign responder on :$VERIFY_PORT" >&2; exit 3 ;;
      cleanup_in_progress) echo "  FAIL verify  owned cleanup in progress" >&2; exit 4 ;;
      unsafe) echo "  FAIL verify  unsafe guardian/control state" >&2; exit 4 ;;
      *) echo "  FAIL verify  down" >&2; exit 5 ;;
    esac
    ;;
  logs)
    name="${2:?usage: services.sh logs <service>}"
    _validate_service "$name" || exit 1
    if [ "${3:-}" = -f ]; then
      "$DANUS_PY" -I -B "$GUARDIAN" read-log "$(_log "$name")" --follow
    else
      "$DANUS_PY" -I -B "$GUARDIAN" read-log "$(_log "$name")"
    fi
    ;;
  *)
    sed -n '1,12p' "$0"
    exit 1
    ;;
esac
