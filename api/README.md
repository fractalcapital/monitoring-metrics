# Monitoring dashboard API (VPS)

The dashboard page is static (GitHub Pages) and holds no secrets, so it
cannot fetch data or send a push by itself. This service runs on the
user's VPS (proud-pipes-7, behind the existing Cloudflare Tunnel) and is
the backend for the dashboard's manual buttons. It also replaces the
twice-daily GitHub Actions fetch once it proves reliable.

## Endpoints

Base URL: `https://api.infidea.app` (tunnel hostname → `127.0.0.1:8080`).

| Method | Path | Body | What it does |
| --- | --- | --- | --- |
| GET | `/health` | — | `{"ok": true}` — tunnel / monitoring check |
| POST | `/api/refresh` | — | Starts a background fetch + git-push job and returns `{"status": "started"}` at once. The fetch can run minutes from a datacenter IP (Yahoo retries with backoff), past Cloudflare's ~100s tunnel timeout, so a synchronous reply would 524. Poll `GET /api/job` for the outcome; the page re-reads `data.json` when the job finishes. Only one slow job runs at a time; a second start while one runs returns `{"ok": false, "error": "a … job is already running"}` |
| GET | `/api/job` | — | `{"running": "refresh" \| "push" \| null, "last_result": {...} \| null}`. No auth (read-only; the page reads it same-origin) |
| POST | `/api/push` | `{"mode": "test" \| "status", "channels": ["feishu"] \| ["bark"]}` | Both run synchronously and return per-channel results (fast). `test` sends one test message and touches no data file. `status` sends the 5-indicator summary built from the cached `data.json` (no fetch — from a datacenter IP the Yahoo fetch takes minutes, so a push that fetches first takes 3+ minutes). The slow fetch-then-push path lives only in the GitHub workflow (`MANUAL_PUSH=status`). Rate-limited per channel, so the Feishu and Bark buttons don't block each other |

All POSTs need the `X-Api-Token` header. The token is also in the
dashboard page source (public), so it is NOT real security. The real
protections are rate limits — 1 call per minute per IP, 10 pushes / 30
refreshes per day per IP, 20 pushes / 60 refreshes per day globally
(the real client IP comes from the `X-Forwarded-For` header: behind
the Cloudflare Tunnel the TCP peer is always cloudflared on
127.0.0.1, so without it per-IP limits would be global) —
and the fact that these endpoints can only send to the owner's
Feishu/Bark and fetch public market data. CORS allows only
`https://mon.infidea.app`.

The Feishu webhook URL and Bark key live ONLY in this service's
environment (`/opt/monitoring-metrics/.env`, root-only). They never go
into the repo or the page.

## Files

- `api_server.py` — stdlib-only HTTP service (no pip dependencies).
- `monitoring-api.service` — systemd unit (runs as root, restarts always).
- `../scripts/push_data.sh` — fetch + git-publish, used by cron and by
  the API. Exit 0 if the fetch succeeded (even if the git push failed —
  retried on the next run); exit 1 only if the fetch failed.

## VPS setup (proud-pipes-7, as root)

```bash
# 1. Clone the repo (public, no credential needed to read)
git clone https://github.com/fractalcapital/monitoring-metrics.git /opt/monitoring-metrics
cd /opt/monitoring-metrics

# 2. Secrets file (root-only). API_TOKEN is the token Muse gave you in
#    chat; the two webhook values are the same ones already in the
#    repo's GitHub Secrets -- paste them here yourself, never in chat.
cat > .env <<'EOF'
API_TOKEN=<the token from chat>
FEISHU_WEBHOOK_URL=<full Feishu bot webhook URL>
BARK_URL=<Bark base only, https://bark.infidea.app/<key>, no /title/body>
FRED_API_KEY=<free FRED API key from fred.stlouisfed.org>
# Optional overrides:
# FUTU_CLI=/usr/local/bin/futu
# MOVE_SOURCE_ORDER=yahoo,tradingview,thetrading_tools
EOF
chmod 600 .env

# Optional (recommended on VPS): install curl_cffi so Yahoo ^MOVE uses Chrome TLS impersonation
python3 -m pip install --break-system-packages curl_cffi || python3 -m pip install curl_cffi

# 3. Git identity + push credential. Create a fine-grained personal
#    access token on GitHub (Settings -> Developer settings -> Personal
#    access tokens -> Fine-grained tokens): only this repo
#    (fractalcapital/monitoring-metrics), only "Contents: read and
#    write", expiry 90 days. Then:
git config user.name "monitoring-vps"
git config user.email "monitoring-vps@infidea.app"
git config credential.helper "store --file=/opt/monitoring-metrics/.git-credentials"
printf 'https://x-access-token:<the PAT>@github.com\n' > .git-credentials
chmod 600 .git-credentials
#    Test the push credential once (should say Everything up-to-date):
git push

# 4. Install + start the service
cp api/monitoring-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now monitoring-api
systemctl status monitoring-api --no-pager
curl -s http://127.0.0.1:8080/health

# 5. Twice-daily fetch (same times as the old Actions schedule:
#    21:30 and 23:30 UTC = 05:30 and 07:30 Beijing next day)
(crontab -l 2>/dev/null; echo '30 21,23 * * * cd /opt/monitoring-metrics && ./scripts/push_data.sh >> /var/log/monitoring-fetch.log 2>&1') | crontab -

# 6. Tunnel hostname: in the Cloudflare Zero Trust dashboard
#    (Networks -> Tunnels -> the tunnel -> Public Hostname -> Add):
#    hostname api.infidea.app, service http://127.0.0.1:8080
#    Then verify from outside the VPS:
curl -s https://api.infidea.app/health
```

## Verify the manual buttons (after the dashboard is wired to the API)

```bash
TOKEN=<the token from chat>
curl -s -X POST https://api.infidea.app/api/push \
  -H "X-Api-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"mode":"test"}'
curl -s -X POST https://api.infidea.app/api/refresh \
  -H "X-Api-Token: $TOKEN"
```

## Replacing GitHub Actions

While the VPS cron is being proven, the Actions schedule stays on —
but two writers to `data.json` can conflict on `git push`, so once the
VPS cron has run cleanly for a couple of days, disable the schedule in
`.github/workflows/update-dashboard.yml` (leave `workflow_dispatch`
there as an emergency fallback). The dashboard page keeps working
either way: it only reads `data.json` from GitHub Pages.
