#!/bin/bash
# One-shot setup for running the monitoring dashboard fetcher on a Mac
# (Mac Studio / Mac mini) instead of / in addition to the VPS.
# Why a Mac: Yahoo rate-limits datacenter IPs (the VPS and GitHub Actions
# both get 429), but a home broadband IP is not flagged, so the fetch
# succeeds and the dashboard gets same-day closes.
# Run in Terminal on the Mac you want to use (the one that is powered on
# at 05:30 and 07:30 Beijing, or set a wake schedule below):
#   curl -sL https://raw.githubusercontent.com/fractalcapital/monitoring-metrics/main/api/deploy-mac.sh -o /tmp/deploy-mac.sh
#   bash /tmp/deploy-mac.sh
# You will be prompted (typed in your own terminal, never in chat) for:
#   FEISHU_WEBHOOK_URL, BARK_URL, and a GitHub fine-grained PAT
#   (only repo fractalcapital/monitoring-metrics, only Contents read/write).
#   You can reuse the same PAT you created for the VPS.
# The fetcher runs twice daily (05:30 + 07:30 Beijing, right after the US
# close) via launchd and pushes data.json + alert_state.json to the repo.
# The VPS API (api.infidea.app) keeps serving the dashboard buttons; once
# the Mac fetch has run reliably for a day or two, disable the VPS cron
# (on proud-pipes-7: crontab -e, remove the push_data.sh line) so its
# Yahoo 429s don't send false fetch-failure alerts. GitHub Actions stays
# as the emergency fallback.
set -u

REPO_DIR="$HOME/monitoring-metrics"

# --- prerequisites ---
command -v git >/dev/null 2>&1 || { echo "git not found. Install Xcode Command Line Tools first: xcode-select --install" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 not found." >&2; exit 1; }
echo "python3 $(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

# --- repo ---
if [ -d "$REPO_DIR/.git" ]; then
  echo "== Updating existing checkout =="
  git -C "$REPO_DIR" pull --ff-only
else
  echo "== Cloning repo =="
  git clone https://github.com/fractalcapital/monitoring-metrics.git "$REPO_DIR"
fi
cd "$REPO_DIR"

# --- secrets (no API_TOKEN needed on the Mac: it runs no API server) ---
if [ ! -f .env ]; then
  echo "== Secrets (typed here, stored in .env, chmod 600) =="
  read -r -p "Feishu webhook URL: " FEISHU_WEBHOOK_URL
  read -r -p "Bark base URL (https://bark.infidea.app/<key>): " BARK_URL
  cat > .env <<EOF
FEISHU_WEBHOOK_URL=$FEISHU_WEBHOOK_URL
BARK_URL=$BARK_URL
EOF
  chmod 600 .env
  echo ".env written."
else
  echo "== .env already exists, keeping it =="
fi

# --- git push credential ---
git config user.name "monitoring-mac"
git config user.email "monitoring-mac@infidea.app"
git config credential.helper "store --file=$REPO_DIR/.git-credentials"
if [ ! -f .git-credentials ]; then
  echo "== GitHub PAT (you can reuse the same PAT you created for the VPS) =="
  read -r -s -p "GitHub fine-grained PAT (input hidden): " GH_PAT
  echo
  printf 'https://x-access-token:%s@github.com\n' "$GH_PAT" > .git-credentials
  chmod 600 .git-credentials
  unset GH_PAT
  echo "Push credential stored."
else
  echo ".git-credentials already exists, keeping it."
fi
echo "Testing git push..."
if git push; then echo "git push OK."; else echo "WARNING: git push failed -- check the PAT and try 'git push' by hand." >&2; fi

# --- launchd job (05:30 + 07:30 local time, twice daily) ---
PLIST="$HOME/Library/LaunchAgents/com.infidea.monitoring-fetch.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.infidea.monitoring-fetch</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$REPO_DIR/scripts/push_data.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO_DIR</string>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>5</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer></dict>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$REPO_DIR/fetch.log</string>
  <key>StandardErrorPath</key>
  <string>$REPO_DIR/fetch.log</string>
</dict>
</plist>
EOF
launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load "$PLIST"
echo "launchd job installed: com.infidea.monitoring-fetch (05:30 + 07:30 daily)"
launchctl list | grep monitoring-fetch || echo "WARNING: job not listed -- check with 'launchctl list | grep monitoring'." >&2

# --- wake schedule (optional) ---
echo
echo "== Wake schedule =="
echo "The fetch runs at 05:30 and 07:30. If this Mac sleeps overnight, set a"
echo "daily wake at 05:25 and keep it awake until after 07:30 (system sleep"
echo "after 180 min idle on charger). This changes power settings; skip it"
echo "if this Mac is already on overnight."
read -r -p "Set the wake schedule? [y/N] " SET_WAKE
if [ "$SET_WAKE" = "y" ] || [ "$SET_WAKE" = "Y" ]; then
  sudo pmset repeat wakeorpoweron MTWRFSU 05:25:00
  sudo pmset -c sleep 180
  echo "Wake schedule set (05:25 daily, sleep after 180 min idle on charger)."
  pmset -g sched | head -5
else
  echo "Skipped. Make sure this Mac is awake at 05:30 and 07:30, or the fetch will run late (on wake)."
fi

# --- smoke test ---
echo
echo "== Smoke test: one fetch + publish =="
./scripts/push_data.sh | tail -5

echo
echo "Done. Next steps:"
echo "  1. Watch the next scheduled runs (05:30 / 07:30 Beijing). Log: $REPO_DIR/fetch.log"
echo "  2. Once the Mac fetch runs reliably for a day or two, disable the VPS cron"
echo "     (on proud-pipes-7: crontab -e, remove the push_data.sh line) so its"
echo "     Yahoo 429s don't send false fetch-failure alerts."
echo "  3. GitHub Actions stays as the emergency fallback."
