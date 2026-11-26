#!/bin/bash
# Fetch latest closes and publish data.json + alert_state.json to GitHub.
# Used by cron (twice daily on US trading weekdays) and by the VPS API
# service (manual refresh).
# MANUAL_PUSH env (test/status/status-cached) is passed through to the fetch
# script; "test" and "status-cached" do not touch data files.
# Exit 0 if the fetch succeeded, even if the git push failed (retried on the
# next run; consecutive failures trigger an alert); exit 1 only if the fetch
# itself failed.
set -u
cd "$(dirname "$0")/.."

# Serialize concurrent runs (cron + manual VPS API refresh) to avoid
# git index.lock / rebase / data.json write races. Uses flock on Linux,
# falling back to an atomic mkdir lockdir (with stale-PID recovery) on macOS.
LOCK_WAIT_SECONDS="${LOCK_WAIT_SECONDS:-120}"
if [ -d .git ]; then
  LOCK_BASE=".git"
else
  LOCK_BASE="${TMPDIR:-/tmp}"
fi
LOCK_FILE="${LOCK_BASE%/}/monitoring-metrics-push.lock"
PUSH_FAIL_FILE="${LOCK_BASE%/}/monitoring-metrics-push-fail.count"
PUSH_FAIL_ALERT_THRESHOLD="${PUSH_FAIL_ALERT_THRESHOLD:-2}"

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
# our data commit lands on top of the latest remote.
# If rebase conflicts (e.g. a leftover unpushed data commit colliding with
# a newer remote data commit), recover automatically ONLY when the working
# tree is clean and every unpushed commit touches only generated data files.
if ! git pull --rebase --quiet; then
  git rebase --abort >/dev/null 2>&1 || true
  if git fetch origin --quiet 2>/dev/null; then
    _upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo 'origin/main')"
    _dirty="$(git status --porcelain 2>/dev/null || true)"
    _non_data="$(git diff --name-only "${_upstream}..HEAD" 2>/dev/null \
      | grep -v -E '^dashboard/(data|alert_state)\.json$' || true)"
    if [ -z "$_dirty" ] && [ -z "$_non_data" ]; then
      echo "PULL_CONFLICT_RECOVERED (resetting stale local data commits to ${_upstream})"
      git reset --hard "$_upstream" >/dev/null 2>&1 || true
    else
      echo "PULL_FAILED (non-data local changes present; skipping reset)"
    fi
  else
    echo "PULL_FAILED (git fetch failed; will retry next run)"
  fi
fi

python3 scripts/fetch_dashboard.py
fetch_status=$?
if [ "$fetch_status" -ne 0 ]; then
  echo "FETCH_FAILED"
  exit 1
fi
echo "FETCH_OK"

# Check whether market data or alert state substantively changed compared to
# HEAD (ignoring pure timestamp fields like updated_at_utc / last_ok_at_utc)
# to avoid noisy no-op commits when a scheduled retry sees unchanged closes.
# Set ALWAYS_COMMIT=1 to force committing timestamp updates.
_has_substantive_change=1
if [ "${ALWAYS_COMMIT:-0}" != "1" ]; then
  if python3 - <<'PYEOF'
import json, subprocess, sys

IGNORE = {"updated_at_utc", "updated_at_beijing", "last_ok_at_utc"}

def strip_ts(obj):
    if isinstance(obj, dict):
        return {k: strip_ts(v) for k, v in obj.items() if k not in IGNORE}
    if isinstance(obj, list):
        return [strip_ts(x) for x in obj]
    return obj

def load_head(path):
    res = subprocess.run(["git", "show", "HEAD:" + path], capture_output=True, text=True)
    if res.returncode != 0:
        return None
    return strip_ts(json.loads(res.stdout))

def load_work(path):
    with open(path, encoding="utf-8") as fh:
        return strip_ts(json.load(fh))

try:
    same = all(
        load_head(p) == load_work(p)
        for p in ("dashboard/data.json", "dashboard/alert_state.json")
    )
    sys.exit(0 if same else 1)
except Exception:
    sys.exit(1)
PYEOF
  then
    _has_substantive_change=0
  fi
fi

_ping_healthcheck() {
  if [ -n "${HEALTHCHECK_URL:-}" ] && command -v curl >/dev/null 2>&1; then
    curl -fsS -m 10 --retry 3 "$HEALTHCHECK_URL" >/dev/null 2>&1 \
      || echo "HEALTHCHECK_PING_FAILED" >&2
  fi
}

_notify_push_failure() {
  _reason="$1"
  _fails=1
  if [ -f "$PUSH_FAIL_FILE" ]; then
    _prev="$(cat "$PUSH_FAIL_FILE" 2>/dev/null || echo 0)"
    case "$_prev" in
      ''|*[!0-9]*) _prev=0 ;;
    esac
    _fails=$((_prev + 1))
  fi
  printf '%s\n' "$_fails" > "$PUSH_FAIL_FILE" 2>/dev/null || true
  if [ "$_fails" -ge "$PUSH_FAIL_ALERT_THRESHOLD" ]; then
    PUSH_FAIL_COUNT="$_fails" PUSH_FAIL_REASON="$_reason" python3 - <<'PYEOF'
import datetime as dt, os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from alert.notify import send_bark, send_feishu_card

count = os.environ.get("PUSH_FAIL_COUNT", "?")
reason = os.environ.get("PUSH_FAIL_REASON", "git push failed")
now_s = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
title = "【监控报警·预警】数据推送 GitHub 连续失败"
body = (
    "连续失败次数：%s 次\n"
    "失败原因：%s\n"
    "时间：%s\n"
    "本地数据已拉取，但未能推送到 GitHub Pages，线上仪表盘将停留在旧数据，请检查 PAT / 网络 / 仓库状态。\n"
    "查看仪表盘：https://mon.infidea.app/dashboard/"
    % (count, reason, now_s)
)
send_feishu_card(title, body, "orange")
send_bark(title, body, "warning")
PYEOF
  fi
}

if [ "$_has_substantive_change" -eq 0 ]; then
  # Keep the working tree clean so the next run's git pull --rebase succeeds.
  git checkout -- dashboard/data.json dashboard/alert_state.json >/dev/null 2>&1 || true
  printf '0\n' > "$PUSH_FAIL_FILE" 2>/dev/null || true
  _ping_healthcheck
  echo "PUSH_OK (no substantive data change)"
  exit 0
fi

git add dashboard/data.json dashboard/alert_state.json
if git diff --cached --quiet; then
  printf '0\n' > "$PUSH_FAIL_FILE" 2>/dev/null || true
  _ping_healthcheck
  echo "PUSH_OK (nothing new to commit)"
  exit 0
fi

if git commit -m "chore: update dashboard data ($(hostname -s))" >/dev/null 2>&1; then
  if git push; then
    printf '0\n' > "$PUSH_FAIL_FILE" 2>/dev/null || true
    _ping_healthcheck
    echo "PUSH_OK"
  else
    echo "PUSH_FAILED"
    _notify_push_failure "git push failed on $(hostname -s)"
  fi
else
  echo "PUSH_FAILED (commit failed -- check git user.name/user.email)"
  _notify_push_failure "git commit failed on $(hostname -s)"
fi
exit 0
