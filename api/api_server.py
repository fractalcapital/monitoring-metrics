#!/usr/bin/env python3
"""Monitoring dashboard API (runs on the VPS, behind Cloudflare Tunnel).

The dashboard page is static (GitHub Pages) and holds no secrets, so it
cannot fetch market data or send a push by itself. This tiny service is
the single backend for both reading live data and triggering manual actions:

  GET  /health          -> {"ok": true} (tunnel / monitoring check)
  GET  /api/data        -> latest dashboard data.json (public read-only,
                           ETag + Cache-Control; no git commit needed)
  GET  /api/alert-state -> latest alert_state.json (public read-only)
  POST /api/refresh     -> start a background fetch job and return
                           {"status": "started"} at once (the fetch can take
                           up to a few minutes if fallback retries trigger;
                           Cloudflare cuts a tunnel request off at ~100s, so a
                           synchronous reply would 524). Poll GET /api/job for
                           the outcome.
  GET  /api/job         -> {"running": "refresh"|"push"|null,
                           "last_result": {...}|null}; no auth needed
                           (read-only)
  POST /api/push        -> {"mode": "test"|"status", "channels": [...]}; both
                           run synchronously and return per-channel results
                           (fast). "test" sends one test message; "status"
                           sends the summary built from the cached data.json
                           (MANUAL_PUSH=status-cached, no fetch). Channels
                           default to both; the dashboard has one button per
                           channel, rate-limited per channel so the two
                           buttons don't block each other.

Data files (data.json, alert_state.json) are stored in MM_OUT_DIR
(production: /var/lib/monitoring/data, decoupled from the git working tree;
local dev default: <repo>/dashboard).

Auth: the X-Api-Token header must match the API_TOKEN env var on POST
endpoints. The token is ALSO in the dashboard page source (public), so it is
NOT real security. The real protections are:
  - rate limits (1/min per IP, and a daily cap per endpoint), keyed by
    Cloudflare's non-spoofable CF-Connecting-IP header, and
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
import signal
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
DATA_DIR = Path(os.environ.get("MM_OUT_DIR") or str(REPO_DIR / "dashboard"))
ALLOWED_ORIGIN = "https://mon.infidea.app"
DASHBOARD_URL = "https://mon.infidea.app/dashboard/"
DATA_CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=300"

# Rate limits: (calls per 60s per IP, calls per day per IP, calls per day global)
LIMITS = {
    "/api/refresh": (1, 30, 60),
    "/api/push": (1, 10, 20),
}
FETCH_TIMEOUT = 300  # seconds; fallback retries can take a couple of minutes

_lock = threading.Lock()          # serialise rate-limit state and job slot
_hits = {}                        # (endpoint, ip, scope) -> [timestamps]
_global_hits = {}                 # endpoint -> [timestamps]


def _client_ip(headers, peer_ip):
    """Extract the real client IP for rate limiting.

    Behind Cloudflare Tunnel the TCP peer is always cloudflared on 127.0.0.1.
    Cloudflare overwrites CF-Connecting-IP with the real client IP (unlike
    X-Forwarded-For, whose leftmost entry a client can spoof). Fall back to
    the peer address for direct local access.
    """
    cf_ip = (headers.get("CF-Connecting-IP") or "").strip()
    if cf_ip:
        return cf_ip
    return peer_ip


def _rate_limited(endpoint, ip, scope=""):
    per_min, per_day, global_day = LIMITS[endpoint]
    now = time.time()
    with _lock:
        key = (endpoint, ip, scope)
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


def _run(cmd, env_extra=None, timeout=None):
    """Run cmd in the repo dir in its own process group; kill the whole group on timeout."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    limit = FETCH_TIMEOUT if timeout is None else timeout
    proc = subprocess.Popen(
        cmd, cwd=str(REPO_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=limit)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
        return None, "", "timed out after %ss" % limit


_PUSH_RE = re.compile(
    r"manual push (test|status) \(([^)]*)\): (.*)")


def _parse_push_results(out):
    """Parse the script's 'manual push ...' line into {channel: bool}."""
    m = _PUSH_RE.search(out)
    if not m:
        return None
    results = {}
    for part in m.group(3).split():
        if "=" in part:
            k, v = part.split("=", 1)
            results[k] = (v == "True")
    return results


def _data_dir():
    """Return the active runtime data directory (respects MM_OUT_DIR override)."""
    env_dir = os.environ.get("MM_OUT_DIR", "").strip()
    return Path(env_dir) if env_dir else DATA_DIR


def _read_json_bytes(filename):
    """Read raw UTF-8 bytes of a JSON file from DATA_DIR (fallback: repo dashboard/)."""
    candidates = [_data_dir() / filename]
    fallback = REPO_DIR / "dashboard" / filename
    if fallback != candidates[0]:
        candidates.append(fallback)
    for path in candidates:
        try:
            raw = path.read_bytes()
            # Ensure valid JSON so a corrupted file never gets served as 200.
            json.loads(raw.decode("utf-8"))
            return raw
        except (OSError, ValueError, UnicodeDecodeError):
            continue
    return None


def _do_refresh():
    """Fetch latest closes into DATA_DIR; return a response dict."""
    code, out, err = _run(
        ["bash", "scripts/push_data.sh"],
        {"MM_OUT_DIR": str(_data_dir())},
    )
    data = None
    alert_state = None
    try:
        data = json.loads((_data_dir() / "data.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        alert_state = json.loads((_data_dir() / "alert_state.json")
                                 .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if code != 0 or data is None:
        return {"ok": False,
                "error": "fetch failed: %s" % (err.strip()[-300:] or out.strip()[-300:])}
    resp = {"ok": True, "data": data}
    if alert_state is not None:
        resp["alert_state"] = alert_state
    return resp


def _do_push(mode, channels):
    """Send a test/status push to the given channels; return a dict."""
    channels = [c for c in channels if c in ("feishu", "bark")] or ["feishu", "bark"]
    env_base = {"MM_OUT_DIR": str(_data_dir())}
    if mode == "test":
        code, out, err = _run(
            ["python3", "scripts/fetch_dashboard.py"],
            {**env_base, "MANUAL_PUSH": "test",
             "MANUAL_CHANNELS": ",".join(channels)})
    elif mode == "status":
        # Cached: build the summary from the existing data.json without
        # fetching. Fast enough to answer synchronously (1-2s).
        code, out, err = _run(
            ["python3", "scripts/fetch_dashboard.py"],
            {**env_base, "MANUAL_PUSH": "status-cached",
             "MANUAL_CHANNELS": ",".join(channels)})
    else:
        return {"ok": False, "error": "mode must be test or status"}
    results = _parse_push_results(out)
    if code != 0 or results is None:
        return {"ok": False,
                "error": "push failed: %s" % (err.strip()[-300:] or out.strip()[-300:])}
    resp = {"ok": True, "mode": mode, "channels": channels,
            "feishu": results.get("feishu"),
            "bark": results.get("bark")}
    sent = [v for k, v in results.items() if k in channels]
    if not any(sent):
        resp["ok"] = False
        resp["error"] = "all requested channels failed (check the service log)"
    return resp


# One slow job at a time (a fetch can run minutes; the job guard, not the
# subprocess lock, serialises refresh jobs). Test/status pushes run
# synchronously and touch no data files, so they don't need the guard.
_job = {"running": None, "result": None}


def _start_job(kind, fn):
    """Start fn in a background thread; return an immediate reply.

    Returns {"ok": true, "status": "started", "job": kind} or, if a job
    is already running, {"ok": false, "error": ...}. The outcome lands
    in _job["result"] and is readable via GET /api/job.
    """
    with _lock:
        if _job["running"]:
            return {"ok": False,
                    "error": "a %s job is already running" % _job["running"]}
        _job["running"] = kind
        _job["result"] = None

    def _run_job():
        try:
            res = fn()
        except Exception as exc:  # never leave the job stuck "running"
            res = {"ok": False, "error": "job crashed: %s" % exc}
        with _lock:
            _job["running"] = None
            _job["result"] = res

    threading.Thread(target=_run_job, daemon=True).start()
    return {"ok": True, "status": "started", "job": kind}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # to stdout, picked up by journald
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def _send_json(self, status, payload, public_cors=False,
                   cache_control=None, head_only=False):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_raw_json(status, body, public_cors=public_cors,
                            cache_control=cache_control, head_only=head_only)

    def _send_raw_json(self, status, body, public_cors=False,
                       cache_control=None, etag=None, head_only=False):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        if etag:
            self.send_header("ETag", etag)
        if public_cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        else:
            origin = self.headers.get("Origin", "")
            if origin == ALLOWED_ORIGIN:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and not head_only:
            self.wfile.write(body)

    def _serve_data_file(self, filename, head_only=False):
        raw = _read_json_bytes(filename)
        if raw is None:
            self._send_json(404, {"ok": False, "error": "%s not found" % filename},
                            public_cors=True, cache_control="no-store",
                            head_only=head_only)
            return
        etag = '"%s"' % hashlib.sha256(raw).hexdigest()[:16]
        inm = (self.headers.get("If-None-Match") or "").strip()
        if inm == etag or inm == "W/" + etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", DATA_CACHE_CONTROL)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_raw_json(200, raw, public_cors=True,
                            cache_control=DATA_CACHE_CONTROL, etag=etag,
                            head_only=head_only)

    def do_OPTIONS(self):
        origin = self.headers.get("Origin", "")
        self.send_response(204)
        if origin == ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, X-Api-Token")
            self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only=False):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/health"):
            self._send_json(200, {
                "ok": True, "service": "monitoring-api",
                "time": dt.datetime.now(dt.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ")},
                public_cors=True, cache_control="no-store",
                head_only=head_only)
        elif path == "/api/data":
            self._serve_data_file("data.json", head_only=head_only)
        elif path == "/api/alert-state":
            self._serve_data_file("alert_state.json", head_only=head_only)
        elif path == "/api/job":
            # Copy snapshot under lock so slow socket writes never block _lock.
            with _lock:
                snap = {"running": _job["running"], "last_result": _job["result"]}
            self._send_json(200, snap, cache_control="no-store",
                            head_only=head_only)
        else:
            self._send_json(404, {"ok": False, "error": "not found"},
                            head_only=head_only)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/refresh", "/api/push"):
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
        ip = _client_ip(self.headers, self.client_address[0])
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
        scope = ""
        channels = []
        if path == "/api/push":
            # Rate-limit per channel so the Feishu and Bark buttons don't
            # block each other when clicked one after another.
            channels = [str(c).lower() for c in body.get("channels", [])
                        if str(c).lower() in ("feishu", "bark")]
            scope = ",".join(sorted(channels)) or "both"
        limited = _rate_limited(path, ip, scope)
        if limited:
            self._send_json(429, {"ok": False, "error": limited})
            return
        if path == "/api/refresh":
            # Async: the fetch can run minutes, past Cloudflare's ~100s
            # tunnel timeout. Start the job and let the page poll /api/job.
            self._send_json(200, _start_job("refresh", _do_refresh))
        else:
            # Both modes are fast and synchronous: "test" sends one test
            # message, "status" sends the summary from the cached data.json.
            mode = str(body.get("mode", "")).lower()
            self._send_json(200, _do_push(mode, channels))


def main():
    if not API_TOKEN:
        print("WARNING: API_TOKEN is not set -- all POSTs will fail",
              flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("monitoring-api listening on %s:%d repo=%s data_dir=%s"
          % (HOST, PORT, REPO_DIR, _data_dir()), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
