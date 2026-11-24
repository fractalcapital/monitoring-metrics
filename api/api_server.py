#!/usr/bin/env python3
"""Monitoring dashboard API (runs on the VPS, behind Cloudflare Tunnel).

The dashboard page is static (GitHub Pages) and holds no secrets, so it
cannot trigger a data fetch or send a push by itself. This tiny service
is the backend for the dashboard's two manual buttons:

  GET  /health       -> {"ok": true} (tunnel / monitoring check)
  POST /api/refresh  -> run the fetch script now, git-push data.json,
                        return the fresh data.json payload
  POST /api/push     -> {"mode": "test"|"status"}; run the fetch script
                        with MANUAL_PUSH=mode and return per-channel results

Auth: the X-Api-Token header must match the API_TOKEN env var. The token
is ALSO in the dashboard page source (public), so it is NOT real
security. The real protections are:
  - rate limits (1/min per IP, and a daily cap per endpoint), and
  - these endpoints can only send to the owner's Feishu/Bark and fetch
    public market data -- there is nothing else to steal here.
The webhook URL and Bark key live ONLY in this service's environment
(systemd EnvironmentFile on the VPS); they never go into the repo or
the page.

Stdlib only. Binds 127.0.0.1:8080 by default; Cloudflare Tunnel maps
https://api.infidea.app to it (no open ports, no cert to manage).
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("API_HOST", "127.0.0.1")
PORT = int(os.environ.get("API_PORT", "8080"))
API_TOKEN = os.environ.get("API_TOKEN", "")
REPO_DIR = Path(os.environ.get(
    "REPO_DIR", str(Path(__file__).resolve().parent.parent)))
ALLOWED_ORIGIN = "https://mon.infidea.app"
DASHBOARD_URL = "https://mon.infidea.app/dashboard/"

# Rate limits: (calls per 60s per IP, calls per day per IP, calls per day global)
LIMITS = {
    "/api/refresh": (1, 30, 60),
    "/api/push": (1, 10, 20),
}
FETCH_TIMEOUT = 300  # seconds; Yahoo retries can take a couple of minutes

_lock = threading.Lock()          # serialise fetch/push runs (git push)
_hits = {}                        # (endpoint, ip) -> [timestamps]
_global_hits = {}                 # endpoint -> [timestamps]


def _rate_limited(endpoint, ip):
    per_min, per_day, global_day = LIMITS[endpoint]
    now = time.time()
    with _lock:
        key = (endpoint, ip)
        ts = [t for t in _hits.get(key, []) if now - t < 86400]
        if len([t for t in ts if now - t < 60]) >= per_min:
            return "too fast: at most %d call(s) per minute" % per_min
        if len(ts) >= per_day:
            return "daily limit reached (%d/day)" % per_day
        gts = [t for t in _global_hits.get(endpoint, []) if now - t < 86400]
        if len(gts) >= global_day:
            return "daily limit reached (%d/day)" % global_day
        ts.append(now)
        _hits[key] = ts
        gts.append(now)
        _global_hits[endpoint] = gts
    return None


def _run(cmd, env_extra=None):
    """Run cmd in the repo dir; return (returncode, stdout, stderr)."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO_DIR), env=env, capture_output=True,
            text=True, timeout=FETCH_TIMEOUT)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return None, "", "timed out after %ss" % FETCH_TIMEOUT


_PUSH_RE = re.compile(
    r"manual push (test|status): feishu=(True|False) bark=(True|False)")


def _do_refresh():
    """Fetch now and publish data.json; return a response dict."""
    with _lock:
        code, out, err = _run(["bash", "scripts/push_data.sh"])
    pushed = "PUSH_OK" in out
    data = None
    try:
        data = json.loads((REPO_DIR / "dashboard" / "data.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if code != 0 or data is None:
        return {"ok": False,
                "error": "fetch failed: %s" % (err.strip()[-300:] or out.strip()[-300:]),
                "pushed": False}
    resp = {"ok": True, "pushed": pushed, "data": data}
    if not pushed:
        resp["warning"] = ("data fetched but git push failed; "
                           "the page shows the fresh data, but it will "
                           "revert on the next load until a push succeeds")
    return resp


def _do_push(mode):
    """Send a test/status push; return a response dict."""
    if mode == "test":
        with _lock:
            code, out, err = _run(
                ["python3", "scripts/fetch_dashboard.py"],
                {"MANUAL_PUSH": "test"})
        pushed = None
    elif mode == "status":
        with _lock:
            code, out, err = _run(
                ["bash", "scripts/push_data.sh"], {"MANUAL_PUSH": "status"})
        pushed = "PUSH_OK" in out
    else:
        return {"ok": False, "error": "mode must be test or status"}
    m = _PUSH_RE.search(out)
    if code != 0 or not m:
        return {"ok": False,
                "error": "push failed: %s" % (err.strip()[-300:] or out.strip()[-300:]),
                "pushed": pushed}
    resp = {"ok": True, "mode": mode,
            "feishu": m.group(2) == "True",
            "bark": m.group(3) == "True", "pushed": pushed}
    if not (resp["feishu"] or resp["bark"]):
        resp["ok"] = False
        resp["error"] = "both channels failed (check the service log)"
    return resp


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # to stdout, picked up by journald
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("Origin", "")
        if origin == ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        origin = self.headers.get("Origin", "")
        self.send_response(204)
        if origin == ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, X-Api-Token")
            self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/health"):
            self._send_json(200, {
                "ok": True, "service": "monitoring-api",
                "time": dt.datetime.now(dt.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ")})
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path not in ("/api/refresh", "/api/push"):
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if not API_TOKEN:
            self._send_json(500, {"ok": False,
                                  "error": "API_TOKEN not configured on the server"})
            return
        token = self.headers.get("X-Api-Token", "")
        if not hmac.compare_digest(token, API_TOKEN):
            self._send_json(401, {"ok": False, "error": "bad token"})
            return
        ip = self.client_address[0]
        limited = _rate_limited(self.path, ip)
        if limited:
            self._send_json(429, {"ok": False, "error": limited})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 4096:
            self._send_json(400, {"ok": False, "error": "body too large"})
            return
        raw = self.rfile.read(length) if length else b""
        body = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send_json(400, {"ok": False, "error": "bad JSON"})
                return
        if self.path == "/api/refresh":
            self._send_json(200, _do_refresh())
        else:
            self._send_json(200, _do_push(str(body.get("mode", "")).lower()))


def main():
    if not API_TOKEN:
        print("WARNING: API_TOKEN is not set -- all POSTs will fail",
              flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("monitoring-api listening on %s:%d repo=%s"
          % (HOST, PORT, REPO_DIR), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
