#!/bin/bash
# One-shot deploy for the monitoring dashboard API on proud-pipes-7.
# Run as root on the VPS, from anywhere:
#   curl -sL https://raw.githubusercontent.com/fractalcapital/monitoring-metrics/main/api/deploy.sh -o /tmp/deploy.sh
#   bash /tmp/deploy.sh <API_TOKEN>
# API_TOKEN is the token Muse gave you in chat. It also goes into the
# dashboard page, so it is public by design; rate limits are the real
# protection.
# You will be prompted (typed in your own terminal, never in chat) for:
#   FEISHU_WEBHOOK_URL, BARK_URL, and a GitHub fine-grained PAT
#   (only repo fractalcapital/monitoring-metrics, only Contents read/write,
#   90-day expiry) used to push data.json back to the repo.
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

command -v git >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq git curl; }
command -v python3 >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq python3; }
if ! python3 -c "import curl_cffi" >/dev/null 2>&1; then
  echo "== Installing curl_cffi for Yahoo TLS impersonation =="
  python3 -m pip install --quiet --break-system-packages curl_cffi 2>/dev/null \
    || python3 -m pip install --quiet curl_cffi 2>/dev/null \
    || echo "WARNING: curl_cffi install skipped (pip missing?); Yahoo will use stdlib urllib + TradingView/CSV fallback." >&2
fi

REPO_DIR=/opt/monitoring-metrics
if [ -d "$REPO_DIR/.git" ]; then
  echo "== Updating existing checkout =="
  git -C "$REPO_DIR" pull --ff-only
else
  echo "== Cloning repo =="
  git clone https://github.com/fractalcapital/monitoring-metrics.git "$REPO_DIR"
fi
cd "$REPO_DIR"

if [ ! -f .env ]; then
  echo "== Secrets (typed here, stored root-only in .env) =="
  read -r -p "Feishu webhook URL: " FEISHU_WEBHOOK_URL
  read -r -p "Bark base URL (https://bark.infidea.app/<key>): " BARK_URL
  read -r -p "FRED API key (optional, Enter to use public CSV): " FRED_API_KEY
  cat > .env <<EOF
API_TOKEN=$API_TOKEN
FEISHU_WEBHOOK_URL=$FEISHU_WEBHOOK_URL
BARK_URL=$BARK_URL
FRED_API_KEY=$FRED_API_KEY
EOF
  chmod 600 .env
  echo ".env written."
else
  echo "== .env already exists, keeping it =="
  # Make sure the token in .env matches the one passed in.
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
echo "Testing git push..."
if git push; then echo "git push OK."; else echo "WARNING: git push failed -- check the PAT and try 'git push' by hand." >&2; fi

echo "== Installing service =="
cp api/monitoring-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now monitoring-api
sleep 2
systemctl status monitoring-api --no-pager | head -12
echo "== Health check =="
curl -s http://127.0.0.1:8080/health; echo

echo "== Cron (twice daily, 21:30 + 23:30 UTC) =="
crontab -l 2>/dev/null | grep -v "monitoring-metrics.*push_data.sh" > /tmp/monitoring-cron.tmp || true
echo '30 21,23 * * * cd /opt/monitoring-metrics && ./scripts/push_data.sh >> /var/log/monitoring-fetch.log 2>&1' >> /tmp/monitoring-cron.tmp
crontab /tmp/monitoring-cron.tmp
rm -f /tmp/monitoring-cron.tmp
crontab -l | grep push_data.sh

echo "== Smoke test: one fetch + publish =="
./scripts/push_data.sh | tail -5

echo
echo "Done. Next steps (Muse does these):"
echo "  1. Add the tunnel hostname api.infidea.app -> http://127.0.0.1:8080"
echo "     in the Cloudflare Zero Trust dashboard."
echo "  2. Wire the dashboard buttons to https://api.infidea.app and verify live."
echo "API_TOKEN in use: $API_TOKEN"
