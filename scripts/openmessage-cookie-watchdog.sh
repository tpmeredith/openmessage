#!/usr/bin/env bash
set -euo pipefail

STATUS_URL="${OPENMESSAGE_STATUS_URL:-http://127.0.0.1:7007/api/status}"
SERVICE_NAME="${OPENMESSAGE_SERVICE_NAME:-openmessage.service}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REFRESH_SCRIPT="${OPENMESSAGE_COOKIE_REFRESH_SCRIPT:-$SCRIPT_DIR/refresh-google-session-cookies-linux.py}"
SYSTEMCTL_BIN="${OPENMESSAGE_SYSTEMCTL_BIN:-systemctl}"
STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}"
STATE_DIR="${OPENMESSAGE_WATCHDOG_STATE_DIR:-$STATE_ROOT/openmessage/cookie-watchdog}"
STATE_FILE="$STATE_DIR/state"
LOCK_FILE="$STATE_DIR/lock"
FAILURES_REQUIRED="${OPENMESSAGE_WATCHDOG_FAILURES_REQUIRED:-3}"
COOLDOWN_SECONDS="${OPENMESSAGE_WATCHDOG_COOLDOWN_SECONDS:-1800}"
FORCE=0

if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
fi

if ! [[ "$FAILURES_REQUIRED" =~ ^[1-9][0-9]*$ ]]; then
  echo "OPENMESSAGE_WATCHDOG_FAILURES_REQUIRED must be a positive integer." >&2
  exit 2
fi
if ! [[ "$COOLDOWN_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "OPENMESSAGE_WATCHDOG_COOLDOWN_SECONDS must be a non-negative integer." >&2
  exit 2
fi

umask 077
install -d -m 700 "$STATE_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Another OpenMessage cookie watchdog check is already running."
  exit 0
fi

failures=0
last_attempt_epoch=0
if [[ -f "$STATE_FILE" ]]; then
  saved_failures="$(awk -F= '$1 == "failures" {print $2}' "$STATE_FILE" 2>/dev/null || true)"
  saved_attempt="$(awk -F= '$1 == "last_attempt_epoch" {print $2}' "$STATE_FILE" 2>/dev/null || true)"
  if [[ "$saved_failures" =~ ^[0-9]+$ ]]; then
    failures="$saved_failures"
  fi
  if [[ "$saved_attempt" =~ ^[0-9]+$ ]]; then
    last_attempt_epoch="$saved_attempt"
  fi
fi

write_state() {
  local state_tmp="$STATE_FILE.tmp.$$"
  printf 'failures=%s\nlast_attempt_epoch=%s\n' "$failures" "$last_attempt_epoch" >"$state_tmp"
  chmod 600 "$state_tmp"
  mv -f -- "$state_tmp" "$STATE_FILE"
}

status_json=""
status_available="false"
if status_json="$(curl -fsS --max-time 5 "$STATUS_URL" 2>/dev/null)"; then
  if parsed_status="$(python3 -c 'import json,sys
data=json.load(sys.stdin)
g=data.get("google") or {}
print(str(bool(g.get("paired"))).lower(), str(bool(g.get("connected"))).lower(), str(bool(g.get("needs_pairing"))).lower(), (g.get("last_error") or "").replace("\n", " "))' <<<"$status_json" 2>/dev/null)"; then
    read -r paired connected needs_pairing last_error <<<"$parsed_status"
    status_available="true"
  fi
fi

if [[ "$status_available" != "true" ]]; then
  paired="false"
  connected="false"
  needs_pairing="false"
  last_error="status endpoint unavailable"
fi

if [[ "$FORCE" != "1" && "$status_available" != "true" ]]; then
  if ! "$SYSTEMCTL_BIN" --user is-active --quiet "$SERVICE_NAME"; then
    failures=0
    write_state
    echo "OpenMessage service is not active; starting it without replacing session cookies."
    "$SYSTEMCTL_BIN" --user start "$SERVICE_NAME"
    exit 0
  fi
elif [[ "$FORCE" != "1" ]]; then
  if [[ "$paired" != "true" ]]; then
    failures=0
    write_state
    echo "OpenMessage is not paired; cookie watchdog will not overwrite or restart it."
    exit 0
  fi
  if [[ "$needs_pairing" == "true" ]] || grep -Eiq 'invalidated|pair again|logged out server-side' <<<"$last_error"; then
    failures=0
    write_state
    echo "OpenMessage requires a real Google re-pair; automatic cookie refresh is intentionally suppressed."
    exit 0
  fi
  if [[ "$connected" == "true" ]]; then
    failures=0
    write_state
    echo "OpenMessage Google connection is healthy."
    exit 0
  fi
fi

if [[ "$FORCE" != "1" ]]; then
  failures=$((failures + 1))
  if (( failures < FAILURES_REQUIRED )); then
    write_state
    echo "OpenMessage is disconnected; waiting for built-in recovery ($failures/$FAILURES_REQUIRED)."
    exit 0
  fi

  now_epoch="$(date +%s)"
  since_attempt=$((now_epoch - last_attempt_epoch))
  if (( last_attempt_epoch > 0 && since_attempt < COOLDOWN_SECONDS )); then
    write_state
    echo "OpenMessage remains disconnected, but fallback recovery is cooling down ($since_attempt/$COOLDOWN_SECONDS seconds)."
    exit 0
  fi
else
  now_epoch="$(date +%s)"
fi

failures=0
last_attempt_epoch="$now_epoch"
write_state

if [[ "$status_available" == "true" ]]; then
  echo "OpenMessage stayed disconnected through $FAILURES_REQUIRED checks; refreshing cookies once and restarting. reason=${last_error:-unknown}"
  "$SYSTEMCTL_BIN" --user stop "$SERVICE_NAME" || true
  if ! "$REFRESH_SCRIPT" --quiet --no-backup; then
    echo "Cookie refresh failed; restarting with the preserved session and entering cooldown." >&2
    "$SYSTEMCTL_BIN" --user start "$SERVICE_NAME" || true
    exit 1
  fi
else
  echo "OpenMessage status endpoint stayed unavailable; restarting the service without replacing session cookies."
  "$SYSTEMCTL_BIN" --user stop "$SERVICE_NAME" || true
fi
"$SYSTEMCTL_BIN" --user start "$SERVICE_NAME"
