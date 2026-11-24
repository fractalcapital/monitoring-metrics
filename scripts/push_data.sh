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
if git commit -m "chore: update dashboard data (vps)" >/dev/null 2>&1; then
  if git push; then
    echo "PUSH_OK"
  else
    echo "PUSH_FAILED"
  fi
else
  echo "PUSH_FAILED (commit failed -- check git user.name/user.email)"
fi
exit 0
