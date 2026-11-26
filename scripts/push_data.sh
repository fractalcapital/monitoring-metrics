#!/bin/bash
# Fetch latest closes and publish data.json + alert_state.json to GitHub.
# Used by cron (twice daily, same times as the old Actions schedule) and
# by the VPS API service (refresh + status push).
# MANUAL_PUSH env (test/status) is passed through to the fetch script;
# "test" only sends a test message and does not touch data files.
# Exit 0 if the fetch succeeded, even if the git push failed (the push is
# retried on the next run); exit 1 only if the fetch itself failed.
set -u
cd "$(dirname "$0")/.."

# Serialize concurrent runs (cron + manual VPS API refresh) to avoid
# git index.lock / rebase / data.json write races. Uses flock on Linux,
# falling back to an atomic mkdir lockdir (with stale-PID recovery) on macOS.
LOCK_WAIT_SECONDS="${LOCK_WAIT_SECONDS:-120}"
LOCK_BASE="${TMPDIR:-/tmp}"
LOCK_FILE="${LOCK_BASE%/}/monitoring-metrics-push.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK_FILE"
  if ! flock -w "$LOCK_WAIT_SECONDS" 9; then
    echo "LOCK_BUSY (another push_data.sh run is still active after ${LOCK_WAIT_SECONDS}s)" >&2
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
    echo "LOCK_BUSY (another push_data.sh run is still active after ${LOCK_WAIT_SECONDS}s)" >&2
    exit 1
  fi
  trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM
fi

# Load secrets for cron (the API service already has them via systemd).
if [ -f .env ]; then set -a; . ./.env; set +a; fi

# Sync first, while the tree is still clean: the repo may be behind
# (dashboard/API changes pushed from elsewhere). Pull with rebase so
# our data commit lands on top of the latest remote. A failed pull
# (e.g. a data.json conflict with an Actions commit, while both
# schedules still run) is not fatal: abort any partial rebase and let
# the push fail this run; the next run retries. (Must run before the
# fetch: once the fetch has modified data.json, rebase refuses to run.)
if ! git pull --rebase --quiet; then
  git rebase --abort >/dev/null 2>&1 || true
  echo "PULL_FAILED (will retry next run)"
fi

python3 scripts/fetch_dashboard.py
fetch_status=$?
if [ "$fetch_status" -ne 0 ]; then
  echo "FETCH_FAILED"
  exit 1
fi
echo "FETCH_OK"

git add dashboard/data.json dashboard/alert_state.json
if git diff --cached --quiet; then
  echo "PUSH_OK (nothing new to commit)"
  exit 0
fi
if git commit -m "chore: update dashboard data ($(hostname -s))" >/dev/null 2>&1; then
  if git push; then
    echo "PUSH_OK"
  else
    echo "PUSH_FAILED"
  fi
else
  echo "PUSH_FAILED (commit failed -- check git user.name/user.email)"
fi
exit 0
