#!/usr/bin/env python3
"""Fetch live closes for the monitoring dashboard and write dashboard/data.json.

<<<<<<< Updated upstream
Sources (server-side only -- browsers are blocked by CORS on Yahoo/FRED):
  - Yahoo Finance v8 chart API (query1.finance.yahoo.com), range=1mo, interval=1d
    for ^VIX, ^VVIX, ^SKEW, ^MOVE, ^VIX1D, ^VIX9D, ^VIX3M, ^VIX6M, ^GSPC
  - FRED fredgraph.csv?id=BAMLH0A0HYM2 for HY OAS (ICE BofA US High Yield OAS)
  - Futu CLI (optional, VPS only): `futu kline` daily closes for VIX /
    VVIX / SKEW, enabled with FUTU_SOURCE=1 in the environment (e.g. the
    VPS .env). Futu index codes are US..VIX / US..VVIX / US..SKEW
    (NOT US.VIX -- that returns INVALID_SYMBOL). Futu is tried first
    for those three only; on any Futu failure the script falls back to
    Yahoo for that indicator, so a Futu outage never blocks a run.
    GitHub Actions has no Futu login, so it never sets FUTU_SOURCE and
    keeps using Yahoo. MOVE / SPX / term structure / HY OAS have no
    Futu source and always use Yahoo / FRED.
    The Futu token lives in ~/.futu/token.json, so the cron job must
    run as the same user who logged in (root on proud-pipes-7).
=======
Multi-source architecture (server-side only -- browsers are blocked by CORS):
  - VIX / VVIX / SKEW:
      1. Futu CLI (`futu kline` / `history-kline` / `quote`) when installed on VPS
      2. CBOE official CDN daily CSV (`cdn.cboe.com/.../*_History.csv`)
      3. Yahoo Finance (`curl_cffi` Chrome TLS impersonation + Cookie/Crumb)
  - VIX term structure (VIX1D / VIX9D / VIX3M / VIX6M):
      1. CBOE official CDN daily CSV
      2. Yahoo Finance (`curl_cffi` + Cookie/Crumb)
  - S&P 500 (SPX):
      1. Futu CLI (`US..SPX`)
      2. FRED (`SP500`, official JSON API if FRED_API_KEY is set, else CSV)
      3. Stooq daily CSV (`^spx`)
      4. Yahoo Finance (`^GSPC`)
  - MOVE Index (ICE BofA MOVE):
      Configurable via MOVE_SOURCE_ORDER (default: "yahoo,tradingview,thetrading_tools")
      1. Yahoo Finance (`^MOVE`, single symbol via `curl_cffi` + Cookie/Crumb)
      2. TradingView public scanner (`TVC:MOVE`) + incremental history upsert
      3. TheTrading.Tools daily CSV (`move-index.csv`)
  - HY OAS (ICE BofA US High Yield OAS, daily):
      1. FRED official JSON API (`BAMLH0A0HYM2`, requires FRED_API_KEY env var)
      2. FRED public CSV (`fredgraph.csv?id=BAMLH0A0HYM2`)
>>>>>>> Stashed changes

Fault isolation:
  - Every indicator resolves independently through its fallback chain.
  - If all providers fail for one indicator and a previous dashboard/data.json
    exists, that indicator keeps its last known value (marked stale=True) and
    triggers a fetch-failure alert (24h dedup), while all other indicators
    continue updating normally.
  - Only fails hard (exit non-zero) if no previous data.json exists to fall
    back to for a required indicator.

<<<<<<< Updated upstream
Alerts (Feishu robot webhook, server-side only):
  - Thresholds live in ALERT_THRESHOLDS below (from the repo READMEs' bands).
  - The webhook URL is read ONLY from the FEISHU_WEBHOOK_URL environment
    variable. It must never be written into any file in this repo (the repo
    is public) and must never be printed or logged. If it is unset, alerts
    are evaluated and recorded in dashboard/alert_state.json, but nothing
    is sent -- and the run still exits 0.
  - Dedup: alert_state.json records each indicator's last level + last sent
    time. A message is sent only on a new trigger, a level upgrade, or a
    same-level repeat after 24h. Same level within 24h is not re-sent.
  - The dashboard page never calls the webhook; it only reads
    dashboard/alert_state.json (same-origin) for display.

Alerts (Bark push, in parallel with Feishu):
  - Same triggers, same content, same dedup file. Bark is a GET to the
    user's own Bark server: BARK_URL env var holds only the base part
    (https://<bark server>/<key>, no title/body). Title/body are
    URL-encoded into the path; group=monitoring-metrics; level is
    "critical" for severe, "active" for warning.
  - The Bark base URL/key is never written to any file and never logged.
    If BARK_URL is unset, Bark is skipped silently. If one channel fails,
    only a warning is logged and the other channel still sends.

Manual push (MANUAL_PUSH env var, set by workflow_dispatch input or the
VPS API):
  - Set by the GitHub Actions workflow's manual_push input (none/test/status).
  - "test" sends one test message on both channels and exits without
    touching data.json. "status" runs the normal fetch first, then sends
    one status summary (current values + 波动情况 labels, same rules as
    the dashboard page's statusFor) on both channels. "status-cached"
    sends the same summary built from the existing data.json without
    fetching first: the dashboard push buttons use this, because from a
    datacenter IP the Yahoo fetch takes minutes (429 retries) and a push
    that fetches first feels broken. data.json is refreshed twice daily
    by cron and by manual refresh, and the message carries the data
    dates, so the reader can see how fresh it is. Manual pushes are
    never deduped -- every explicit run sends. They do not touch
    alert_state.json, so they never interfere with threshold alerts.
  - The dashboard page sends pushes through the VPS API
    (https://api.infidea.app, see api/README.md); it holds no webhook
    URL or Bark key itself (both live only on the VPS). If the API is
    down, the page links to the GitHub workflow page as a fallback.
=======
Secrets (env vars only -- NEVER commit to this public repo, NEVER log):
  - FRED_API_KEY       (optional, enables FRED official JSON API)
  - FEISHU_WEBHOOK_URL (optional, Feishu robot webhook)
  - BARK_URL           (optional, Bark push base URL)
  - YAHOO_PROXY        (optional, proxy URL for Yahoo requests only)
>>>>>>> Stashed changes
"""
import csv
import datetime as dt
import io
import json
import os
<<<<<<< Updated upstream
=======
import re
import shutil
>>>>>>> Stashed changes
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from curl_cffi import requests as cffi_requests  # type: ignore
except ImportError:
    cffi_requests = None

# Alert delivery lives in alert/notify.py. The script runs as
# `python3 scripts/fetch_dashboard.py` from the repo root, so
# sys.path[0] is scripts/ — add the repo root first so `alert` imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from alert.notify import send_bark, send_feishu_card  # noqa: E402

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}
PLAIN_UA = {"User-Agent": "Python-urllib/3.12"}

YAHOO_SYMBOLS = {
    "vix": "^VIX",
    "vvix": "^VVIX",
    "skew": "^SKEW",
    "move": "^MOVE",
    "vix1d": "^VIX1D",
    "vix9d": "^VIX9D",
    "vix3m": "^VIX3M",
    "vix6m": "^VIX6M",
    "spx": "^GSPC",
}

DEFAULT_FUTU_SYMBOLS = {
    "vix": "US..VIX",
    "vvix": "US..VVIX",
    "skew": "US..SKEW",
    "spx": "US..SPX",
}

CBOE_BASE = "https://cdn.cboe.com/api/global/us_indices/daily_prices/"
CBOE_FILES = {
    "vix": "VIX_History.csv",
    "vvix": "VVIX_History.csv",
    "skew": "SKEW_History.csv",
    "vix1d": "VIX1D_History.csv",
    "vix9d": "VIX9D_History.csv",
    "vix3m": "VIX3M_History.csv",
    "vix6m": "VIX6M_History.csv",
}

FRED_API_BASE = "https://api.stlouisfed.org/fred/series/observations"
FRED_CSV_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="
STOOQ_SPX_URL = "https://stooq.com/q/d/l/?s=^spx&i=d"
TV_SCAN_URL = "https://scanner.tradingview.com/global/scan"
TV_SYMBOL_URL = (
    "https://scanner.tradingview.com/symbol"
    "?symbol=TVC:MOVE&fields=close,change_abs,change"
)
TRADING_TOOLS_MOVE_URL = (
    "https://www.thetrading.tools/data/indicators/move-index.csv"
)

# Futu CLI source (VPS only, opt-in via FUTU_SOURCE=1). Verified
# 2026-11-25 on proud-pipes-7: `futu quote 'US..VIX' 'US..VVIX'
# 'US..SKEW' --format json` returns last_price / prev_close_price,
# and `futu kline 'US..VIX' --ktype 2 --num 5 --format json` returns
# {"kline_list": [{"date": 20261124, "close_price": 26.36, ...}]}
# with closes matching Yahoo (2026-11-23 VIX 26.62, 11-24 26.36;
# VVIX prev 117.36, SKEW prev 140.37). The kline date is an int
# YYYYMMDD; close_price is the daily close.
FUTU_SYMBOLS = {
    "vix": "US..VIX",
    "vvix": "US..VVIX",
    "skew": "US..SKEW",
}
FUTU_BIN = os.environ.get("FUTU_BIN", "futu")
FUTU_ENABLED = os.environ.get("FUTU_SOURCE", "").strip().lower() in (
    "1", "true", "yes", "on")

DASHBOARD_URL = "https://mon.infidea.app/dashboard/"

# --- Alert thresholds (from the repo READMEs' bands; edit here to change) ---
# warning = alert, critical = severe. Keys without "critical" only ever warn.
ALERT_THRESHOLDS = {
    "vix": {"label": "VIX 恐慌指数", "unit": "", "warning": 35, "critical": 40},
    "vvix": {"label": "VVIX 波动率", "unit": "", "warning": 140},
    "skew": {"label": "SKEW 黑天鹅", "unit": "", "warning": 150},
    "move": {"label": "MOVE 债市波动", "unit": "", "warning": 140},
    "hy_oas": {"label": "HY OAS 信用利差", "unit": "bp", "warning": 500, "critical": 1000},
    # Cross-check: VIX high AND (VVIX or MOVE high).
    "cross": {"label": "综合预警", "unit": "", "vix": 30, "vvix": 140, "move": 140},
}

LEVEL_SEVERITY = {"normal": 0, "warning": 1, "critical": 2}
LEVEL_LABEL = {"normal": "正常", "warning": "预警", "critical": "严重"}
RESEND_AFTER = dt.timedelta(hours=24)

<<<<<<< Updated upstream
# Always fetch direct, ignoring any system proxy: urllib's default
# opener follows the OS proxy settings, and on a Mac the user may route
# other traffic through a VPS proxy (Xray). The fetcher must use the
# machine's own IP — Yahoo rate-limits the VPS datacenter IP, so a
# proxied fetch would 429 exactly like the VPS does. On the VPS and
# Actions runners there is no proxy configured, so this changes nothing
# there.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

=======
FETCH_SOURCES = {
    "equities": "波动率与股指（Futu CLI / CBOE / Yahoo：VIX / VVIX / SKEW / 期限结构 / 标普 500）",
    "move": "MOVE 债市波动（Yahoo / TradingView / CSV）",
    "fred": "FRED（HY OAS）",
}
>>>>>>> Stashed changes

_YAHOO_SESSION_CACHE = {"session": None, "crumb": None, "initialized": False}


# ----------------------------------------------------------- HTTP helpers ---

def fetch_url(url, timeout=15, headers=None, retries=2, data=None,
              method=None, max_429_wait=15):
    """Fetch a URL via stdlib urllib with bounded retry on 429/transient errors.

    Honours Retry-After when present (capped by max_429_wait so fallback
    providers are tried promptly instead of hanging for minutes).
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
<<<<<<< Updated upstream
            req = urllib.request.Request(url, headers=headers or UA)
            with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
=======
            req = urllib.request.Request(
                url, data=data, headers=headers or UA, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
>>>>>>> Stashed changes
                if resp.status != 200:
                    raise RuntimeError("HTTP %s for %s" % (resp.status, url))
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code != 429 or attempt == retries:
                raise
            wait = None
            try:
                ra = int(exc.headers.get("Retry-After", "0") or 0)
                if ra > 0:
                    wait = min(ra, max_429_wait)
            except (ValueError, TypeError):
                pass
            if wait is None:
                wait = min([3, 8, 15][attempt] if attempt < 3 else 15,
                           max_429_wait)
            print("  429 for %s, retry %d/%d after %ds"
                  % (url, attempt + 1, retries, wait), file=sys.stderr)
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt == retries:
                raise
            time.sleep([2, 5, 10][attempt] if attempt < 3 else 10)
    raise last_exc


def _normalize_date(raw):
    """Parse common date strings (YYYY-MM-DD, MM/DD/YYYY, timestamps) -> YYYY-MM-DD."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        if ts > 1e8:
            return dt.datetime.fromtimestamp(
                ts, tz=dt.timezone.utc).date().isoformat()
        return None
    s = str(raw).strip()
    if not s:
        return None
    # "2026-11-23 00:00:00" or "2026-11-23T..."
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    # "11/23/2026" or "1/5/2026" (CBOE CSV format)
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(3)), int(m.group(1)), int(m.group(2)))
    # "2026/11/23"
    m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _finalize_pairs(pairs, round_digits=2, keep=40):
    """Deduplicate by date (latest wins), sort ascending, round values."""
    by_date = {}
    for item in pairs:
        d = item.get("date")
        v = item.get("value")
        if not d or v is None:
            continue
        val = round(float(v)) if round_digits == 0 else round(float(v), round_digits)
        by_date[d] = val
    out = [{"date": d, "value": by_date[d]} for d in sorted(by_date)]
    if keep and len(out) > keep:
        out = out[-keep:]
    return out


def _latest_us_weekday_iso(now_utc=None):
    """Best-effort latest US Eastern weekday date (YYYY-MM-DD)."""
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    # Approximate ET (UTC-5; good enough as a fallback date when VIX date is absent).
    et = now_utc - dt.timedelta(hours=5)
    d = et.date()
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def _upsert_history(prev_history, date, close_val, prev_close_val=None,
                    keep=30, round_digits=2):
    """Merge a single quote (date, close, optional prev_close) into prior history.

    Guards against US market holidays: if close_val (and prev_close_val when
    given) already match the last point in prev_history, keep the existing
    date instead of stamping a holiday date onto unchanged closes.
    """
    hist = _finalize_pairs(prev_history or [], round_digits=round_digits, keep=keep)
    close_r = (round(float(close_val)) if round_digits == 0
               else round(float(close_val), round_digits))
    prev_r = None
    if prev_close_val is not None:
        prev_r = (round(float(prev_close_val)) if round_digits == 0
                  else round(float(prev_close_val), round_digits))

    if hist:
        last = hist[-1]
        second_last = hist[-2] if len(hist) >= 2 else None
        same_last = abs(last["value"] - close_r) < 1e-6
        same_prev = (
            prev_r is None
            or (second_last is not None and abs(second_last["value"] - prev_r) < 1e-6)
        )
        if same_last and same_prev and date >= last["date"]:
            # Unchanged close (e.g. weekend / US holiday) -- keep real trading date.
            return hist
        if date <= last["date"]:
            # Update the matching date (or latest if date is slightly behind).
            hist[-1] = {"date": last["date"], "value": close_r}
            return hist[-keep:]

    if not hist and prev_r is not None:
        prev_day = (dt.date.fromisoformat(date) - dt.timedelta(days=1))
        while prev_day.weekday() >= 5:
            prev_day -= dt.timedelta(days=1)
        hist.append({"date": prev_day.isoformat(), "value": prev_r})
    hist.append({"date": date, "value": close_r})
    return _finalize_pairs(hist, round_digits=round_digits, keep=keep)


# ------------------------------------------------------- Provider: Futu ---

def _futu_symbols():
    symbols = dict(DEFAULT_FUTU_SYMBOLS)
    raw = os.environ.get("FUTU_SYMBOLS", "").strip()
    if raw:
        try:
            overrides = json.loads(raw)
            if isinstance(overrides, dict):
                for k, v in overrides.items():
                    if isinstance(v, str) and v.strip():
                        symbols[k] = v.strip()
        except ValueError:
            print("WARNING invalid FUTU_SYMBOLS JSON; using defaults",
                  file=sys.stderr)
    return symbols


def _futu_bin():
    if os.environ.get("USE_FUTU", "1").strip().lower() in ("0", "false", "no"):
        return None
    cli = os.environ.get("FUTU_CLI", "futu").strip() or "futu"
    found = shutil.which(cli)
    if found:
        return found
    if cli == "futu":
        for candidate in (
            "/usr/local/bin/futu",
            "/usr/bin/futu",
            str(Path.home() / ".local" / "bin" / "futu"),
            str(Path.home() / ".npm-global" / "bin" / "futu"),
        ):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _extract_json_from_stdout(text):
    """Parse JSON from CLI stdout, tolerating leading warning/banner lines."""
    s = (text or "").strip()
    if not s:
        raise RuntimeError("empty stdout")
    try:
        return json.loads(s)
    except ValueError:
        pass
    for idx, ch in enumerate(s):
        if ch in "[{":
            try:
                return json.loads(s[idx:])
            except ValueError:
                continue
    raise RuntimeError("could not parse JSON from futu output: %s" % s[:160])


def _extract_futu_rows(obj):
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in ("data", "list", "kl_list", "kline_list", "kline",
                    "rows", "items", "quotes", "result"):
            val = obj.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                nested = _extract_futu_rows(val)
                if nested:
                    return nested
        # Single quote object
        if any(k in obj for k in ("close_price", "close", "last_price")):
            return [obj]
    return []


def _parse_futu_kline_json(text):
    obj = _extract_json_from_stdout(text)
    rows = _extract_futu_rows(obj)
    pairs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = None
        for dk in ("time_key", "time", "data_date", "date", "trade_date", "timestamp"):
            if dk in row and row[dk] not in (None, ""):
                date = _normalize_date(row[dk])
                if date:
                    break
        val = None
        for vk in ("close_price", "close", "last_price"):
            if vk in row and row[vk] not in (None, ""):
                try:
                    val = float(row[vk])
                    break
                except (ValueError, TypeError):
                    pass
        if date and val is not None and val > 0:
            pairs.append({"date": date, "value": val})
    return _finalize_pairs(pairs, round_digits=2, keep=40)


def _parse_futu_quote_json(text):
    obj = _extract_json_from_stdout(text)
    rows = _extract_futu_rows(obj)
    for row in rows:
        if not isinstance(row, dict):
            continue
        last_price = None
        for vk in ("last_price", "close_price", "close", "price"):
            if vk in row and row[vk] not in (None, ""):
                try:
                    last_price = float(row[vk])
                    break
                except (ValueError, TypeError):
                    pass
        if last_price is None or last_price <= 0:
            continue
        prev_close = None
        for pk in ("prev_close_price", "prev_close", "last_close"):
            if pk in row and row[pk] not in (None, ""):
                try:
                    prev_close = float(row[pk])
                    break
                except (ValueError, TypeError):
                    pass
        date = None
        for dk in ("data_date", "update_time", "time", "date", "timestamp"):
            if dk in row and row[dk] not in (None, ""):
                date = _normalize_date(row[dk])
                if date:
                    break
        return last_price, prev_close, date
    raise RuntimeError("no valid quote in futu output")


def _run_futu_cmd(cmd, timeout=20):
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[-200:]
        raise RuntimeError("futu exit %d: %s" % (proc.returncode, err))
    return proc.stdout


def fetch_futu(symbol, prev_history=None):
    """Fetch daily closes via Futu CLI (`kline` -> `history-kline` -> `quote`)."""
    cli = _futu_bin()
    if not cli:
        raise RuntimeError("futu CLI not available")

    errors = []
    # 1. `futu kline <symbol> --ktype 2 --num 35 --autype 0 --format json`
    try:
        out = _run_futu_cmd([
            cli, "kline", symbol,
            "--ktype", "2", "--num", "35", "--autype", "0",
            "--format", "json",
        ])
        pairs = _parse_futu_kline_json(out)
        if len(pairs) >= 2:
            return pairs
        errors.append("kline returned %d bars" % len(pairs))
    except Exception as exc:  # noqa: BLE001
        errors.append("kline: %s" % exc)

    # 2. `futu history-kline <symbol> --start ... --end ... --format json`
    try:
        today = dt.datetime.now(dt.timezone.utc).date()
        start = (today - dt.timedelta(days=60)).isoformat()
        end = (today + dt.timedelta(days=1)).isoformat()
        out = _run_futu_cmd([
            cli, "history-kline", symbol,
            "--start", start, "--end", end,
            "--autype", "0", "--format", "json",
        ])
        pairs = _parse_futu_kline_json(out)
        if len(pairs) >= 2:
            return pairs
        errors.append("history-kline returned %d bars" % len(pairs))
    except Exception as exc:  # noqa: BLE001
        errors.append("history-kline: %s" % exc)

    # 3. `futu quote <symbol> --format json` + local history upsert
    try:
        out = _run_futu_cmd([cli, "quote", symbol, "--format", "json"])
        last_price, prev_close, date = _parse_futu_quote_json(out)
        use_date = date or _latest_us_weekday_iso()
        pairs = _upsert_history(prev_history, use_date, last_price, prev_close)
        if len(pairs) >= 2:
            return pairs
        errors.append("quote produced <2 points")
    except Exception as exc:  # noqa: BLE001
        errors.append("quote: %s" % exc)

    raise RuntimeError("futu %s failed (%s)" % (symbol, "; ".join(errors)))


# ------------------------------------------------------- Provider: CBOE ---

def _parse_csv_series(text, preferred_cols=("CLOSE",), scale=1.0,
                      round_digits=2, keep=40):
    """Parse a date/value CSV (handles CBOE, Stooq, FRED, and generic CSVs)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("empty CSV")
    reader = csv.reader(io.StringIO("\n".join(lines)))
    header = next(reader, None)
    if not header or len(header) < 2:
        raise RuntimeError("invalid CSV header: %s" % (header,))
    norm_header = [h.strip().upper().lstrip("\ufeff") for h in header]

    date_idx = 0
    for i, col in enumerate(norm_header):
        if "DATE" in col or col == "TIME":
            date_idx = i
            break

    val_idx = None
    for pref in preferred_cols:
        p = pref.upper()
        if p in norm_header:
            val_idx = norm_header.index(p)
            break
    if val_idx is None:
        # Fallback for 2-column CSVs like `DATE,VVIX` or `DATE,SKEW`
        val_idx = len(norm_header) - 1
        if val_idx == date_idx:
            val_idx = 1

    pairs = []
    for row in reader:
        if len(row) <= max(date_idx, val_idx):
            continue
        date = _normalize_date(row[date_idx])
        raw_val = row[val_idx].strip()
        if not date or not raw_val or raw_val in (".", "N/A", "nan", "NaN"):
            continue
        try:
            val = float(raw_val) * scale
        except ValueError:
            continue
        pairs.append({"date": date, "value": val})
    out = _finalize_pairs(pairs, round_digits=round_digits, keep=keep)
    if len(out) < 2:
        raise RuntimeError("too few valid CSV rows (%d)" % len(out))
    return out


def fetch_cboe(key):
    filename = CBOE_FILES.get(key)
    if not filename:
        raise RuntimeError("no CBOE CSV configured for %s" % key)
    url = CBOE_BASE + filename
    raw = fetch_url(url, timeout=20, headers=UA, retries=2)
    text = raw.decode("utf-8-sig", errors="replace")
    return _parse_csv_series(
        text,
        preferred_cols=("CLOSE", key.upper(), "VALUE"),
        round_digits=2,
        keep=40,
    )


# ------------------------------------------------------- Provider: FRED ---

def _fetch_fred_api(series_id, api_key, scale=1.0, round_digits=2):
    params = urllib.parse.urlencode({
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "45",
    })
    url = "%s?%s" % (FRED_API_BASE, params)
    # Never leak api_key in exception messages or logs.
    try:
        req = urllib.request.Request(url, headers=PLAIN_UA)
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                raise RuntimeError("FRED API HTTP %s for %s" % (resp.status, series_id))
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError("FRED API HTTP %s for %s" % (exc.code, series_id)) from None
    except Exception as exc:
        raise RuntimeError("FRED API error for %s: %s"
                           % (series_id, type(exc).__name__)) from None

    data = json.loads(raw.decode("utf-8"))
    obs = data.get("observations") or []
    pairs = []
    for item in obs:
        date = _normalize_date(item.get("date"))
        val = (item.get("value") or "").strip()
        if not date or not val or val == ".":
            continue
        try:
            pairs.append({"date": date, "value": float(val) * scale})
        except ValueError:
            continue
    out = _finalize_pairs(pairs, round_digits=round_digits, keep=40)
    if len(out) < 2:
        raise RuntimeError("too few FRED API observations for %s" % series_id)
    return out


def _fetch_fred_csv(series_id, scale=1.0, round_digits=2):
    # FRED hangs (read timeout) on browser-style UAs from datacenter IPs;
    # plain Python-urllib UA returns in <1s.
    url = FRED_CSV_BASE + urllib.parse.quote(series_id)
    raw = fetch_url(url, timeout=20, headers=PLAIN_UA, retries=2)
    text = raw.decode("utf-8", errors="replace")
    return _parse_csv_series(
        text,
        preferred_cols=(series_id, "VALUE"),
        scale=scale,
        round_digits=round_digits,
        keep=40,
    )


def fetch_fred_series(series_id, scale=1.0, round_digits=2):
    """Fetch a FRED series: official JSON API (if FRED_API_KEY set) -> CSV."""
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if api_key:
        try:
            return _fetch_fred_api(
                series_id, api_key, scale=scale, round_digits=round_digits), "fred_api"
        except Exception as exc:  # noqa: BLE001
            print("  FRED API %s failed (%s); falling back to fredgraph.csv"
                  % (series_id, exc), file=sys.stderr)
    return _fetch_fred_csv(
        series_id, scale=scale, round_digits=round_digits), "fred_csv"


# ------------------------------------------------------ Provider: Stooq ---

def fetch_stooq_spx():
    raw = fetch_url(STOOQ_SPX_URL, timeout=20, headers=UA, retries=2)
    text = raw.decode("utf-8", errors="replace")
    return _parse_csv_series(text, preferred_cols=("CLOSE",), round_digits=2, keep=40)


# ------------------------------------------------------ Provider: Yahoo ---

def _get_yahoo_cffi_session():
    """Return a cached (curl_cffi Session, crumb) tuple with Chrome TLS fingerprint."""
    if cffi_requests is None:
        return None, None
    if _YAHOO_SESSION_CACHE["initialized"]:
        return _YAHOO_SESSION_CACHE["session"], _YAHOO_SESSION_CACHE["crumb"]

    proxies = None
    proxy_url = os.environ.get("YAHOO_PROXY", "").strip()
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}

    sess = cffi_requests.Session(impersonate="chrome")
    if proxies:
        sess.proxies = proxies

    crumb = None
    try:
        # fc.yahoo.com intentionally returns 404 while setting the A3 cookie.
        sess.get("https://fc.yahoo.com", timeout=10)
    except Exception:  # noqa: BLE001
        pass

    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            r = sess.get("https://%s/v1/test/getcrumb" % host, timeout=10)
            if r.status_code == 200 and r.text and "<html" not in r.text.lower():
                crumb = r.text.strip()
                break
        except Exception:  # noqa: BLE001
            continue

    _YAHOO_SESSION_CACHE["session"] = sess
    _YAHOO_SESSION_CACHE["crumb"] = crumb
    _YAHOO_SESSION_CACHE["initialized"] = True
    return sess, crumb


def _reset_yahoo_cffi_session():
    _YAHOO_SESSION_CACHE["session"] = None
    _YAHOO_SESSION_CACHE["crumb"] = None
    _YAHOO_SESSION_CACHE["initialized"] = False


def _parse_yahoo_chart_json(raw_bytes, symbol):
    data = json.loads(raw_bytes.decode("utf-8"))
    result = data["chart"]["result"][0]
    stamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    pairs = []
    for ts, close in zip(stamps, closes):
        if close is None:
            continue
        date = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date().isoformat()
        pairs.append({"date": date, "value": round(float(close), 2)})
    out = _finalize_pairs(pairs, round_digits=2, keep=40)
    if len(out) < 2:
        raise RuntimeError("too few closes for %s: %s" % (symbol, out))
    return out


def fetch_yahoo(symbol, fast_fail=True):
    """Fetch 1mo daily chart from Yahoo Finance.

    Uses `curl_cffi` (Chrome TLS impersonation + Cookie/Crumb) when installed,
    falling back to stdlib `urllib`. When fast_fail=True (fallback providers
    exist), avoids long sleeps on 429 so failover happens within seconds.
    """
    sess, crumb = _get_yahoo_cffi_session()
    params = {"range": "1mo", "interval": "1d"}
    if crumb:
        params["crumb"] = crumb

    if sess is not None:
        last_exc = None
        retries = 1 if fast_fail else 2
        for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
            url = "https://%s/v8/finance/chart/%s" % (host, urllib.parse.quote(symbol))
            for attempt in range(retries + 1):
                try:
                    resp = sess.get(url, params=params, timeout=15)
                    if resp.status_code == 200:
                        return _parse_yahoo_chart_json(resp.content, symbol), "yahoo_cffi"
                    if resp.status_code == 429:
                        last_exc = RuntimeError("HTTP 429 Too Many Requests")
                        if attempt < retries:
                            time.sleep(2 if fast_fail else 8)
                            continue
                        break
                    if resp.status_code in (401, 403):
                        _reset_yahoo_cffi_session()
                    last_exc = RuntimeError("HTTP %d" % resp.status_code)
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if attempt < retries:
                        time.sleep(2)
                        continue
                    break
        raise RuntimeError("Yahoo(cffi) %s failed: %s" % (symbol, last_exc))

    # Stdlib urllib fallback when curl_cffi is not installed.
    query = "?range=1mo&interval=1d"
    last_exc = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = "https://%s/v8/finance/chart/%s%s" % (
            host, urllib.parse.quote(symbol), query)
        try:
            raw = fetch_url(
                url,
                timeout=15,
                headers=UA,
                retries=1 if fast_fail else 2,
                max_429_wait=5 if fast_fail else 15,
            )
            return _parse_yahoo_chart_json(raw, symbol), "yahoo_urllib"
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            print("  Yahoo %s via %s failed: %s" % (symbol, host, exc),
                  file=sys.stderr)
    raise last_exc


# ------------------------------------------- Providers: MOVE alternatives ---

def _parse_tradingview_move(raw_bytes, mode):
    data = json.loads(raw_bytes.decode("utf-8"))
    if mode == "scan":
        items = data.get("data") or []
        for item in items:
            cols = item.get("d") or []
            if cols and cols[0] is not None:
                close = float(cols[0])
                change_abs = float(cols[1]) if len(cols) > 1 and cols[1] is not None else None
                prev_close = (close - change_abs) if change_abs is not None else None
                return close, prev_close
        raise RuntimeError("TradingView scan returned no TVC:MOVE row")
    # single-symbol JSON
    close = data.get("close")
    if close is None:
        raise RuntimeError("TradingView symbol response missing close")
    close = float(close)
    change_abs = data.get("change_abs")
    prev_close = (close - float(change_abs)) if change_abs is not None else None
    return close, prev_close


def fetch_tradingview_move(prev_history=None, reference_date=None):
    """Fetch latest TVC:MOVE close from TradingView public scanner + upsert history."""
    errors = []
    close, prev_close = None, None

    # 1. POST /global/scan
    try:
        payload = json.dumps({
            "symbols": {"tickers": ["TVC:MOVE"]},
            "columns": ["close", "change_abs", "change"],
        }).encode("utf-8")
        headers = dict(UA)
        headers["Content-Type"] = "application/json"
        raw = fetch_url(TV_SCAN_URL, timeout=15, headers=headers,
                        retries=1, data=payload, method="POST")
        close, prev_close = _parse_tradingview_move(raw, "scan")
    except Exception as exc:  # noqa: BLE001
        errors.append("scan: %s" % exc)

    # 2. GET /symbol fallback
    if close is None:
        try:
            raw = fetch_url(TV_SYMBOL_URL, timeout=15, headers=UA, retries=1)
            close, prev_close = _parse_tradingview_move(raw, "symbol")
        except Exception as exc:  # noqa: BLE001
            errors.append("symbol: %s" % exc)

    if close is None:
        raise RuntimeError("TradingView TVC:MOVE failed (%s)" % "; ".join(errors))

    use_date = reference_date or _latest_us_weekday_iso()
    pairs = _upsert_history(prev_history, use_date, close, prev_close,
                            keep=30, round_digits=2)
    if len(pairs) < 2:
        raise RuntimeError("TradingView TVC:MOVE produced <2 points")
    return pairs


<<<<<<< Updated upstream
def parse_futu_kline(data):
    """Parse `futu kline --format json` output into [{date, value}].

    Accepts the verified shape {"kline_list": [{"date": 20261124,
    "close_price": 26.36, ...}]}. `date` is an int YYYYMMDD.
    """
    items = []
    if isinstance(data, dict):
        items = data.get("kline_list") or data.get("data") or []
    pairs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        date_raw = item.get("date")
        close = item.get("close_price")
        if date_raw is None or close is None:
            continue
        ds = str(date_raw).strip()
        if len(ds) != 8 or not ds.isdigit():
            continue
        pairs.append({"date": "%s-%s-%s" % (ds[:4], ds[4:6], ds[6:]),
                      "value": round(float(close), 2)})
    pairs.sort(key=lambda p: p["date"])
    # De-duplicate by date, keeping the last occurrence.
    deduped = {}
    for pair in pairs:
        deduped[pair["date"]] = pair
    pairs = [deduped[d] for d in sorted(deduped)]
    if len(pairs) < 2:
        raise RuntimeError("too few Futu closes: %s" % (pairs,))
    return pairs


def fetch_futu_kline(symbol, num=30):
    """Daily closes for one Futu symbol via `futu kline --ktype 2`.

    Runs the local CLI (needs a logged-in token in ~/.futu/token.json
    for the user running this script). The keyring warning on stderr
    is harmless and ignored; only a non-zero exit is an error.
    """
    cmd = [FUTU_BIN, "kline", symbol, "--ktype", "2",
           "--num", str(num), "--format", "json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("futu kline %s failed to run: %s"
                           % (symbol, exc))
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError("futu kline %s exited %d: %s"
                           % (symbol, proc.returncode, detail[-300:]))
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError("futu kline %s returned bad JSON: %s"
                           % (symbol, exc))
    return parse_futu_kline(data)


def fetch_fred_hy_oas():
    # FRED hangs (read timeout) on browser-style and unknown UAs from
    # datacenter IPs — verified: Mozilla and "monitoring-metrics/1.0"
    # both time out, while Python-urllib and curl UAs return in <1s.
    # Use the plain urllib UA for FRED only; Yahoo keeps the browser UA.
    raw = fetch_url(FRED_URL, timeout=20,
                    headers={"User-Agent": "Python-urllib/3.12"})
    text = raw.decode("utf-8")
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        val = (row.get("BAMLH0A0HYM2") or "").strip()
        # Header is "observation_date" on fredgraph.csv (not "DATE").
        date = ((row.get("observation_date") or row.get("DATE") or "")
                .strip())
        if not val or val == "." or not date:
            continue
        # FRED reports this series in percent (e.g. 3.76 means 3.76%).
        # Everything downstream (data.json, dashboard, alerts) uses bp,
        # so convert here: 3.76% -> 376bp. Do NOT treat the raw percent
        # as bp — that would make the 500bp alert threshold unreachable.
        rows.append({"date": date, "value": round(float(val) * 100)})
    if len(rows) < 2:
        raise RuntimeError("too few FRED rows: %s" % (rows,))
    return rows
=======
def fetch_trading_tools_move():
    """Fetch ICE BofA MOVE Index daily CSV from thetrading.tools."""
    raw = fetch_url(TRADING_TOOLS_MOVE_URL, timeout=20, headers=UA, retries=1)
    text = raw.decode("utf-8-sig", errors="replace")
    return _parse_csv_series(
        text,
        preferred_cols=("CLOSE", "MOVE", "VALUE"),
        round_digits=2,
        keep=40,
    )
>>>>>>> Stashed changes


# -------------------------------------------- Indicator resolution chains ---

def _prev_pairs_for(prev_payload, key):
    """Extract cached history pairs for an indicator from previous data.json."""
    if not isinstance(prev_payload, dict):
        return None
    try:
        if key == "spx":
            node = prev_payload["spx"]
            hist = node.get("history") or []
            if len(hist) >= 2:
                return _finalize_pairs(hist, round_digits=2, keep=40)
            return _finalize_pairs([
                {"date": node["prev_date"], "value": node["prev_close"]},
                {"date": node["date"], "value": node["value"]},
            ], round_digits=2, keep=40)
        if key in ("vix1d", "vix9d", "vix3m", "vix6m"):
            ts = prev_payload.get("term_structure") or {}
            if key in ts and ts.get("date"):
                v = float(ts[key])
                d = ts["date"]
                return [{"date": d, "value": v}, {"date": d, "value": v}]
            return None
        if key == "hy_oas":
            hy = prev_payload["indicators"]["hy_oas"]
            hist = hy.get("history") or []
            if len(hist) >= 2:
                return _finalize_pairs(hist, round_digits=0, keep=40)
            return _finalize_pairs([
                {"date": hy["prev_date"], "value": hy["prev_value_bp"]},
                {"date": hy["date"], "value": hy["value_bp"]},
            ], round_digits=0, keep=40)
        node = prev_payload["indicators"][key]
        hist = node.get("history") or []
        if len(hist) >= 2:
            return _finalize_pairs(hist, round_digits=2, keep=40)
        if node.get("prev_date") and node.get("prev_close") is not None:
            return _finalize_pairs([
                {"date": node["prev_date"], "value": node["prev_close"]},
                {"date": node["date"], "value": node["value"]},
            ], round_digits=2, keep=40)
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _try_providers(name, providers):
    """Run (label, fn) pairs in order; return (pairs, source_label) or raise."""
    errors = []
    for label, fn in providers:
        try:
            res = fn()
            if isinstance(res, tuple) and len(res) == 2:
                pairs, actual_label = res
            else:
                pairs, actual_label = res, label
            if pairs and len(pairs) >= 2:
                return pairs, actual_label
            errors.append("%s: too few points" % label)
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (label, exc))
            print("  [%s] %s failed: %s" % (name, label, exc), file=sys.stderr)
    raise RuntimeError("; ".join(errors))


def resolve_equities_and_term(prev_payload):
    """Fetch VIX, VVIX, SKEW, SPX, and VIX term structure with per-key fallback.

    Returns (pairs_map, sources_map, stale_keys, errors_list).
    """
    futu_map = _futu_symbols()
    has_futu = bool(_futu_bin())
    pairs_map = {}
    sources_map = {}
    stale_keys = set()
    errors = []

    # 1. Core CBOE volatility indices: Futu CLI -> CBOE official CSV -> Yahoo
    for key in ("vix", "vvix", "skew"):
        prev_hist = _prev_pairs_for(prev_payload, key)
        chain = []
        if has_futu and key in futu_map:
            sym = futu_map[key]
            chain.append(("futu", lambda s=sym, h=prev_hist: fetch_futu(s, h)))
        chain.append(("cboe", lambda k=key: fetch_cboe(k)))
        chain.append(("yahoo", lambda k=key: fetch_yahoo(YAHOO_SYMBOLS[k], fast_fail=True)))
        try:
            pairs_map[key], sources_map[key] = _try_providers(key, chain)
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (key, exc))
            if prev_hist and len(prev_hist) >= 2:
                pairs_map[key] = prev_hist
                sources_map[key] = "cached"
                stale_keys.add(key)
            else:
                raise RuntimeError("No data or cache for %s: %s" % (key, exc)) from exc

    # 2. VIX term structure (1D / 9D / 3M / 6M): CBOE official CSV -> Yahoo
    for key in ("vix1d", "vix9d", "vix3m", "vix6m"):
        prev_hist = _prev_pairs_for(prev_payload, key)
        chain = [
            ("cboe", lambda k=key: fetch_cboe(k)),
            ("yahoo", lambda k=key: fetch_yahoo(YAHOO_SYMBOLS[k], fast_fail=True)),
        ]
        try:
            pairs_map[key], sources_map[key] = _try_providers(key, chain)
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (key, exc))
            if prev_hist:
                pairs_map[key] = prev_hist
                sources_map[key] = "cached"
                stale_keys.add(key)
            else:
                raise RuntimeError("No data or cache for %s: %s" % (key, exc)) from exc

    # 3. S&P 500: Futu CLI -> FRED SP500 -> Stooq -> Yahoo
    prev_spx = _prev_pairs_for(prev_payload, "spx")
    spx_chain = []
    if has_futu and "spx" in futu_map:
        spx_sym = futu_map["spx"]
        spx_chain.append(("futu", lambda s=spx_sym, h=prev_spx: fetch_futu(s, h)))
    spx_chain.append(("fred", lambda: fetch_fred_series("SP500", scale=1.0, round_digits=2)))
    spx_chain.append(("stooq", fetch_stooq_spx))
    spx_chain.append(("yahoo", lambda: fetch_yahoo(YAHOO_SYMBOLS["spx"], fast_fail=True)))
    try:
        pairs_map["spx"], sources_map["spx"] = _try_providers("spx", spx_chain)
    except Exception as exc:  # noqa: BLE001
        errors.append("spx: %s" % exc)
        if prev_spx and len(prev_spx) >= 2:
            pairs_map["spx"] = prev_spx
            sources_map["spx"] = "cached"
            stale_keys.add("spx")
        else:
            raise RuntimeError("No data or cache for spx: %s" % exc) from exc

    return pairs_map, sources_map, stale_keys, errors


def resolve_move(prev_payload, reference_date=None):
    """Fetch MOVE Index via configurable multi-source fallback chain.

    Default order: yahoo (curl_cffi + crumb) -> tradingview -> thetrading_tools.
    Override with MOVE_SOURCE_ORDER env var (e.g. "tradingview,yahoo,thetrading_tools").
    Returns (pairs, source_label, is_stale, error_str_or_None).
    """
    prev_hist = _prev_pairs_for(prev_payload, "move")
    order_raw = os.environ.get(
        "MOVE_SOURCE_ORDER", "yahoo,tradingview,thetrading_tools").strip()
    order = [s.strip().lower() for s in order_raw.split(",") if s.strip()]
    if not order:
        order = ["yahoo", "tradingview", "thetrading_tools"]

    registry = {
        "yahoo": lambda: fetch_yahoo(YAHOO_SYMBOLS["move"], fast_fail=True),
        "tradingview": lambda: fetch_tradingview_move(
            prev_history=prev_hist, reference_date=reference_date),
        "thetrading_tools": fetch_trading_tools_move,
    }
    chain = [(name, registry[name]) for name in order if name in registry]
    if not chain:
        chain = [(k, registry[k]) for k in ("yahoo", "tradingview", "thetrading_tools")]

    try:
        pairs, src = _try_providers("move", chain)
        return pairs, src, False, None
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        if prev_hist and len(prev_hist) >= 2:
            return prev_hist, "cached", True, err
        raise RuntimeError("No data or cache for move: %s" % err) from exc


def resolve_hy_oas(prev_payload):
    """Fetch HY OAS (FRED API -> fredgraph.csv -> cached -> verified snapshot)."""
    try:
        rows, src = fetch_fred_series("BAMLH0A0HYM2", scale=100.0, round_digits=0)
        return rows, src, False, None
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        prev_rows = _prev_pairs_for(prev_payload, "hy_oas")
        if prev_rows and len(prev_rows) >= 2:
            return prev_rows, "cached", True, err
        # Verified 2026-11-20 snapshot fallback (352bp -> 376bp) on brand-new run.
        fallback = [
            {"date": "2026-11-19", "value": 352},
            {"date": "2026-11-20", "value": 376},
        ]
        return fallback, "snapshot", True, err


def indicator(pairs, stale=False, source=None):
    last = pairs[-1]
    prev = pairs[-2]
    if prev["value"]:
        change_pct = round((last["value"] - prev["value"]) / prev["value"] * 100, 2)
    else:
        change_pct = None
    out = {
        "value": last["value"],
        "date": last["date"],
        "prev_close": prev["value"],
        "prev_date": prev["date"],
        "change_pct": change_pct,
        "history": pairs[-30:],
    }
    if stale:
        out["stale"] = True
    if source:
        out["source"] = source
    return out


def _describe_sources(sources_map):
    label_map = {
        "futu": "Futu CLI",
        "cboe": "CBOE CDN",
        "fred_api": "FRED API",
        "fred_csv": "FRED CSV",
        "stooq": "Stooq",
        "yahoo_cffi": "Yahoo Finance",
        "yahoo_urllib": "Yahoo Finance",
        "tradingview": "TradingView",
        "thetrading_tools": "TheTrading.Tools",
        "cached": "缓存",
        "snapshot": "快照",
    }
    by_src = {}
    name_map = {
        "vix": "VIX", "vvix": "VVIX", "skew": "SKEW",
        "move": "MOVE", "hy_oas": "HY OAS", "spx": "SPX",
        "vix1d": "期限结构",
    }
    for key in ("vix", "vvix", "skew", "vix1d", "spx", "move", "hy_oas"):
        raw = sources_map.get(key)
        if not raw:
            continue
        lbl = label_map.get(raw, raw)
        by_src.setdefault(lbl, []).append(name_map[key])
    parts = ["%s (%s)" % (lbl, "/".join(items)) for lbl, items in by_src.items()]
    return "; ".join(parts)


# ---------------------------------------------------------------- alerts ---

def _level_for(value, spec):
    if "critical" in spec and value >= spec["critical"]:
        return "critical"
    if value >= spec["warning"]:
        return "warning"
    return "normal"


def evaluate_alerts(payload):
    """Return {key: {level, value, date, threshold, change}} for every key."""
    ind = payload["indicators"]
    out = {}
    for key in ("vix", "vvix", "skew", "move"):
        spec = ALERT_THRESHOLDS[key]
        d = ind[key]
        level = _level_for(d["value"], spec)
        threshold = spec["critical"] if level == "critical" else spec["warning"]
        if d.get("change_pct") is not None:
            change = "%+.2f%% vs %s" % (d["change_pct"], d["prev_date"])
        else:
            change = "-"
        out[key] = {
            "level": level,
            "value": d["value"],
            "date": d["date"],
            "threshold": threshold,
            "change": change,
        }
    hy = ind["hy_oas"]
    spec = ALERT_THRESHOLDS["hy_oas"]
    level = _level_for(hy["value_bp"], spec)
    threshold = spec["critical"] if level == "critical" else spec["warning"]
    out["hy_oas"] = {
        "level": level,
        "value": hy["value_bp"],
        "date": hy["date"],
        "threshold": threshold,
        "change": "%+.0fbp vs %s" % (hy["change_bp"], hy["prev_date"]),
    }
    # Cross-check: VIX >= 30 AND (VVIX >= 140 OR MOVE >= 140).
    cross = ALERT_THRESHOLDS["cross"]
    parts = []
    if ind["vvix"]["value"] >= cross["vvix"]:
        parts.append("VVIX %s >= %s" % (ind["vvix"]["value"], cross["vvix"]))
    if ind["move"]["value"] >= cross["move"]:
        parts.append("MOVE %s >= %s" % (ind["move"]["value"], cross["move"]))
    hit = ind["vix"]["value"] >= cross["vix"] and bool(parts)
    change = ("VIX >= 30 and " + ", ".join(parts)) if hit else "-"
    out["cross"] = {
        "level": "warning" if hit else "normal",
        "value": ind["vix"]["value"],
        "date": ind["vix"]["date"],
        "threshold": cross["vix"],
        "change": change,
    }
    return out


def load_alert_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("alerts"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"alerts": {}}


def _parse_utc(s):
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def format_alert_parts(key, ev):
<<<<<<< Updated upstream
    """Shared alert content for every channel: (title, body).

    Feishu gets title + body as one interactive card; Bark gets
    them as its title/body path segments. Same content on both channels.
    """
=======
    """Shared alert content for every channel: (title, body)."""
>>>>>>> Stashed changes
    spec = ALERT_THRESHOLDS[key]
    unit = spec.get("unit", "")
    level_cn = LEVEL_LABEL[ev["level"]]
    if key == "cross":
        title = "【监控报警·%s】综合预警" % level_cn
        body = ("触发条件：%s\n"
                "VIX 当前值：%s（日期 %s)" % (ev["change"], ev["value"], ev["date"]))
    else:
        title = "【监控报警·%s】%s" % (level_cn, spec["label"])
        body = ("当前值：%s%s（阈值 %s%s）\n"
                "日期：%s\n"
                "变化：%s" % (ev["value"], unit, ev["threshold"], unit,
                                ev["date"], ev["change"]))
    body = "%s\n查看仪表盘：%s" % (body, DASHBOARD_URL)
    return title, body


def _channel_should_send(old_level, new_level, old_sent_at, now_utc):
    """Shared dedup rule, applied per channel: send only on a new trigger,
    a level upgrade, or a same-level repeat after 24h."""
    if new_level == "normal":
        return False
    if old_level == "normal":
        return True
    if LEVEL_SEVERITY[new_level] > LEVEL_SEVERITY[old_level]:
        return True
    return old_sent_at is None or now_utc - old_sent_at >= RESEND_AFTER


# ------------------------------------------- data-fetch failure alerts ---

def _write_state(state_path, state):
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    Path(state_path).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def _fetch_alert_send(key, label, detail, state, now_utc):
    """Send a fetch-failure alert on Feishu + Bark with 24h dedup."""
    now_s = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    alerts = state.setdefault("alerts", {})
    old = alerts.get(key, {})
    old_level = old.get("level", "normal")
    old_channels = old.get("channels", {}) or {}
    channels_configured = {
        "feishu": bool(os.environ.get("FEISHU_WEBHOOK_URL")),
        "bark": bool(os.environ.get("BARK_URL")),
    }
    state["channels_configured"] = channels_configured
    title = "【监控报警·预警】数据获取失败"
    body = ("数据源：%s\n"
            "失败原因：%s\n"
            "时间：%s\n"
            "仪表盘该项暂时保留上一次成功拉取的数据（带日期），其他正常数据源已继续更新，"
            "请及时检查。\n"
            "查看仪表盘：%s"
            % (label, detail, now_s, DASHBOARD_URL))
    channels = {}
    for ch in ("feishu", "bark"):
        ch_old = old_channels.get(ch, {}) or {}
        channels[ch] = {
            "last_sent_at_utc": ch_old.get("last_sent_at_utc"),
            "last_sent_value": ch_old.get("last_sent_value"),
        }
    entry = {
        "level": "warning",
        "level_label": "预警",
        "value": detail,
        "date": now_utc.date().isoformat(),
        "threshold": "-",
        "change": label,
        "last_sent_at_utc": old.get("last_sent_at_utc"),
        "last_sent_value": old.get("last_sent_value"),
        "channels": channels,
    }
    sent = []
    for ch, sender in (("feishu", lambda: send_feishu_card(title, body,
                                                            "orange")),
                        ("bark", lambda: send_bark(title, body, "warning"))):
        ch_old = entry["channels"].get(ch, {}) or {}
        ch_sent_at = _parse_utc(ch_old.get("last_sent_at_utc"))
        if not _channel_should_send(old_level, "warning",
                                    ch_sent_at, now_utc):
            continue
        if not channels_configured[ch]:
            print("  fetch alert %s (no %s configured, not sent)"
                  % (key, ch))
            continue
        if sender():
            entry["channels"][ch] = {"last_sent_at_utc": now_s,
                                     "last_sent_value": detail}
            sent.append(ch)
    if sent:
        sent_at_list = [c["last_sent_at_utc"]
                        for c in entry["channels"].values()
                        if c.get("last_sent_at_utc")]
        if sent_at_list:
            entry["last_sent_at_utc"] = max(sent_at_list)
        entry["last_sent_value"] = detail
    alerts[key] = entry
    return sent


def record_fetch_failure_in_state(source_key, detail, state, now_utc):
    """Record a source failure in-memory and send fetch-failure alert (24h dedup)."""
    now_s = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    status = state.setdefault("fetch_status", {})
    prev = status.get(source_key, {}) or {}
    status[source_key] = {
        "ok": False,
        "last_error": detail,
        "last_failed_at_utc": now_s,
        "last_ok_at_utc": prev.get("last_ok_at_utc"),
    }
    sent = _fetch_alert_send(
        "fetch_" + source_key,
        FETCH_SOURCES.get(source_key, source_key),
        detail,
        state,
        now_utc,
    )
    print("  fetch failure %s: %s (alert sent via: %s)"
          % (source_key, detail, sent or "none"))
    return sent


def record_fetch_success(source_key, state, now_utc, detail=None):
    """Mark a source healthy in-memory; clears warning level so next failure alerts."""
    now_s = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    status = state.setdefault("fetch_status", {})
    prev = status.get(source_key, {}) or {}
    entry = {
        "ok": True,
        "last_error": None,
        "last_failed_at_utc": prev.get("last_failed_at_utc"),
        "last_ok_at_utc": now_s,
    }
    if detail:
        entry["provider"] = detail
    status[source_key] = entry
    key = "fetch_" + source_key
    old = (state.get("alerts", {}) or {}).get(key, {})
    if old.get("level") != "normal":
        state.setdefault("alerts", {})[key] = {
            "level": "normal",
            "level_label": "正常",
            "value": "ok",
            "date": now_utc.date().isoformat(),
            "threshold": "-",
            "change": FETCH_SOURCES.get(source_key, source_key),
            "last_sent_at_utc": old.get("last_sent_at_utc"),
            "last_sent_value": old.get("last_sent_value"),
            "channels": old.get("channels", {}),
        }


def process_alerts(payload, state_path, now_utc, group_errors=None,
                   group_providers=None):
    """Evaluate, dedup and (maybe) send alerts on Feishu + Bark."""
    group_errors = group_errors or {}
    group_providers = group_providers or {}
    state = load_alert_state(state_path)
    now_s = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    channels_configured = {
        "feishu": bool(os.environ.get("FEISHU_WEBHOOK_URL")),
        "bark": bool(os.environ.get("BARK_URL")),
    }

    # Update per-group fetch health accurately (never overwrite a failed group
    # with success on the same run).
    fetch_status = dict(state.get("fetch_status", {}) or {})
    # Drop legacy single-"yahoo" key once replaced by structured groups.
    fetch_status.pop("yahoo", None)
    state["fetch_status"] = fetch_status
    for gkey in ("equities", "move", "fred"):
        if gkey in group_errors:
            record_fetch_failure_in_state(gkey, group_errors[gkey], state, now_utc)
        else:
            record_fetch_success(
                gkey, state, now_utc, detail=group_providers.get(gkey))

    evaluated = evaluate_alerts(payload)
    alerts = state.setdefault("alerts", {})
    sent = []
    for key, ev in evaluated.items():
        old = alerts.get(key, {})
        old_level = old.get("level", "normal")
        old_channels = old.get("channels", {}) or {}
        new_level = ev["level"]
        title, body = format_alert_parts(key, ev)
        channels = {}
        for ch in ("feishu", "bark"):
            ch_old = old_channels.get(ch, {}) or {}
            channels[ch] = {
                "last_sent_at_utc": ch_old.get("last_sent_at_utc"),
                "last_sent_value": ch_old.get("last_sent_value"),
            }
        entry = {
            "level": new_level,
            "level_label": LEVEL_LABEL[new_level],
            "value": ev["value"],
            "date": ev["date"],
            "threshold": ev["threshold"],
            "change": ev["change"],
            "last_sent_at_utc": old.get("last_sent_at_utc"),
            "last_sent_value": old.get("last_sent_value"),
            "channels": channels,
        }
        any_sent = False
        # Header colour follows the alert level: warning orange,
        # critical red.
        template = {"warning": "orange", "critical": "red"}.get(new_level,
                                                                "blue")
        for ch, sender in (("feishu", lambda: send_feishu_card(title, body,
                                                                template)),
                            ("bark", lambda: send_bark(title, body, new_level))):
            ch_old = entry["channels"].get(ch, {}) or {}
            ch_sent_at = _parse_utc(ch_old.get("last_sent_at_utc"))
            if not _channel_should_send(old_level, new_level,
                                        ch_sent_at, now_utc):
                continue
            if not channels_configured[ch]:
                print("  alert %s=%s (no %s configured, not sent)"
                      % (key, new_level, ch))
                continue
            if sender():
                entry["channels"][ch] = {"last_sent_at_utc": now_s,
                                         "last_sent_value": ev["value"]}
                any_sent = True
        if any_sent:
            sent_at_list = [c["last_sent_at_utc"]
                            for c in entry["channels"].values()
                            if c.get("last_sent_at_utc")]
            if sent_at_list:
                entry["last_sent_at_utc"] = max(sent_at_list)
            entry["last_sent_value"] = ev["value"]
            sent.append(key)
        alerts[key] = entry

    new_state = {
        "updated_at_utc": now_s,
        "channels_configured": channels_configured,
        "fetch_status": state.get("fetch_status", {}),
        "alerts": alerts,
    }
    _write_state(state_path, new_state)
    return new_state, sent


# ------------------------------------------------------- manual push ---
# 波动情况 labels: must mirror the dashboard page's STATUS_BANDS /
# statusFor in dashboard/index.html exactly (same bands, same calm
# predicates, same 异动 override).
STATUS_BANDS = {
    "vix": {"bands": [(20, "平静", "b-green"), (30, "警惕", "b-amber"),
                      (40, "紧张", "b-orange"), (float("inf"), "恐慌", "b-red")],
            "calm": lambda v: v < 20},
    "vvix": {"bands": [(70, "偏低", "b-green"), (120, "正常", "b-green"),
                       (140, "偏高", "b-amber"), (float("inf"), "极端", "b-red")],
             "calm": lambda v: 70 <= v < 120},
    "skew": {"bands": [(120, "尾部风险定价正常", "b-green"),
                       (140, "尾部风险定价略高", "b-amber"),
                       (float("inf"), "尾部风险定价偏高", "b-red")],
             "calm": lambda v: v < 120},
    "move": {"bands": [(60, "债市平静", "b-green"), (140, "债市常态", "b-green"),
                       (float("inf"), "债市紧张", "b-red")],
             "calm": lambda v: 60 <= v < 140},
    "hy_oas": {"bands": [(300, "信用偏紧", "b-green"), (500, "信用常态", "b-green"),
                         (800, "信用走阔", "b-amber"), (1000, "信用紧张", "b-orange"),
                         (float("inf"), "信用极端", "b-red")],
               "calm": lambda v: 300 <= v < 500},
}
_STATUS_SEV = {"b-green": 0, "b-amber": 1, "b-orange": 2, "b-red": 3}
_VERDICT_TITLES = {"b-green": "整体平静", "b-amber": "有所升温，未到恐慌",
                   "b-orange": "压力明显", "b-red": "高度紧张"}


def status_label(key, value, change_pct):
    """波动情况 label + class for one indicator (mirrors the page)."""
    spec = STATUS_BANDS[key]
    label, cls = spec["bands"][-1][1], spec["bands"][-1][2]
    for maximum, lab, c in spec["bands"]:
        if value < maximum:
            label, cls = lab, c
            break
    if (spec["calm"](value) and change_pct is not None
            and abs(change_pct) >= 10):
        return {"label": "异动", "cls": "b-amber"}
    return {"label": label, "cls": cls}


def _signed(value, decimals=2):
    return "%s%.*f" % ("+" if value > 0 else "", decimals, value)


def build_status_message(payload, stale=False):
    """(title, body, template) for a manual status push. No state is touched.

    template is the card header colour, from the worst 波动情况 class:
    b-green -> green, b-amber -> yellow, b-orange -> orange, b-red -> red.
    """
    ind = payload["indicators"]
    hy = ind["hy_oas"]
    hy_pct = (hy["change_bp"] / hy["prev_value_bp"] * 100
              if hy.get("prev_value_bp") else None)
    st = {
        "vix": status_label("vix", ind["vix"]["value"], ind["vix"]["change_pct"]),
        "vvix": status_label("vvix", ind["vvix"]["value"], ind["vvix"]["change_pct"]),
        "skew": status_label("skew", ind["skew"]["value"], ind["skew"]["change_pct"]),
        "move": status_label("move", ind["move"]["value"], ind["move"]["change_pct"]),
        "hy_oas": status_label("hy_oas", hy["value_bp"], hy_pct),
    }
    worst_cls = max((st[k]["cls"] for k in ("vix", "move", "hy_oas")),
                    key=lambda c: _STATUS_SEV[c])
    spx = payload["spx"]
    spx_abs = (spx["value"] - spx["prev_close"]
               if spx.get("prev_close") is not None else None)
    lines = ["综合判断：%s" % _VERDICT_TITLES[worst_cls]]
    if stale:
<<<<<<< Updated upstream
        lines.append("注意：数据拉取失败，以下是上次成功数据")
    lines.append("%s %s（%s，%s%%）· %s" % (
        ALERT_THRESHOLDS["vix"]["label"],
=======
        lines.append("注意：部分数据源本次拉取失败，相应项保留上次成功数据")
    lines.append("VIX %s（%s，%s%%）· %s" % (
>>>>>>> Stashed changes
        ind["vix"]["value"],
        _signed(ind["vix"]["value"] - ind["vix"]["prev_close"]),
        _signed(ind["vix"]["change_pct"]), st["vix"]["label"]))
    lines.append("%s %s（%s，%s%%）· %s" % (
        ALERT_THRESHOLDS["vvix"]["label"],
        ind["vvix"]["value"],
        _signed(ind["vvix"]["value"] - ind["vvix"]["prev_close"]),
        _signed(ind["vvix"]["change_pct"]), st["vvix"]["label"]))
    lines.append("%s %s（%s，%s%%）· %s" % (
        ALERT_THRESHOLDS["skew"]["label"],
        ind["skew"]["value"],
        _signed(ind["skew"]["value"] - ind["skew"]["prev_close"]),
        _signed(ind["skew"]["change_pct"]), st["skew"]["label"]))
    lines.append("%s %s（%s，%s%%）· %s" % (
        ALERT_THRESHOLDS["move"]["label"],
        ind["move"]["value"],
        _signed(ind["move"]["value"] - ind["move"]["prev_close"]),
        _signed(ind["move"]["change_pct"]), st["move"]["label"]))
    lines.append("%s %sbp（%sbp，%s%%）· %s" % (
        ALERT_THRESHOLDS["hy_oas"]["label"],
        hy["value_bp"], _signed(hy["change_bp"], 0),
        _signed(hy_pct) if hy_pct is not None else "—", st["hy_oas"]["label"]))
    lines.append("标普 500 %s（%s，%s%%）" % (
        format(spx["value"], ","),
        _signed(spx_abs) if spx_abs is not None else "—",
        _signed(spx["change_pct"])))
<<<<<<< Updated upstream
    # VIX term structure (extra context: the curve shape says whether
    # the market prices near-term fear above/below longer-dated fear).
    ts = payload.get("term_structure") or {}
    ts_vals = [ts.get(k) for k in ("vix1d", "vix9d", "vix3m", "vix6m")]
    if all(v is not None for v in ts_vals):
        lines.append("期限结构：1D %.2f / 9D %.2f / 3M %.2f / 6M %.2f"
                     % tuple(ts_vals))
    lines.append("数据：VIX 等 %s，HY OAS %s" % (
        ind["vix"]["date"], hy["date"]))
=======
    lines.append("数据：VIX %s，MOVE %s，HY OAS %s" % (
        ind["vix"]["date"], ind["move"]["date"], hy["date"]))
    stale_notes = []
    if ind["move"].get("stale"):
        stale_notes.append("MOVE")
>>>>>>> Stashed changes
    if hy.get("stale"):
        stale_notes.append("HY OAS")
    if stale_notes:
        lines.append("（%s 为上次缓存值，本次未拉到）" % " / ".join(stale_notes))
    lines.append("查看仪表盘：%s" % DASHBOARD_URL)
    template = {"b-green": "green", "b-amber": "yellow",
                "b-orange": "orange", "b-red": "red"}[worst_cls]
    return "【手动推送】市场监控状态", "\n".join(lines), template


<<<<<<< Updated upstream
def send_manual_push(mode, payload=None, stale=False,
                     channels=("feishu", "bark")):
    """Send a manual push on the given channels. No dedup, no state writes.

    mode "test": one test message, no payload needed. mode "status":
    a status summary built from payload. channels selects a subset
    ("feishu" and/or "bark"); the dashboard has one button per channel.
    Returns True if at least one requested channel succeeded. A missing
    channel is skipped, never fatal.
    """
=======
def send_manual_push(mode, payload=None, stale=False):
    """Send a manual push on both channels. No dedup, no state writes."""
>>>>>>> Stashed changes
    now_bj = dt.datetime.now(
        dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    if mode == "test":
        title = "【测试】监控报警通道测试"
        body = ("这是一条测试消息，飞书和 Bark 通道已接通。\n"
                "时间：%s（北京时间）\n查看仪表盘：%s"
                % (now_bj, DASHBOARD_URL))
        template = "blue"
    elif mode == "status":
        title, body, template = build_status_message(payload, stale=stale)
    else:
        return False
    results = {}
    if "feishu" in channels:
        results["feishu"] = send_feishu_card(title, body, template)
    if "bark" in channels:
        results["bark"] = send_bark(title, body, "warning")
    print("  manual push %s (%s): %s"
          % (mode, ",".join(channels),
             " ".join("%s=%s" % (k, v) for k, v in results.items())))
    return bool(any(results.values()))


# ------------------------------------------------------------------ main ---

def main():
    root = Path(__file__).resolve().parent.parent
    out_path = root / "dashboard" / "data.json"
    state_path = root / "dashboard" / "alert_state.json"

<<<<<<< Updated upstream
    # Manual push from the workflow_dispatch input or the VPS API
    # (dashboard push buttons). "test" sends one test message and exits;
    # "status" fetches normally, then sends a status summary.
    # "status-cached" sends the same summary from the existing data.json
    # without fetching: the dashboard buttons use this, because from a
    # datacenter IP the Yahoo fetch takes minutes (429 retries).
    # MANUAL_CHANNELS (comma-separated feishu/bark, default both) selects
    # which channels a manual push goes to; the dashboard has one button
    # per channel and passes it through the VPS API.
=======
>>>>>>> Stashed changes
    mode = os.environ.get("MANUAL_PUSH", "").strip().lower()
    channels = tuple(
        c.strip().lower()
        for c in os.environ.get("MANUAL_CHANNELS", "feishu,bark").split(",")
        if c.strip().lower() in ("feishu", "bark")) or ("feishu", "bark")
    if mode not in ("", "none", "test", "status", "status-cached"):
        print("WARNING unknown MANUAL_PUSH=%r, ignoring" % (mode,),
              file=sys.stderr)
        mode = ""
    if mode == "test":
        send_manual_push("test", channels=channels)
        return
    if mode == "status-cached":
        if not out_path.exists():
            print("ERROR status-cached: no data.json to push from",
                  file=sys.stderr)
            sys.exit(1)
        cached = json.loads(out_path.read_text(encoding="utf-8"))
        send_manual_push("status", cached, channels=channels)
        return

<<<<<<< Updated upstream
    # Per-symbol stale fallback (added 2026-11-25): a single Yahoo
    # symbol failing (e.g. ^MOVE 429) used to abort the whole run and
    # throw away the fresh Futu closes. Now a failed symbol falls back
    # to its own history from the previous data.json, the run continues
    # with the other fresh values, and the failed keys are listed in
    # payload["stale_indicators"] and reported via a fetch-failure
    # alert (24h dedup). Only a first run with no previous data to
    # fall back to still fails hard.
    prev_payload = None
    if out_path.exists():
        try:
            prev_payload = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev_payload = None

    def prev_pairs(key):
        if not prev_payload:
            return None
        if key in ("vix1d", "vix9d", "vix3m", "vix6m"):
            ts = prev_payload.get("term_structure") or {}
            if ts.get(key) is not None and ts.get("date"):
                return [{"date": ts["date"], "value": ts[key]}]
            return None
        if key == "spx":
            ind = prev_payload.get("spx") or {}
        else:
            ind = (prev_payload.get("indicators") or {}).get(key) or {}
        hist = ind.get("history") or []
        if len(hist) >= 2:
            return [{"date": h["date"], "value": h["value"]}
                    for h in hist]
        if (ind.get("value") is not None
                and ind.get("prev_close") is not None
                and ind.get("date") and ind.get("prev_date")):
            return [{"date": ind["prev_date"], "value": ind["prev_close"]},
                    {"date": ind["date"], "value": ind["value"]}]
        return None

    pairs = {}
    futu_used = []
    failed_keys = []
    for key, symbol in YAHOO_SYMBOLS.items():
        if FUTU_ENABLED and key in FUTU_SYMBOLS:
            try:
                pairs[key] = fetch_futu_kline(FUTU_SYMBOLS[key], num=30)
                futu_used.append(key)
                continue
            except Exception as exc:  # noqa: BLE001 - fall back to Yahoo
                print("WARNING Futu %s failed (%s); falling back to Yahoo"
                      % (key, exc), file=sys.stderr)
        try:
            pairs[key] = fetch_yahoo(symbol)
        except Exception as exc:
            # Yahoo 429s shared Actions-runner / datacenter IPs.
            print("ERROR fetching %s: %s" % (symbol, exc), file=sys.stderr)
            fallback = prev_pairs(key)
            if fallback is not None:
                pairs[key] = fallback
                failed_keys.append(key)
                print("  %s: using previous data.json values "
                      "(date %s)" % (key, fallback[-1]["date"]),
                      file=sys.stderr)
            elif out_path.exists():
                # No usable previous value for this symbol: keep the
                # old whole-file behaviour (alert on stale data, exit 0).
                _now = dt.datetime.now(dt.timezone.utc)
                record_fetch_failure(
                    "yahoo", "%s: %s" % (symbol, exc),
                    state_path, _now)
                try:
                    prev_payload2 = json.loads(
                        out_path.read_text(encoding="utf-8"))
                    now_utc = dt.datetime.now(dt.timezone.utc)
                    new_state, sent = process_alerts(
                        prev_payload2, state_path, now_utc, hy_stale=True)
                    print("Wrote %s (stale-data alert check, sent: %s)"
                          % (state_path, sent or "none"))
                    if mode == "status":
                        send_manual_push("status", prev_payload2, stale=True,
                                         channels=channels)
                except Exception as exc2:  # noqa: BLE001
                    print("WARNING stale-data alert check failed: %s"
                          % (exc2,), file=sys.stderr)
                return
            else:
                sys.exit(1)
        time.sleep(1)  # don't burst 9 requests at Yahoo in 3 seconds

    try:
        fred_rows = fetch_fred_hy_oas()
        hy_stale = False
    except Exception as exc:
        # HY OAS is daily-only: never let a flaky FRED fetch block the
        # intraday update. Fall back to the last known value from the
        # previous data.json, or to the verified 2026-11-20 snapshot
        # (3.76%, prev 2026-11-19 3.52%, from FRED) on a first run.
        # (TimeoutError/OSError included: FRED intermittently hangs.)
        print("WARNING fetching FRED HY OAS failed (%s); "
              "using last known value" % (exc,), file=sys.stderr)
        hy_stale = True
        # Alert the user: HY OAS is now stale. The dashboard keeps the
        # last known value (with its date), but the user asked to know
        # whenever a source cannot be fetched.
        record_fetch_failure(
            "fred", str(exc), state_path,
            dt.datetime.now(dt.timezone.utc))
        fred_rows = None
=======
    prev_payload = None
    if out_path.exists():
>>>>>>> Stashed changes
        try:
            prev_payload = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev_payload = None

    group_errors = {}
    group_providers = {}

    # 1. VIX / VVIX / SKEW / VIX term structure / SPX
    try:
        pairs, sources_map, stale_eq, eq_errors = resolve_equities_and_term(prev_payload)
    except Exception as exc:
        print("FATAL equities fetch failed with no fallback: %s" % exc,
              file=sys.stderr)
        sys.exit(1)

    if eq_errors:
        group_errors["equities"] = "; ".join(eq_errors)[:300]
    else:
        eq_used = sorted({sources_map[k] for k in ("vix", "vvix", "skew", "vix1d", "spx")
                          if k in sources_map})
        group_providers["equities"] = "+".join(eq_used)

    # Use VIX trading date as reference for MOVE quote-only fallback (TradingView).
    ref_date = pairs["vix"][-1]["date"] if "vix" in pairs else None

    # 2. MOVE Index
    try:
        move_pairs, move_src, move_stale, move_err = resolve_move(
            prev_payload, reference_date=ref_date)
    except Exception as exc:
        print("FATAL MOVE fetch failed with no fallback: %s" % exc,
              file=sys.stderr)
        sys.exit(1)

    pairs["move"] = move_pairs
    sources_map["move"] = move_src
    if move_stale and move_err:
        group_errors["move"] = move_err[:300]
    else:
        group_providers["move"] = move_src

    # 3. HY OAS (FRED)
    fred_rows, hy_src, hy_stale, hy_err = resolve_hy_oas(prev_payload)
    sources_map["hy_oas"] = hy_src
    if hy_stale and hy_err:
        group_errors["fred"] = hy_err[:300]
    else:
        group_providers["fred"] = hy_src

    hy_last = fred_rows[-1]
    hy_prev = fred_rows[-2]
    hy_change_bp = round(hy_last["value"] - hy_prev["value"], 2)

    now_utc = dt.datetime.now(dt.timezone.utc)
    beijing = now_utc.astimezone(dt.timezone(dt.timedelta(hours=8)))

    if futu_used:
        source_text = (
            "Futu CLI (futu kline, daily closes) for %s; "
            "Yahoo Finance (v8 chart API, ~15-min delayed) for "
            "MOVE/term structure/SPX%s; "
            "FRED BAMLH0A0HYM2 (ICE BofA US High Yield OAS, daily) "
            "for HY OAS" % (
                "/".join(k.upper() for k in futu_used),
                " and " + "/".join(
                    k.upper() for k in ("vix", "vvix", "skew")
                    if k not in futu_used) if len(futu_used) < 3 else ""))
    else:
        source_text = (
            "Yahoo Finance (v8 chart API, ~15-min delayed) for "
            "VIX/VVIX/SKEW/MOVE/term structure/SPX; "
            "FRED BAMLH0A0HYM2 (ICE BofA US High Yield OAS, daily) "
            "for HY OAS")

    payload = {
        "updated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at_beijing": beijing.strftime("%Y-%m-%d %H:%M Beijing"),
<<<<<<< Updated upstream
        "source": source_text,
        "stale_indicators": failed_keys,
=======
        "source": _describe_sources(sources_map),
        "sources_used": sources_map,
>>>>>>> Stashed changes
        "market_status": "computed in browser from US ET time",
        "indicators": {
            "vix": indicator(pairs["vix"], stale=("vix" in stale_eq),
                             source=sources_map.get("vix")),
            "vvix": indicator(pairs["vvix"], stale=("vvix" in stale_eq),
                              source=sources_map.get("vvix")),
            "skew": indicator(pairs["skew"], stale=("skew" in stale_eq),
                              source=sources_map.get("skew")),
            "move": indicator(pairs["move"], stale=move_stale,
                              source=move_src),
            "hy_oas": {
                "value_bp": hy_last["value"],
                "value_pct": round(hy_last["value"] / 100, 2),
                "date": hy_last["date"],
                "prev_value_bp": hy_prev["value"],
                "prev_date": hy_prev["date"],
                "change_bp": hy_change_bp,
                "history": fred_rows[-30:],
                "stale": hy_stale,
                "source": hy_src,
            },
        },
        "spx": indicator(pairs["spx"], stale=("spx" in stale_eq),
                         source=sources_map.get("spx")),
        "term_structure": {
            "vix1d": pairs["vix1d"][-1]["value"],
            "vix9d": pairs["vix9d"][-1]["value"],
            "vix": pairs["vix"][-1]["value"],
            "vix3m": pairs["vix3m"][-1]["value"],
            "vix6m": pairs["vix6m"][-1]["value"],
            "date": pairs["vix"][-1]["date"],
        },
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print("Wrote %s" % (out_path,))
    for k in ("vix", "vvix", "skew", "move"):
        ind = payload["indicators"][k]
        print("  %s: %s on %s (prev %s, %s%%) [%s%s]"
              % (k, ind["value"], ind["date"], ind["prev_close"],
                 ind["change_pct"], sources_map.get(k),
                 ", STALE" if ind.get("stale") else ""))
    hy = payload["indicators"]["hy_oas"]
    print("  hy_oas: %sbp on %s (prev %sbp %s) [%s%s]"
          % (hy["value_bp"], hy["date"], hy["prev_value_bp"], hy["prev_date"],
             hy_src, ", STALE" if hy_stale else ""))
    print("  spx: %s on %s [%s]"
          % (payload["spx"]["value"], payload["spx"]["date"], sources_map.get("spx")))
    print("  term: %s [%s]" % (payload["term_structure"], sources_map.get("vix1d")))

<<<<<<< Updated upstream
    new_state, sent = process_alerts(payload, state_path, now_utc,
                                     hy_stale=hy_stale)
    if failed_keys:
        # process_alerts marks the Yahoo source healthy (it ran to
        # completion); re-record the per-symbol failure afterwards so
        # the dashboard keeps showing it until the source recovers,
        # and the user gets the fetch-failure alert (24h dedup).
        record_fetch_failure(
            "yahoo", "failed symbols (kept previous values): %s"
            % ", ".join(failed_keys), state_path,
            dt.datetime.now(dt.timezone.utc))
        print("  stale indicators this run: %s" % (failed_keys,))
=======
    new_state, sent = process_alerts(
        payload,
        state_path,
        now_utc,
        group_errors=group_errors,
        group_providers=group_providers,
    )
>>>>>>> Stashed changes
    print("Wrote %s" % (state_path,))
    print("  channels configured: feishu=%s bark=%s"
          % (new_state["channels_configured"]["feishu"],
             new_state["channels_configured"]["bark"]))
    for key, entry in new_state["alerts"].items():
        print("  alert %s: %s (value %s, last sent %s)"
              % (key, entry["level"], entry["value"], entry["last_sent_at_utc"]))
    if sent:
        print("  alerts sent this run: %s" % (sent,))
    else:
        print("  alerts sent this run: none")
    if mode == "status":
<<<<<<< Updated upstream
        send_manual_push("status", payload, stale=hy_stale,
                         channels=channels)
=======
        any_stale = bool(stale_eq or move_stale or hy_stale)
        send_manual_push("status", payload, stale=any_stale)
>>>>>>> Stashed changes


if __name__ == "__main__":
    main()
