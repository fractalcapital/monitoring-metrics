#!/bin/bash
# One-shot deploy for the monitoring dashboard API on proud-pipes-7.
# Run as root on the VPS, from anywhere:
#   curl -sL https://raw.githubusercontent.com/fractalcapital/monitoring-metrics/main/api/deploy.sh -o /tmp/deploy.sh
#   bash /tmp/deploy.sh <API_TOKEN>
# API_TOKEN also goes into the dashboard page, so it is public by design;
# rate limits (keyed by CF-Connecting-IP) are the real protection.
# You will be prompted (typed in your own terminal, never in chat) for:
#   FEISHU_WEBHOOK_URL, BARK_URL, FRED_API_KEY, HEALTHCHECK_URL (optional),
#   and a GitHub fine-grained PAT (only repo fractalcapital/monitoring-metrics,
#   only Contents read/write, 90-day expiry) used to push data.json back.
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
  useradd --system --home-dir "$REPO_DIR" --shell /bin/bash "$SVC_USER"
fi

if [ -d "$REPO_DIR/.git" ]; then
  echo "== Updating existing checkout =="
  git -C "$REPO_DIR" pull --ff-only
else
  echo "== Cloning repo =="
  git clone https://github.com/fractalcapital/monitoring-metrics.git "$REPO_DIR"
fi
cd "$REPO_DIR"

if [ ! -f .env ]; then
  echo "== Secrets (typed here, stored $SVC_USER-only in .env) =="
  read -r -p "Feishu webhook URL: " FEISHU_WEBHOOK_URL
  read -r -p "Bark base URL (https://bark.infidea.app/<key>): " BARK_URL
  read -r -p "FRED API key (optional, Enter to use public CSV): " FRED_API_KEY
  read -r -p "Healthcheck ping URL (optional, e.g. https://hc-ping.com/<uuid>): " HEALTHCHECK_URL
  cat > .env <<EOF
API_TOKEN=$API_TOKEN
FEISHU_WEBHOOK_URL=$FEISHU_WEBHOOK_URL
BARK_URL=$BARK_URL
FRED_API_KEY=$FRED_API_KEY
HEALTHCHECK_URL=$HEALTHCHECK_URL
EOF
  chmod 600 .env
  echo ".env written."
else
  echo "== .env already exists, keeping it =="
  if ! grep -q "API_TOKEN=$API_TOKEN" .env 2>/dev/null; then
    echo "WARNING: .env already has a different API_TOKEN; keeping the existing one." >&2
    echo "If you want to use the token you just passed, edit .env and restart the service." >&2
  fi
fi

echo "== Git push credential =="
git config user.name "monitoring-vps"
git config user.email "monitoring-vps@infidea.app"
git config credential.helper "store --file=$REPO_DIR/.git-credentials"
if [ ! -f .git-credentials ]; then
  read -r -s -p "GitHub fine-grained PAT (input hidden): " GH_PAT
  echo
  printf 'https://x-access-token:%s@github.com\n' "$GH_PAT" > .git-credentials
  chmod 600 .git-credentials
  unset GH_PAT
  echo "Push credential stored."
else
  echo ".git-credentials already exists, keeping it."
fi

mkdir -p "$LOG_DIR"
chown -R "$SVC_USER:$SVC_USER" "$REPO_DIR" "$LOG_DIR"
chmod 600 "$REPO_DIR/.env" "$REPO_DIR/.git-credentials"

echo "Testing git push as $SVC_USER..."
if su -s /bin/bash "$SVC_USER" -c "cd '$REPO_DIR' && git push"; then
  echo "git push OK."
else
  echo "WARNING: git push failed -- check the PAT and try 'git push' by hand." >&2
fi

echo "== Installing systemd service ($SVC_USER) =="
cp api/monitoring-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now monitoring-api
sleep 2
systemctl status monitoring-api --no-pager | head -12
echo "== Health check =="
curl -s http://127.0.0.1:8080/health; echo

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
echo "30 21,23 * * 1-5 cd $REPO_DIR && ./scripts/push_data.sh >> $LOG_DIR/fetch.log 2>&1" >> /tmp/monitoring-cron.tmp
crontab -u "$SVC_USER" /tmp/monitoring-cron.tmp
rm -f /tmp/monitoring-cron.tmp
crontab -u "$SVC_USER" -l | grep push_data.sh

echo "== Smoke test: one fetch + publish as $SVC_USER =="
su -s /bin/bash "$SVC_USER" -c "cd '$REPO_DIR' && ./scripts/push_data.sh" | tail -5

echo
echo "Done. Next steps:"
echo "  1. Ensure the Cloudflare Tunnel maps api.infidea.app -> http://127.0.0.1:8080."
echo "  2. If using Futu CLI, make sure $SVC_USER can run 'futu' (e.g. /usr/local/bin/futu)."
echo "API_TOKEN in use: $API_TOKEN"
