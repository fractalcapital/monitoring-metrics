#!/bin/bash
# Optional local backup fetcher/alerter setup for a Mac (Mac Studio / Mac mini).
# Run in Terminal on the Mac you want to use (powered on at 05:30 and 07:30
# Beijing on Tue-Sat, right after US Mon-Fri closes):
#   curl -sL https://raw.githubusercontent.com/fractalcapital/monitoring-metrics/main/api/deploy-mac.sh -o /tmp/deploy-mac.sh
#   bash /tmp/deploy-mac.sh
# You will be prompted (typed in your own terminal, never in chat) for:
#   FEISHU_WEBHOOK_URL, BARK_URL, FRED_API_KEY (optional), HEALTHCHECK_URL (optional).
# Note: The VPS (proud-pipes-7) is the primary fetcher and serves live data via
# https://api.infidea.app/api/data. This Mac job writes locally to MM_OUT_DIR
# (no git push) and can serve as an independent alert backup if VPS cron is off.
set -u

REPO_DIR="$HOME/monitoring-metrics"
DATA_DIR="$HOME/.local/share/monitoring-metrics"

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

mkdir -p "$DATA_DIR"
for fname in data.json alert_state.json; do
  if [ ! -f "$DATA_DIR/$fname" ] && [ -f "$REPO_DIR/dashboard/$fname" ]; then
    cp "$REPO_DIR/dashboard/$fname" "$DATA_DIR/$fname"
  fi
done

# --- secrets (no API_TOKEN or GitHub PAT needed on the Mac) ---
if [ ! -f .env ]; then
  echo "== Secrets (typed here, stored in .env, chmod 600) =="
  read -r -p "Feishu webhook URL: " FEISHU_WEBHOOK_URL
  read -r -p "Bark base URL (https://bark.infidea.app/<key>): " BARK_URL
  read -r -p "FRED API key (optional, Enter to use public CSV): " FRED_API_KEY
  read -r -p "Healthcheck ping URL (optional): " HEALTHCHECK_URL
  cat > .env <<EOF
MM_OUT_DIR=$DATA_DIR
FEISHU_WEBHOOK_URL=$FEISHU_WEBHOOK_URL
BARK_URL=$BARK_URL
FRED_API_KEY=$FRED_API_KEY
HEALTHCHECK_URL=$HEALTHCHECK_URL
EOF
  chmod 600 .env
  echo ".env written."
else
  echo "== .env already exists, keeping it =="
  if ! grep -q '^MM_OUT_DIR=' .env 2>/dev/null; then
    printf '\nMM_OUT_DIR=%s\n' "$DATA_DIR" >> .env
    echo "Appended MM_OUT_DIR=$DATA_DIR to .env."
  fi
fi

# --- launchd job (Tue-Sat 05:30 + 07:30 Beijing = US Mon-Fri closes) ---
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
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>5</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>5</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>5</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>5</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>6</integer><key>Hour</key><integer>5</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>6</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer></dict>
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
echo "launchd job installed: com.infidea.monitoring-fetch (Tue-Sat 05:30 + 07:30 local)"
launchctl list | grep monitoring-fetch || echo "WARNING: job not listed -- check with 'launchctl list | grep monitoring'." >&2

# --- wake schedule (optional) ---
echo
echo "== Wake schedule =="
echo "The fetch runs Tue-Sat at 05:30 and 07:30. If this Mac sleeps overnight,"
echo "set a wake at 05:25 and keep it awake until after 07:30 (system sleep"
echo "after 180 min idle on charger). Skip if already awake overnight."
read -r -p "Set the wake schedule? [y/N] " SET_WAKE
if [ "$SET_WAKE" = "y" ] || [ "$SET_WAKE" = "Y" ]; then
  sudo pmset repeat wakeorpoweron TWRFS 05:25:00
  sudo pmset -c sleep 180
  echo "Wake schedule set (Tue-Sat 05:25, sleep after 180 min idle on charger)."
  pmset -g sched | head -5
else
  echo "Skipped. Make sure this Mac is awake at 05:30 and 07:30 Tue-Sat."
fi

# --- smoke test ---
echo
echo "== Smoke test: one fetch =="
./scripts/push_data.sh | tail -5

echo
echo "Done. Watch the next scheduled runs (Tue-Sat 05:30 / 07:30 Beijing). Log: $REPO_DIR/fetch.log"
