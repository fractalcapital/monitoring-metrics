#!/bin/bash
# One-shot deploy for the monitoring dashboard API on proud-pipes-7.
# Run as root on the VPS, from anywhere:
#   curl -sL https://raw.githubusercontent.com/fractalcapital/monitoring-metrics/main/api/deploy.sh -o /tmp/deploy.sh
#   bash /tmp/deploy.sh <API_TOKEN>
# API_TOKEN also goes into the dashboard page, so it is public by design;
# rate limits (keyed by CF-Connecting-IP) are the real protection.
# You will be prompted (typed in your own terminal, never in chat) for:
#   FEISHU_WEBHOOK_URL, BARK_URL, FRED_API_KEY, HEALTHCHECK_URL (optional).
# Runtime data (data.json / alert_state.json) is stored in /var/lib/monitoring/data
# and served directly via GET /api/data & GET /api/alert-state (no GitHub PAT needed).
set -u

API_TOKEN="${1:-}"
if [ -z "$API_TOKEN" ]; then
  echo "Usage: bash deploy.sh <API_TOKEN>" >&2
  exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root." >&2
  exit 1
fi

SVC_USER="monitoring"
SVC_HOME="/var/lib/monitoring"
DATA_DIR="$SVC_HOME/data"
REPO_DIR="/opt/monitoring-metrics"
LOG_DIR="/var/log/monitoring-metrics"

command -v git >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq git curl; }
command -v python3 >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq python3; }
if ! python3 -c "import curl_cffi" >/dev/null 2>&1; then
  echo "== Installing curl_cffi for Yahoo TLS impersonation =="
  python3 -m pip install --quiet --break-system-packages curl_cffi 2>/dev/null \
    || python3 -m pip install --quiet curl_cffi 2>/dev/null \
    || echo "WARNING: curl_cffi install skipped (pip missing?); Yahoo will use stdlib urllib + TradingView/CSV fallback." >&2
fi

if ! id "$SVC_USER" >/dev/null 2>&1; then
  echo "== Creating dedicated system user ($SVC_USER) =="
  useradd --system --create-home --home-dir "$SVC_HOME" --shell /bin/bash "$SVC_USER"
fi
mkdir -p "$SVC_HOME" "$DATA_DIR"
chown -R "$SVC_USER:$SVC_USER" "$SVC_HOME"
chmod 700 "$SVC_HOME"

if [ -d "$REPO_DIR/.git" ]; then
  echo "== Updating existing checkout =="
  git config --global --add safe.directory "$REPO_DIR" 2>/dev/null || true
  git -C "$REPO_DIR" pull --ff-only
else
  echo "== Cloning repo =="
  git clone https://github.com/fractalcapital/monitoring-metrics.git "$REPO_DIR"
fi
cd "$REPO_DIR"

# Seed runtime data dir from repo snapshot on first migration so history & alert
# dedup state carry over seamlessly without dirtying the git working tree.
for fname in data.json alert_state.json; do
  if [ ! -f "$DATA_DIR/$fname" ] && [ -f "$REPO_DIR/data/$fname" ]; then
    cp "$REPO_DIR/data/$fname" "$DATA_DIR/$fname"
    echo "Seeded $DATA_DIR/$fname from data/$fname"
  fi
done

if [ ! -f .env ]; then
  echo "== Secrets (typed here, stored $SVC_USER-only in .env) =="
  read -r -p "Feishu webhook URL: " FEISHU_WEBHOOK_URL
  read -r -p "Bark base URL (https://bark.infidea.app/<key>): " BARK_URL
  read -r -p "FRED API key (optional, Enter to use public CSV): " FRED_API_KEY
  read -r -p "Healthcheck ping URL (optional, e.g. https://hc-ping.com/<uuid>): " HEALTHCHECK_URL
  cat > .env <<EOF
API_TOKEN=$API_TOKEN
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
  if ! grep -q "API_TOKEN=$API_TOKEN" .env 2>/dev/null; then
    echo "WARNING: .env already has a different API_TOKEN; keeping the existing one." >&2
    echo "If you want to use the token you just passed, edit .env and restart the service." >&2
  fi
fi

if [ -f "$REPO_DIR/.git-credentials" ]; then
  echo "NOTE: $REPO_DIR/.git-credentials is no longer needed (data is served via API, not pushed to GitHub)."
  echo "      You can remove it and revoke the old GitHub PAT once you verify /api/data works."
fi

mkdir -p "$LOG_DIR"
rm -f /tmp/monitoring-metrics-push.lock /tmp/monitoring-metrics-push-fail.count \
      "$REPO_DIR/.git/monitoring-metrics-push.lock" "$REPO_DIR/.git/monitoring-metrics-push-fail.count"
chown -R "$SVC_USER:$SVC_USER" "$REPO_DIR" "$SVC_HOME" "$LOG_DIR"
chmod 600 "$REPO_DIR/.env"
chmod +x "$REPO_DIR/scripts/push_data.sh" "$REPO_DIR/scripts/fetch_dashboard.py" "$REPO_DIR/api/api_server.py"

echo "== Installing systemd service ($SVC_USER) =="
cp api/monitoring-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable monitoring-api
systemctl restart monitoring-api
sleep 2
systemctl status monitoring-api --no-pager | head -12
echo "== Health & data check =="
curl -s http://127.0.0.1:8080/health; echo
curl -s http://127.0.0.1:8080/api/data | head -c 200; echo

echo "== Configuring logrotate =="
cat > /etc/logrotate.d/monitoring-metrics <<EOF
$LOG_DIR/fetch.log /var/log/monitoring-fetch.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF

echo "== Cron (US trading weekdays: 21:30 + 23:30 UTC Mon-Fri, as $SVC_USER) =="
# Remove any legacy root cron entry so we don't double-run.
if crontab -l 2>/dev/null | grep -q "monitoring-metrics.*push_data.sh"; then
  crontab -l 2>/dev/null | grep -v "monitoring-metrics.*push_data.sh" | crontab - || true
fi
crontab -u "$SVC_USER" -l 2>/dev/null | grep -v "monitoring-metrics.*push_data.sh" > /tmp/monitoring-cron.tmp || true
echo "30 21,23 * * 1-5 cd $REPO_DIR && /bin/bash ./scripts/push_data.sh >> $LOG_DIR/fetch.log 2>&1" >> /tmp/monitoring-cron.tmp
crontab -u "$SVC_USER" /tmp/monitoring-cron.tmp
rm -f /tmp/monitoring-cron.tmp
crontab -u "$SVC_USER" -l | grep push_data.sh

echo "== Smoke test: one fetch as $SVC_USER =="
su -s /bin/bash "$SVC_USER" -c "cd '$REPO_DIR' && /bin/bash ./scripts/push_data.sh" | tail -10

echo
echo "Done. Next steps:"
echo "  1. Ensure the Cloudflare Tunnel maps api.infidea.app -> http://127.0.0.1:8080."
echo "  2. Verify https://api.infidea.app/api/data returns the live JSON."
echo "  3. If using Futu CLI, make sure $SVC_USER can run 'futu' (e.g. /usr/local/bin/futu)."
echo "API_TOKEN in use: $API_TOKEN"
