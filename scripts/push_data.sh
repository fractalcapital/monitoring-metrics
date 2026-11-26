#!/bin/bash
# Fetch latest market closes and atomically update data.json + alert_state.json
# in MM_OUT_DIR (default: dashboard/; production VPS: /var/lib/monitoring/data).
# Used by cron (twice daily on US trading weekdays) and by the VPS API
# service (manual refresh). Data is served directly by api_server.py
# (GET /api/data & GET /api/alert-state), so no git commit/push is performed.
#
# MANUAL_PUSH env (test/status/status-cached) is passed through to the fetch
# script; "test" and "status-cached" do not touch data files.
set -u
cd "$(dirname "$0")/.."

# Load secrets and MM_OUT_DIR for cron (systemd already has them via .env).
if [ -f .env ]; then set -a; . ./.env; set +a; fi

# Serialize concurrent runs (cron + manual VPS API refresh) to avoid
# overlapping data.json / alert_state.json writes. Uses flock on Linux,
# falling back to an atomic mkdir lockdir (with stale-PID recovery) on macOS.
LOCK_WAIT_SECONDS="${LOCK_WAIT_SECONDS:-120}"
if [ -n "${MM_OUT_DIR:-}" ]; then
  mkdir -p "$MM_OUT_DIR" 2>/dev/null || true
  LOCK_BASE="$MM_OUT_DIR"
elif [ -d .git ]; then
  LOCK_BASE=".git"
else
  LOCK_BASE="${TMPDIR:-/tmp}"
fi
LOCK_FILE="${LOCK_BASE%/}/monitoring-metrics-fetch.lock"

if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK_FILE"
  if ! flock -w "$LOCK_WAIT_SECONDS" 9; then
    echo "LOCK_BUSY (another fetch run is still active after ${LOCK_WAIT_SECONDS}s)" >&2
    exit 1
  fi
else
  LOCK_DIR="${LOCK_FILE}.d"
  _lock_acquired=0
  _waited=0
  while [ "$_waited" -lt "$LOCK_WAIT_SECONDS" ]; do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      printf '%s\n' "$$" > "$LOCK_DIR/pid"
      _lock_acquired=1
      break
    fi
    if [ -f "$LOCK_DIR/pid" ]; then
      _old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
      if [ -n "$_old_pid" ] && ! kill -0 "$_old_pid" 2>/dev/null; then
        rm -rf "$LOCK_DIR"
        continue
      fi
    fi
    sleep 1
    _waited=$((_waited + 1))
  done
  if [ "$_lock_acquired" -ne 1 ]; then
    echo "LOCK_BUSY (another fetch run is still active after ${LOCK_WAIT_SECONDS}s)" >&2
    exit 1
  fi
  trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM
fi

python3 scripts/fetch_dashboard.py
fetch_status=$?
if [ "$fetch_status" -ne 0 ]; then
  echo "FETCH_FAILED"
  exit 1
fi
echo "FETCH_OK"

if [ -n "${HEALTHCHECK_URL:-}" ] && command -v curl >/dev/null 2>&1; then
  curl -fsS -m 10 --retry 3 "$HEALTHCHECK_URL" >/dev/null 2>&1 \
    || echo "HEALTHCHECK_PING_FAILED" >&2
fi
exit 0
