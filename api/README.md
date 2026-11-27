# Monitoring dashboard API (VPS)

The dashboard page is static (GitHub Pages) and holds no secrets, so it
cannot fetch data or send a push by itself. This service runs on the VPS
(`proud-pipes-7`, behind Cloudflare Tunnel) as a dedicated non-root user
(`monitoring`) and handles both the scheduled weekday data fetch (via cron)
and the dashboard's manual refresh/push buttons.

## Endpoints

Base URL: `https://api.infidea.app` (tunnel hostname → `127.0.0.1:8080`).

| Method | Path | Body | What it does |
| --- | --- | --- | --- |
| GET | `/health` | — | `{"ok": true}` — tunnel / monitoring check |
| GET | `/api/data` | — | Public read-only live `data.json` from `MM_OUT_DIR` (fallback: `data/data.json`; supports `ETag`/`304`, gzip, mtime cache, `Cache-Control: public, max-age=60, stale-while-revalidate=300`). |
| GET | `/api/history` | — | Public read-only ~1y `history.json` for `90D / 1Y` chart views (same ETag/gzip/cache semantics). |
| GET | `/api/alert-state` | — | Public read-only live `alert_state.json` from `MM_OUT_DIR` (same ETag/gzip/cache semantics). |
| GET | `/api/alert-history` | — | Public read-only `alert_history.json` (recent alert trigger/upgrade/resolve log; returns empty list if absent). |
| POST | `/api/refresh` | — | Starts a background fetch job into `MM_OUT_DIR` and returns `{"status": "started"}` immediately (avoids Cloudflare's ~100s tunnel timeout). Poll `GET /api/job` for completion. Only one refresh job runs at a time. |
| GET | `/api/job` | — | `{"running": "refresh" \| null, "last_result": {...} \| null}`. Read-only snapshot copied under lock. |
| POST | `/api/push` | `{"mode": "test" \| "status", "channels": ["feishu"] \| ["bark"]}` | Synchronous (1–2s). `test` sends a test message; `status` builds the summary from the cached `data.json` (`MANUAL_PUSH=status-cached`, no fetch). Rate-limited per channel so Feishu and Bark buttons don't block each other. |

### Auth & Rate Limits

- POST endpoints require `X-Api-Token` matching `API_TOKEN`. Because the
  token is embedded in the static page, real protection comes from rate
  limits (1/min per IP, 10 pushes / 30 refreshes per day per IP, 20 pushes /
  60 refreshes per day globally), keyed by Cloudflare's non-spoofable
  `CF-Connecting-IP` header (falling back to peer IP for local calls;
  `X-Forwarded-For` is intentionally ignored).
- CORS allows only `https://mon.infidea.app` on write endpoints (`POST`); public
  read-only `GET /health`, `GET /api/data`, and `GET /api/alert-state` emit
  `Access-Control-Allow-Origin: *`.
- Subprocess runs execute in their own session (`start_new_session=True`) so
  timeouts terminate the entire process group cleanly.
- Secrets (`FEISHU_WEBHOOK_URL`, `BARK_URL`, `FRED_API_KEY`, optional
  `HEALTHCHECK_URL`) and `MM_OUT_DIR=/var/lib/monitoring/data` live only in
  `/opt/monitoring-metrics/.env` (`chmod 600`, owned by `monitoring`). No
  GitHub PAT is required on the VPS.

## Files

- `api_server.py` — stdlib-only HTTP service (serves live `data.json` / `alert_state.json` and manual refresh/push endpoints).
- `monitoring-api.service` — hardened systemd unit (`User=monitoring`,
  `NoNewPrivileges=yes`, `ProtectSystem=strict`, restricted `ReadWritePaths`).
- `deploy.sh` — one-shot VPS deployment (creates `monitoring` user and
  `/var/lib/monitoring/data`, installs systemd unit, logrotate for
  `/var/log/monitoring-metrics/fetch.log`, and weekday cron `30 21,23 * * 1-5`).
- `deploy-mac.sh` — optional macOS local backup fetcher/alerter via `launchd`
  (Tue–Sat 05:30 / 07:30 Beijing, no git push).
- `../scripts/push_data.sh` — serialized fetch + atomic write to `MM_OUT_DIR`
  + optional `HEALTHCHECK_URL` ping (no git commit/push).

## Quick VPS Setup

```bash
curl -sL https://raw.githubusercontent.com/fractalcapital/monitoring-metrics/main/api/deploy.sh -o /tmp/deploy.sh
sudo bash /tmp/deploy.sh <API_TOKEN>
```

## Verify Endpoints

```bash
curl -s https://api.infidea.app/health

TOKEN=<your-api-token>
curl -s -X POST https://api.infidea.app/api/push \
  -H "X-Api-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"mode":"test"}'
curl -s -X POST https://api.infidea.app/api/refresh \
  -H "X-Api-Token: $TOKEN"
```
