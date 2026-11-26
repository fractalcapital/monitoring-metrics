#!/usr/bin/env python3
"""Fetch live closes for the monitoring dashboard and write dashboard/data.json.

Multi-source architecture (server-side only -- browsers are blocked by CORS):
  - VIX / VVIX / SKEW:
      1. Futu CLI (`futu kline --ktype 2 --num 35 --format json`, verified on
         proud-pipes-7: returns {"kline_list": [{"date": 20261124, "close_price": ...}]};
         index codes are US..VIX / US..VVIX / US..SKEW, NOT US.VIX).
         Enabled automatically when `futu` is available or FUTU_SOURCE=1,
         disable with FUTU_SOURCE=0 / USE_FUTU=0.
      2. CBOE official CDN daily CSV (`cdn.cboe.com/.../*_History.csv`)
      3. Yahoo Finance (`curl_cffi` Chrome TLS impersonation + Cookie/Crumb)
  - VIX term structure (VIX1D / VIX9D / VIX3M / VIX6M):
      1. CBOE official CDN daily CSV
      2. Yahoo Finance (`curl_cffi` + Cookie/Crumb)
  - S&P 500 (SPX):
      1. Futu CLI (`US..SPX`) when Futu is enabled
      2. Yahoo Finance (`^GSPC`, via `curl_cffi` + Cookie/Crumb; same-day close + H/L)
      3. Stooq daily CSV (`^spx`; same-day close + H/L)
      4. FRED (`SP500`, official JSON API / CSV; 1-business-day lag fallback)
  - MOVE Index (ICE BofA MOVE):
      Configurable via MOVE_SOURCE_ORDER (default: "yahoo,tradingview,thetrading_tools")
      1. Yahoo Finance (`^MOVE`, single symbol via `curl_cffi` + Cookie/Crumb)
      2. TradingView public scanner (`TVC:MOVE`) + holiday-safe history upsert
      3. TheTrading.Tools daily CSV (`move-index.csv`)
  - HY OAS (ICE BofA US High Yield OAS, daily):
      1. FRED official JSON API (`BAMLH0A0HYM2`, requires FRED_API_KEY env var)
      2. FRED public CSV (`fredgraph.csv?id=BAMLH0A0HYM2`)

Fault isolation:
  - Every indicator resolves independently through its fallback chain.
  - If all providers fail for one indicator and a previous dashboard/data.json
    exists, that indicator keeps its last known value (listed in
    payload["stale_indicators"] and marked stale=True) and triggers a
    fetch-failure alert (24h dedup), while all other indicators continue
    updating normally.

Data quality & freshness:
  - Intraday high/low are sanity-checked (_sanitize_hl): flat H=L feeds
    (Futu SKEW) and bad ticks outside plausible bounds are dropped.
  - Every node carries lag_days / lagging vs the latest NYSE session
    (holiday + early-close aware). A lagging core indicator raises a
    "数据未更新" alert (fetch_freshness, 24h dedup); runs between the
    close and 18:00 ET get one session of grace (sources settle late).
  - ~1 trading year is fetched to compute stats (1y percentile, 5d / 20d
    change) and a composite stress score; data.json keeps 30 points.

Alerts (thresholds in ALERT_THRESHOLDS, shipped to the page via
payload["config"] so the page never hard-codes them):
  - level alerts with hysteresis (HYSTERESIS_PCT), plus VIX term-structure
    inversion, VIX single-day spike and HY OAS fast-widening alerts;
  - a "解除" notice is sent when a level alert that was pushed clears.

Manual push (MANUAL_PUSH / MANUAL_CHANNELS env vars):
  - "test": sends one test message on requested channels and exits.
  - "status": fetches normally, then sends the status summary.
  - "status-cached": builds and sends the summary from existing data.json
    without fetching (used by the VPS API buttons for instant replies).
  - MANUAL_CHANNELS: comma-separated subset of feishu,bark (default both).

Secrets (env vars only -- NEVER commit to this public repo, NEVER log):
  - FRED_API_KEY       (optional, enables FRED official JSON API)
  - FEISHU_WEBHOOK_URL (optional, Feishu robot webhook)
  - BARK_URL           (optional, Bark push base URL)
  - YAHOO_PROXY        (optional, proxy URL for Yahoo requests only)

Other optional env vars:
  - MM_OUT_DIR             write data.json / alert_state.json here instead of
                           dashboard/ (used by tests and local dry runs)
  - EXTRA_MARKET_CLOSURES  comma-separated YYYY-MM-DD ad-hoc NYSE closures
  - FUTU_KLINE_NUM         daily bars requested from Futu (default 260)
  - CREDIT_TIERS=0         skip the IG / CCC OAS context fetch
"""
import csv
import datetime as dt
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

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
    # VIX / VIX3M ratio: > 1 = backwardation (near-term fear above 3M).
    "term": {"label": "VIX 期限结构倒挂", "unit": "", "warning": 1.0,
             "critical": 1.1, "hyst_abs": 0.02},
    # Velocity ("event") alerts: no hysteresis and no "解除" notice.
    "vix_jump": {"label": "VIX 单日急升", "unit": "%", "warning": 25,
                 "critical": 50, "event": True},
    "hy_widen": {"label": "HY OAS 急速走阔", "unit": "bp", "warning": 25,
                 "warning_5d": 50, "event": True},
}
# A triggered level holds until the value is this fraction (or the spec's
# hyst_abs) below its threshold, so readings hovering around a line don't
# flap warning -> normal -> warning and re-alert every run.
HYSTERESIS_PCT = 0.05

LEVEL_SEVERITY = {"normal": 0, "warning": 1, "critical": 2}
LEVEL_LABEL = {"normal": "正常", "warning": "预警", "critical": "严重"}
RESEND_AFTER = dt.timedelta(hours=24)

FETCH_SOURCES = {
    "equities": "波动率与股指（Futu CLI / CBOE / Yahoo：VIX / VVIX / SKEW / 期限结构 / 标普 500）",
    "move": "MOVE 债市波动（Yahoo / TradingView / CSV）",
    "fred": "FRED（HY OAS）",
    "freshness": "数据新鲜度（收盘日期 vs 最近交易日）",
}
FETCH_GROUPS = ("equities", "move", "fred", "freshness")

# Always fetch direct, ignoring any OS proxy (e.g. a Mac routing other traffic
# through a VPS proxy). On the VPS and Actions runners there is no proxy.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_YAHOO_SESSION_CACHE = {"session": None, "crumb": None, "initialized": False}


HISTORY_KEEP = 260   # ~1 trading year kept in memory for percentile / 5d / 20d stats
DISPLAY_KEEP = 30    # points written to data.json history (sparklines + cache)
STATS_WINDOW = 252   # trading days used for the 1y percentile rank
STATS_MIN_WINDOW = 120
# Intraday high/low sanity bounds, relative to min / max(close, prev close).
# Generous on the upside (VIX can triple intraday, e.g. 2024-08-05), strict
# on the downside (a VIX low 40% under both closes is a bad tick).
HL_LOW_FLOOR = 0.70
HL_HIGH_CAP = 3.00

# ----------------------------------------------------------- HTTP helpers ---

def fetch_url(url, timeout=15, headers=None, retries=2, data=None,
              method=None, max_429_wait=15):
    """Fetch a URL via direct urllib with bounded retry on 429/transient errors.

    Honours Retry-After when present (capped by max_429_wait so fallback
    providers are tried promptly instead of hanging for minutes).
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, data=data, headers=headers or UA, method=method)
            with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
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
    """Parse common date formats (YYYYMMDD int, YYYY-MM-DD, MM/DD/YYYY, epoch) -> YYYY-MM-DD."""
    if raw is None:
        return None
    if isinstance(raw, int):
        s_int = str(raw)
        if len(s_int) == 8 and s_int.startswith(("19", "20")):
            return "%s-%s-%s" % (s_int[:4], s_int[4:6], s_int[6:])
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
    # "20261124" (Futu kline int/str YYYYMMDD)
    if len(s) == 8 and s.isdigit() and s.startswith(("19", "20")):
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:])
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


def _round_num(v, round_digits=2):
    return round(float(v)) if round_digits == 0 else round(float(v), round_digits)


def _valid_hl(hi, lo):
    if hi is None or lo is None:
        return None
    try:
        hi_f, lo_f = float(hi), float(lo)
    except (ValueError, TypeError):
        return None
    if math.isfinite(hi_f) and math.isfinite(lo_f) and hi_f > 0 and lo_f > 0 and hi_f >= lo_f:
        return hi_f, lo_f
    return None


def _finalize_pairs(pairs, round_digits=2, keep=HISTORY_KEEP):
    """Deduplicate by date (latest wins), sort ascending, round values."""
    by_date = {}
    for item in pairs:
        d = item.get("date")
        v = item.get("value")
        if not d or v is None:
            continue
        entry = {"date": d, "value": _round_num(v, round_digits)}
        hl = _valid_hl(item.get("high"), item.get("low"))
        if hl is not None:
            entry["high"] = _round_num(hl[0], round_digits)
            entry["low"] = _round_num(hl[1], round_digits)
        by_date[d] = entry
    out = _sanitize_hl([by_date[d] for d in sorted(by_date)])
    if keep and len(out) > keep:
        out = out[-keep:]
    return out


def _sanitize_hl(pairs):
    """Drop implausible intraday high/low in place (pairs sorted by date).

    Removed when: high == low (feeds without real intraday data, e.g. Futu
    SKEW returns H = L = close), the close sits outside [low, high], or the
    range breaks HL_LOW_FLOOR / HL_HIGH_CAP relative to the day's close and
    the previous close (bad ticks such as a VIX low of 15.39 between closes
    of 35.89 and 26.62). The close itself is always kept.
    """
    prev_close = None
    for e in pairs:
        if e.get("high") is not None and e.get("low") is not None:
            hi, lo, c = float(e["high"]), float(e["low"]), float(e["value"])
            ref_lo = min(c, prev_close) if prev_close else c
            ref_hi = max(c, prev_close) if prev_close else c
            if (abs(hi - lo) < 1e-9
                    or lo > c * 1.005 or hi < c * 0.995
                    or lo < ref_lo * HL_LOW_FLOOR
                    or hi > ref_hi * HL_HIGH_CAP):
                e.pop("high", None)
                e.pop("low", None)
        else:
            e.pop("high", None)
            e.pop("low", None)
        prev_close = float(e["value"])
    return pairs


def _easter_sunday(year):
    """Gregorian Easter Sunday (Anonymous Gregorian / Meeus-Jones-Butcher algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _nth_weekday(year, month, weekday, n):
    """Return the n-th (1-based) occurrence of weekday (Mon=0..Sun=6) in year/month."""
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=offset + (n - 1) * 7)


def _last_weekday(year, month, weekday):
    """Return the last occurrence of weekday (Mon=0..Sun=6) in year/month."""
    if month == 12:
        last = dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
    else:
        last = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - dt.timedelta(days=offset)


def _observed_nyse(d, allow_friday=True):
    """Apply NYSE weekend observation rule (Sat -> Fri if allowed, Sun -> Mon)."""
    if d.weekday() == 5:
        return d - dt.timedelta(days=1) if allow_friday else None
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


# Ad-hoc full-day closures not covered by the standard rules.
KNOWN_SPECIAL_CLOSURES = {
    "2025-01-09",  # National Day of Mourning (President Carter)
}


def _extra_closures(year):
    """Known special closures + EXTRA_MARKET_CLOSURES env (YYYY-MM-DD,...)."""
    out = set()
    raw_all = list(KNOWN_SPECIAL_CLOSURES) + os.environ.get(
        "EXTRA_MARKET_CLOSURES", "").split(",")
    for raw in raw_all:
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = dt.date.fromisoformat(raw)
        except ValueError:
            continue
        if d.year == year:
            out.add(d)
    return out


def _nyse_holidays(year):
    """Return set of standard NYSE full-day market closure dates for `year`."""
    holidays = set()
    # New Year's Day: if Saturday, NYSE does NOT observe on prior Dec 31.
    nyd = _observed_nyse(dt.date(year, 1, 1), allow_friday=False)
    if nyd:
        holidays.add(nyd)
    # Martin Luther King Jr. Day: 3rd Monday in January
    holidays.add(_nth_weekday(year, 1, 0, 3))
    # Washington's Birthday / Presidents' Day: 3rd Monday in February
    holidays.add(_nth_weekday(year, 2, 0, 3))
    # Good Friday: Friday before Easter Sunday
    holidays.add(_easter_sunday(year) - dt.timedelta(days=2))
    # Memorial Day: last Monday in May
    holidays.add(_last_weekday(year, 5, 0))
    # Juneteenth National Independence Day (observed by NYSE since 2022)
    if year >= 2022:
        holidays.add(_observed_nyse(dt.date(year, 6, 19)))
    # Independence Day
    holidays.add(_observed_nyse(dt.date(year, 7, 4)))
    # Labor Day: 1st Monday in September
    holidays.add(_nth_weekday(year, 9, 0, 1))
    # Thanksgiving Day: 4th Thursday in November
    holidays.add(_nth_weekday(year, 11, 3, 4))
    # Christmas Day
    holidays.add(_observed_nyse(dt.date(year, 12, 25)))
    holidays |= _extra_closures(year)
    return {d for d in holidays if d is not None}


def _is_nyse_holiday(d):
    return d in _nyse_holidays(d.year)


def _us_eastern_now(now_utc=None):
    """Convert UTC datetime to US Eastern (DST-aware via America/New_York)."""
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    if ZoneInfo is not None:
        try:
            return now_utc.astimezone(ZoneInfo("America/New_York"))
        except Exception:  # noqa: BLE001
            pass
    return now_utc - dt.timedelta(hours=5)


def _latest_us_weekday_iso(now_utc=None):
    """Best-effort latest NYSE trading day in US Eastern time (YYYY-MM-DD)."""
    et = _us_eastern_now(now_utc)
    d = et.date()
    while d.weekday() >= 5 or _is_nyse_holiday(d):
        d -= dt.timedelta(days=1)
    return d.isoformat()


def _nyse_early_closes(year):
    """Standard NYSE 13:00 ET early-close sessions for `year`."""
    hol = _nyse_holidays(year)
    days = {
        _nth_weekday(year, 11, 3, 4) + dt.timedelta(days=1),  # day after Thanksgiving
        dt.date(year, 12, 24),                                # Christmas Eve
        dt.date(year, 7, 3),                                  # Independence Day eve
    }
    return {d for d in days if d.weekday() < 5 and d not in hol}


def _is_trading_day(d):
    return d.weekday() < 5 and not _is_nyse_holiday(d)


def _close_minutes(d):
    """Session close in ET minutes after midnight (13:00 on early closes)."""
    return 13 * 60 if d in _nyse_early_closes(d.year) else 16 * 60


def _prev_trading_day(d):
    d -= dt.timedelta(days=1)
    while not _is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def _expected_close_date(now_utc=None, settle_minutes=15):
    """Latest NYSE session whose close (+settle_minutes) has passed (ET date)."""
    et = _us_eastern_now(now_utc)
    d = et.date()
    mins = et.hour * 60 + et.minute
    if _is_trading_day(d) and mins >= _close_minutes(d) + settle_minutes:
        return d.isoformat()
    return _prev_trading_day(d).isoformat()


def _trading_lag(data_date, expected_date):
    """NYSE sessions after data_date up to expected_date (0 = up to date)."""
    try:
        a = dt.date.fromisoformat(str(data_date))
        b = dt.date.fromisoformat(str(expected_date))
    except (TypeError, ValueError):
        return None
    n = 0
    while a < b and n <= 60:
        a += dt.timedelta(days=1)
        if _is_trading_day(a):
            n += 1
    return n


def _market_session_now(now_utc=None):
    """(is_regular_session_open, et_date_iso)."""
    et = _us_eastern_now(now_utc)
    d = et.date()
    mins = et.hour * 60 + et.minute
    is_open = _is_trading_day(d) and 9 * 60 + 30 <= mins < _close_minutes(d)
    return is_open, d.isoformat()


def _upsert_history(prev_history, date, close_val, prev_close_val=None,
                    keep=HISTORY_KEEP, round_digits=2, high_val=None, low_val=None):
    """Holiday-safe single-quote upsert (see _upsert_history_raw) + H/L sanity."""
    return _sanitize_hl(_upsert_history_raw(
        prev_history, date, close_val, prev_close_val, keep=keep,
        round_digits=round_digits, high_val=high_val, low_val=low_val))


def _upsert_history_raw(prev_history, date, close_val, prev_close_val=None,
                        keep=HISTORY_KEEP, round_digits=2, high_val=None,
                        low_val=None):
    """Merge a single quote (date, close, optional prev_close/high/low) into prior history.

    Guards against US market holidays: if close_val (and prev_close_val when
    given) already match the last point in prev_history, keep the existing
    date instead of stamping a holiday date onto unchanged closes.
    """
    hist = _finalize_pairs(prev_history or [], round_digits=round_digits, keep=keep)
    close_r = _round_num(close_val, round_digits)
    prev_r = _round_num(prev_close_val, round_digits) if prev_close_val is not None else None
    hl = _valid_hl(high_val, low_val)
    latest_item = {"date": date, "value": close_r}
    if hl is not None:
        latest_item["high"] = _round_num(hl[0], round_digits)
        latest_item["low"] = _round_num(hl[1], round_digits)

    if hist:
        last = hist[-1]
        second_last = hist[-2] if len(hist) >= 2 else None
        same_last = abs(last["value"] - close_r) < 1e-6
        same_prev = (
            prev_r is None
            or (second_last is not None and abs(second_last["value"] - prev_r) < 1e-6)
        )
        if same_last and same_prev and date >= last["date"]:
            if ("high" in latest_item and "low" in latest_item
                    and (last.get("high") is None or last.get("low") is None)):
                hist[-1] = dict(last, high=latest_item["high"], low=latest_item["low"])
            return hist
        if date <= last["date"]:
            updated = {"date": last["date"], "value": close_r}
            if "high" in latest_item and "low" in latest_item:
                updated["high"] = latest_item["high"]
                updated["low"] = latest_item["low"]
            elif (date == last["date"]
                  and last.get("high") is not None and last.get("low") is not None):
                updated["high"] = last["high"]
                updated["low"] = last["low"]
            hist[-1] = updated
            return hist[-keep:]

    if not hist and prev_r is not None:
        prev_day = dt.date.fromisoformat(date) - dt.timedelta(days=1)
        while prev_day.weekday() >= 5 or _is_nyse_holiday(prev_day):
            prev_day -= dt.timedelta(days=1)
        hist.append({"date": prev_day.isoformat(), "value": prev_r})
    hist.append(latest_item)
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
    """Return path to `futu` CLI when enabled and present, else None.

    Supports both upstream FUTU_SOURCE=1 / FUTU_BIN and FUTU_CLI / USE_FUTU.
    """
    for flag in ("FUTU_SOURCE", "USE_FUTU"):
        val = os.environ.get(flag, "").strip().lower()
        if val in ("0", "false", "no", "off"):
            return None
    cli = (
        os.environ.get("FUTU_CLI", "").strip()
        or os.environ.get("FUTU_BIN", "").strip()
        or "futu"
    )
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
        for key in ("kline_list", "data", "list", "kl_list", "kline",
                    "rows", "items", "quotes", "result"):
            val = obj.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                nested = _extract_futu_rows(val)
                if nested:
                    return nested
        if any(k in obj for k in ("close_price", "close", "last_price")):
            return [obj]
    return []


def _first_pos_float(row, keys):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            try:
                val = float(row[key])
                if math.isfinite(val) and val > 0:
                    return val
            except (ValueError, TypeError):
                pass
    return None


def parse_futu_kline(obj_or_text):
    """Parse `futu kline --format json` output into [{date, value, high?, low?}].

    Handles both verified proud-pipes-7 shape
    {"kline_list": [{"date": 20261124, "close_price": 26.36, "high_price": ..., "low_price": ...}]}
    and alternative list / time_key / close shapes.
    """
    obj = (_extract_json_from_stdout(obj_or_text)
           if isinstance(obj_or_text, str) else obj_or_text)
    rows = _extract_futu_rows(obj)
    pairs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = None
        for dk in ("date", "time_key", "time", "data_date", "trade_date", "timestamp"):
            if dk in row and row[dk] not in (None, ""):
                date = _normalize_date(row[dk])
                if date:
                    break
        val = _first_pos_float(row, ("close_price", "closePrice", "close", "last_price", "lastPrice"))
        if date and val is not None:
            item = {"date": date, "value": val}
            hi = _first_pos_float(row, ("high_price", "highPrice", "high", "highest_price", "max_price"))
            lo = _first_pos_float(row, ("low_price", "lowPrice", "low", "lowest_price", "min_price"))
            if hi is not None and lo is not None and hi >= lo:
                item["high"] = hi
                item["low"] = lo
            pairs.append(item)
    out = _finalize_pairs(pairs, round_digits=2, keep=HISTORY_KEEP)
    if len(out) < 2:
        raise RuntimeError("too few Futu closes: %s" % (out,))
    return out


def _parse_futu_quote_json(text):
    obj = _extract_json_from_stdout(text)
    rows = _extract_futu_rows(obj)
    for row in rows:
        if not isinstance(row, dict):
            continue
        last_price = _first_pos_float(
            row, ("last_price", "lastPrice", "close_price", "closePrice", "close", "price"))
        if last_price is None:
            continue
        prev_close = _first_pos_float(
            row, ("prev_close_price", "prevClosePrice", "prev_close", "last_close"))
        high_price = _first_pos_float(
            row, ("high_price", "highPrice", "high", "highest_price", "max_price"))
        low_price = _first_pos_float(
            row, ("low_price", "lowPrice", "low", "lowest_price", "min_price"))
        date = None
        for dk in ("data_date", "date", "update_time", "time", "timestamp"):
            if dk in row and row[dk] not in (None, ""):
                date = _normalize_date(row[dk])
                if date:
                    break
        return last_price, prev_close, date, high_price, low_price
    raise RuntimeError("no valid quote in futu output")


def _run_futu_cmd(cmd, timeout=45):
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[-200:]
        raise RuntimeError("futu exit %d: %s" % (proc.returncode, err))
    return proc.stdout


def fetch_futu(symbol, prev_history=None):
    """Fetch daily closes (and intraday high/low) via Futu CLI (`kline` -> `quote` fallback)."""
    cli = _futu_bin()
    if not cli:
        raise RuntimeError("futu CLI not available")

    errors = []
    # 1. Verified proud-pipes-7 command: `futu kline <symbol> --ktype 2 --num N --format json`
    #    (N = ~1y for stats; retried with 30 if the long request fails).
    nums = []
    for n in ((os.environ.get("FUTU_KLINE_NUM", "").strip() or str(HISTORY_KEEP)), "30"):
        if n not in nums:
            nums.append(n)
    for num in nums:
        try:
            out = _run_futu_cmd([
                cli, "kline", symbol, "--ktype", "2", "--num", num, "--format", "json",
            ])
            pairs = parse_futu_kline(out)
            # If kline omitted high/low on the latest bar, enrich best-effort from `futu quote`
            if pairs and (pairs[-1].get("high") is None or pairs[-1].get("low") is None):
                try:
                    q_out = _run_futu_cmd([cli, "quote", symbol, "--format", "json"])
                    _, _, q_date, q_hi, q_lo = _parse_futu_quote_json(q_out)
                    hl = _valid_hl(q_hi, q_lo)
                    if hl is not None and (not q_date or q_date == pairs[-1]["date"]):
                        pairs[-1]["high"] = _round_num(hl[0], 2)
                        pairs[-1]["low"] = _round_num(hl[1], 2)
                except Exception:  # noqa: BLE001
                    pass
            return _sanitize_hl(pairs)
        except Exception as exc:  # noqa: BLE001
            errors.append("kline(num=%s): %s" % (num, exc))

    # 2. `futu quote <symbol> --format json` + local history upsert
    try:
        out = _run_futu_cmd([cli, "quote", symbol, "--format", "json"])
        last_price, prev_close, date, high_price, low_price = _parse_futu_quote_json(out)
        use_date = date or _latest_us_weekday_iso()
        pairs = _upsert_history(
            prev_history, use_date, last_price, prev_close,
            high_val=high_price, low_val=low_price)
        if len(pairs) >= 2:
            return pairs
        errors.append("quote produced <2 points")
    except Exception as exc:  # noqa: BLE001
        errors.append("quote: %s" % exc)

    raise RuntimeError("futu %s failed (%s)" % (symbol, "; ".join(errors)))


# ------------------------------------------------------- Provider: CBOE ---

def _parse_csv_series(text, preferred_cols=("CLOSE",), scale=1.0,
                      round_digits=2, keep=HISTORY_KEEP):
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
        val_idx = len(norm_header) - 1
        if val_idx == date_idx:
            val_idx = 1

    high_idx = norm_header.index("HIGH") if "HIGH" in norm_header else None
    low_idx = norm_header.index("LOW") if "LOW" in norm_header else None

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
        item = {"date": date, "value": val}
        if (high_idx is not None and low_idx is not None
                and len(row) > max(high_idx, low_idx)):
            try:
                hi = float(row[high_idx].strip()) * scale
                lo = float(row[low_idx].strip()) * scale
                if _valid_hl(hi, lo) is not None:
                    item["high"] = hi
                    item["low"] = lo
            except ValueError:
                pass
        pairs.append(item)
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
        keep=HISTORY_KEEP,
    )


# ------------------------------------------------------- Provider: FRED ---

def _fetch_fred_api(series_id, api_key, scale=1.0, round_digits=2):
    params = urllib.parse.urlencode({
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "300",
    })
    url = "%s?%s" % (FRED_API_BASE, params)
    # Never leak api_key in exception messages or logs.
    try:
        req = urllib.request.Request(url, headers=PLAIN_UA)
        with _NO_PROXY_OPENER.open(req, timeout=20) as resp:
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
    out = _finalize_pairs(pairs, round_digits=round_digits, keep=HISTORY_KEEP)
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
        keep=HISTORY_KEEP,
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
    return _parse_csv_series(text, preferred_cols=("CLOSE",), round_digits=2, keep=HISTORY_KEEP)


# ------------------------------------------------------ Provider: Yahoo ---

def _get_yahoo_cffi_session():
    """Return a cached (curl_cffi Session, crumb) tuple with Chrome TLS fingerprint."""
    if cffi_requests is None:
        return None, None
    if _YAHOO_SESSION_CACHE["initialized"]:
        return _YAHOO_SESSION_CACHE["session"], _YAHOO_SESSION_CACHE["crumb"]

    sess = cffi_requests.Session(impersonate="chrome")
    proxy_url = os.environ.get("YAHOO_PROXY", "").strip()
    if proxy_url:
        sess.proxies = {"http": proxy_url, "https": proxy_url}
    else:
        # Match _NO_PROXY_OPENER: do not inherit OS proxy unless YAHOO_PROXY is explicit.
        sess.trust_env = False

    crumb = None
    try:
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
    quote = result["indicators"]["quote"][0]
    closes = quote["close"]
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    pairs = []
    for i, (ts, close) in enumerate(zip(stamps, closes)):
        if close is None:
            continue
        date = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date().isoformat()
        item = {"date": date, "value": round(float(close), 2)}
        if i < len(highs) and i < len(lows):
            hl = _valid_hl(highs[i], lows[i])
            if hl is not None:
                item["high"] = round(hl[0], 2)
                item["low"] = round(hl[1], 2)
        pairs.append(item)
    out = _finalize_pairs(pairs, round_digits=2, keep=HISTORY_KEEP)
    if len(out) < 2:
        raise RuntimeError("too few closes for %s: %s" % (symbol, out))
    return out


def fetch_yahoo(symbol, fast_fail=True):
    """Fetch ~1y daily chart from Yahoo Finance (stats need a year of closes).

    Uses `curl_cffi` (Chrome TLS impersonation + Cookie/Crumb) when installed,
    falling back to stdlib `urllib`. When fast_fail=True (fallback providers
    exist), avoids long sleeps on 429 so failover happens within seconds.
    """
    sess, crumb = _get_yahoo_cffi_session()
    params = {"range": "1y", "interval": "1d"}
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
    query = "?range=1y&interval=1d"
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
                            keep=HISTORY_KEEP, round_digits=2)
    if len(pairs) < 2:
        raise RuntimeError("TradingView TVC:MOVE produced <2 points")
    return pairs


def fetch_trading_tools_move():
    """Fetch ICE BofA MOVE Index daily CSV from thetrading.tools."""
    raw = fetch_url(TRADING_TOOLS_MOVE_URL, timeout=20, headers=UA, retries=1)
    text = raw.decode("utf-8-sig", errors="replace")
    return _parse_csv_series(
        text,
        preferred_cols=("CLOSE", "MOVE", "VALUE"),
        round_digits=2,
        keep=HISTORY_KEEP,
    )


# -------------------------------------------- Indicator resolution chains ---

def _attach_node_hl(pairs, node, round_digits=2):
    """Preserve top-level day_high/day_low on the matching latest pair when cached."""
    if not pairs or not isinstance(node, dict):
        return pairs
    hl = _valid_hl(node.get("day_high"), node.get("day_low"))
    if hl is not None and pairs[-1].get("date") == node.get("date"):
        if pairs[-1].get("high") is None or pairs[-1].get("low") is None:
            pairs[-1] = dict(
                pairs[-1],
                high=_round_num(hl[0], round_digits),
                low=_round_num(hl[1], round_digits),
            )
    return pairs


def _prev_pairs_for(prev_payload, key):
    """Extract cached history pairs for an indicator from previous data.json."""
    if not isinstance(prev_payload, dict):
        return None
    try:
        if key == "spx":
            node = prev_payload["spx"]
            hist = node.get("history") or []
            if len(hist) >= 2:
                return _attach_node_hl(_finalize_pairs(hist, round_digits=2, keep=HISTORY_KEEP), node)
            return _attach_node_hl(_finalize_pairs([
                {"date": node["prev_date"], "value": node["prev_close"]},
                {"date": node["date"], "value": node["value"]},
            ], round_digits=2, keep=HISTORY_KEEP), node)
        if key in ("vix1d", "vix9d", "vix3m", "vix6m"):
            ts = prev_payload.get("term_structure") or {}
            ts_dates = ts.get("dates") or {}
            d = ts_dates.get(key) or ts.get("date")
            if ts.get(key) is not None and d:
                v = float(ts[key])
                return [{"date": d, "value": v}, {"date": d, "value": v}]
            return None
        if key == "hy_oas":
            hy = prev_payload["indicators"]["hy_oas"]
            hist = hy.get("history") or []
            if len(hist) >= 2:
                return _finalize_pairs(hist, round_digits=0, keep=HISTORY_KEEP)
            return _finalize_pairs([
                {"date": hy["prev_date"], "value": hy["prev_value_bp"]},
                {"date": hy["date"], "value": hy["value_bp"]},
            ], round_digits=0, keep=HISTORY_KEEP)
        node = prev_payload["indicators"][key]
        hist = node.get("history") or []
        if len(hist) >= 2:
            return _attach_node_hl(_finalize_pairs(hist, round_digits=2, keep=HISTORY_KEEP), node)
        if node.get("prev_date") and node.get("prev_close") is not None:
            return _attach_node_hl(_finalize_pairs([
                {"date": node["prev_date"], "value": node["prev_close"]},
                {"date": node["date"], "value": node["value"]},
            ], round_digits=2, keep=HISTORY_KEEP), node)
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

    # 3. S&P 500: Futu CLI -> Yahoo (same-day + H/L) -> Stooq -> FRED SP500 (1d lag fallback)
    prev_spx = _prev_pairs_for(prev_payload, "spx")
    spx_chain = []
    if has_futu and "spx" in futu_map:
        spx_sym = futu_map["spx"]
        spx_chain.append(("futu", lambda s=spx_sym, h=prev_spx: fetch_futu(s, h)))
    spx_chain.append(("yahoo", lambda: fetch_yahoo(YAHOO_SYMBOLS["spx"], fast_fail=True)))
    spx_chain.append(("stooq", fetch_stooq_spx))
    spx_chain.append(("fred", lambda: fetch_fred_series("SP500", scale=1.0, round_digits=2)))
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
        fallback = [
            {"date": "2026-11-19", "value": 352},
            {"date": "2026-11-20", "value": 376},
        ]
        return fallback, "snapshot", True, err


def indicator(pairs, stale=False, source=None, stats=None):
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
        "history": pairs[-DISPLAY_KEEP:],
    }
    if last.get("high") is not None and last.get("low") is not None:
        out["day_high"] = last["high"]
        out["day_low"] = last["low"]
    if stale:
        out["stale"] = True
    if source:
        out["source"] = source
    if stats:
        out["stats"] = stats
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


# ------------------------------------------------- stats / context data ---

# Composite stress score weights (1y percentile ranks). SKEW is left out on
# purpose: it is often HIGH in calm markets (cheap tail hedges), so a high
# SKEW percentile is not "stress".
COMPOSITE_WEIGHTS = {"vix": 1.0, "move": 1.0, "hy_oas": 1.0, "vvix": 0.5}


def compute_stats(pairs, prev_stats=None, is_bp=False):
    """1y percentile rank + 5d / 20d change for one series.

    When fewer than STATS_MIN_WINDOW closes are available (cached / quote-only
    fallback paths only keep 30 points), the previous run's percentile is
    reused and flagged pct_rank_stale instead of computing a misleading rank.
    """
    vals = [float(p["value"]) for p in (pairs or []) if p.get("value") is not None]
    if len(vals) < 2:
        return prev_stats or None
    last = vals[-1]
    window = vals[-STATS_WINDOW:]
    out = {"window": len(window)}
    if len(window) >= STATS_MIN_WINDOW:
        below = sum(1 for v in window if v < last)
        equal = sum(1 for v in window if v == last)
        out["pct_rank"] = int(round(100.0 * (below + 0.5 * equal) / len(window)))
    elif isinstance(prev_stats, dict) and prev_stats.get("pct_rank") is not None:
        out["pct_rank"] = prev_stats["pct_rank"]
        out["window"] = prev_stats.get("window", len(window))
        out["pct_rank_stale"] = True
    for n in (5, 20):
        if len(vals) > n:
            base = vals[-1 - n]
            out["chg_%dd" % n] = round(last - base, 0 if is_bp else 2)
            if base:
                out["chg_%dd_pct" % n] = round((last - base) / base * 100, 2)
    return out


def composite_score(stats_map):
    """Weighted mean of 1y percentile ranks (0-100); None if <2 core inputs."""
    num = den = 0.0
    comps = {}
    for key, weight in COMPOSITE_WEIGHTS.items():
        pr = (stats_map.get(key) or {}).get("pct_rank")
        if pr is None:
            continue
        comps[key] = pr
        num += weight * pr
        den += weight
    if den < 2.0:
        return None
    return {"score": int(round(num / den)), "components": comps,
            "weights": COMPOSITE_WEIGHTS, "basis": "近一年分位加权"}


CREDIT_TIER_SERIES = (
    ("ig", "BAMLC0A0CM", "投资级 IG"),
    ("ccc", "BAMLH0A3HYC", "CCC 级"),
)


def resolve_credit_tiers(prev_payload):
    """IG / CCC OAS context (FRED). Optional: never fatal, never alerts."""
    prev = {}
    if isinstance(prev_payload, dict) and isinstance(prev_payload.get("credit_tiers"), dict):
        prev = prev_payload["credit_tiers"]
    out = {}
    for key, series_id, label in CREDIT_TIER_SERIES:
        try:
            rows, src = fetch_fred_series(series_id, scale=100.0, round_digits=0)
            last, before = rows[-1], rows[-2]
            item = {
                "label": label, "series": series_id,
                "value_bp": last["value"], "date": last["date"],
                "prev_value_bp": before["value"], "prev_date": before["date"],
                "change_bp": round(last["value"] - before["value"]),
                "source": src,
            }
            st = compute_stats(rows, (prev.get(key) or {}).get("stats"), is_bp=True)
            if st:
                item["stats"] = st
            out[key] = item
        except Exception as exc:  # noqa: BLE001
            print("  [credit_tiers] %s failed: %s" % (series_id, exc), file=sys.stderr)
            if isinstance(prev.get(key), dict):
                item = dict(prev[key])
                item["stale"] = True
                out[key] = item
    return out


# Freshness: trading-session lag allowed before a node is flagged lagging
# (display), and before a "数据未更新" alert fires. FRED HY OAS is
# normally one business day behind.
FRESH_ALLOWED_LAG = {"hy_oas": 2}
FRESH_ALERT_LAG = {"hy_oas": 3}
FRESHNESS_ALERT_AFTER_ET_MIN = 18 * 60
NODE_LABELS = {"vix": "VIX", "vvix": "VVIX", "skew": "SKEW", "move": "MOVE",
               "spx": "标普 500", "hy_oas": "HY OAS"}


def annotate_freshness(payload, now_utc, stale_keys=()):
    """Stamp lag_days / lagging / intraday on payload nodes.

    Returns (expected_close_date, issues) where issues lists the nodes whose
    lag is alert-worthy. Between the close and 18:00 ET one session of grace
    is given (CBOE / ICE closes post late); cached (stale) nodes are skipped
    because their fetch failure already alerts separately.
    """
    expected = _expected_close_date(now_utc)
    market_open, et_today = _market_session_now(now_utc)
    payload["expected_close_date"] = expected
    payload.pop("intraday", None)
    ind = payload["indicators"]
    nodes = {k: ind[k] for k in ("vix", "vvix", "skew", "move", "hy_oas")}
    nodes["spx"] = payload["spx"]
    for k, node in nodes.items():
        for f in ("lag_days", "lagging", "intraday"):
            node.pop(f, None)
        lag = _trading_lag(node.get("date"), expected)
        if lag:
            node["lag_days"] = lag
            if lag > FRESH_ALLOWED_LAG.get(k, 0):
                node["lagging"] = True
        if market_open and k != "hy_oas" and node.get("date") == et_today:
            node["intraday"] = True
            payload["intraday"] = True
    ts = payload.get("term_structure") or {}
    ts.pop("lag_days", None)
    tlags = {}
    for tenor, d in (ts.get("dates") or {}).items():
        lag = _trading_lag(d, expected)
        if lag:
            tlags[tenor] = lag
    if tlags:
        ts["lag_days"] = tlags
    et_now = _us_eastern_now(now_utc)
    grace = (expected == et_now.date().isoformat()
             and et_now.hour * 60 + et_now.minute < FRESHNESS_ALERT_AFTER_ET_MIN)
    issues = []
    for k in ("vix", "vvix", "skew", "move", "spx", "hy_oas"):
        if k in stale_keys:
            continue
        lag = nodes[k].get("lag_days", 0)
        if lag - (1 if grace else 0) >= FRESH_ALERT_LAG.get(k, 1):
            issues.append("%s %s（落后 %d 个交易日）"
                          % (NODE_LABELS[k], nodes[k].get("date"), lag))
    return expected, issues


# ---------------------------------------------------------------- alerts ---

def _level_for(value, spec):
    if "critical" in spec and value >= spec["critical"]:
        return "critical"
    if value >= spec["warning"]:
        return "warning"
    return "normal"


def _level_with_hysteresis(value, spec, old_level="normal"):
    """Level for value, holding a previously triggered level inside its band."""
    raw = _level_for(value, spec)
    old_level = old_level if old_level in LEVEL_SEVERITY else "normal"
    if LEVEL_SEVERITY[raw] >= LEVEL_SEVERITY[old_level]:
        return raw
    lvl = old_level
    while LEVEL_SEVERITY[lvl] > LEVEL_SEVERITY[raw]:
        thr = spec.get(lvl)
        if thr is None:
            break
        band = spec.get("hyst_abs", abs(thr) * HYSTERESIS_PCT)
        if value > thr - band:
            return lvl
        lvl = "warning" if lvl == "critical" else "normal"
    return raw


def _change_text(d):
    if d.get("change_pct") is not None:
        return "%+.2f%% vs %s" % (d["change_pct"], d.get("prev_date"))
    return "-"


def _fmt_bp(v):
    return "—" if v is None else "%+dbp" % round(v)


def evaluate_alerts(payload, prev_levels=None):
    """Return {key: {level, value, date, threshold, change}} for every alert key.

    prev_levels ({key: level} from alert_state.json) enables hysteresis.
    """
    prev_levels = prev_levels or {}
    ind = payload["indicators"]
    out = {}

    def _ev(level, value, date, threshold, change):
        return {"level": level, "value": value, "date": date,
                "threshold": threshold, "change": change}

    for key in ("vix", "vvix", "skew", "move"):
        spec = ALERT_THRESHOLDS[key]
        d = ind[key]
        level = _level_with_hysteresis(d["value"], spec, prev_levels.get(key, "normal"))
        threshold = spec["critical"] if level == "critical" else spec["warning"]
        out[key] = _ev(level, d["value"], d["date"], threshold, _change_text(d))

    hy = ind["hy_oas"]
    spec = ALERT_THRESHOLDS["hy_oas"]
    level = _level_with_hysteresis(hy["value_bp"], spec, prev_levels.get("hy_oas", "normal"))
    threshold = spec["critical"] if level == "critical" else spec["warning"]
    out["hy_oas"] = _ev(level, hy["value_bp"], hy["date"], threshold,
                        "%+.0fbp vs %s" % (hy["change_bp"], hy["prev_date"]))

    # Cross-check: VIX >= 30 AND (VVIX >= 140 OR MOVE >= 140). Once on, each
    # leg may sit HYSTERESIS_PCT lower before it clears.
    cross = ALERT_THRESHOLDS["cross"]
    f = (1 - HYSTERESIS_PCT) if prev_levels.get("cross", "normal") != "normal" else 1.0
    t_vix, t_vvix, t_move = (round(cross["vix"] * f, 1), round(cross["vvix"] * f, 1),
                             round(cross["move"] * f, 1))
    parts = []
    if ind["vvix"]["value"] >= t_vvix:
        parts.append("VVIX %s >= %g" % (ind["vvix"]["value"], t_vvix))
    if ind["move"]["value"] >= t_move:
        parts.append("MOVE %s >= %g" % (ind["move"]["value"], t_move))
    hit = ind["vix"]["value"] >= t_vix and bool(parts)
    change = ("VIX >= %g and %s" % (t_vix, ", ".join(parts))) if hit else "-"
    out["cross"] = _ev("warning" if hit else "normal", ind["vix"]["value"],
                       ind["vix"]["date"], cross["vix"], change)

    # VIX term structure: VIX / VIX3M > 1 = backwardation.
    ts = payload.get("term_structure") or {}
    spec = ALERT_THRESHOLDS["term"]
    if ts.get("vix") and ts.get("vix3m"):
        ratio = round(float(ts["vix"]) / float(ts["vix3m"]), 3)
        level = _level_with_hysteresis(ratio, spec, prev_levels.get("term", "normal"))
        threshold = spec["critical"] if level == "critical" else spec["warning"]
        change = "VIX %.2f / VIX3M %.2f" % (ts["vix"], ts["vix3m"])
        if ts.get("vix1d") is not None and ts.get("vix9d") is not None:
            change += "；VIX1D %.2f / VIX9D %.2f" % (ts["vix1d"], ts["vix9d"])
        if ts.get("date_mismatch"):
            change += "（各期限日期不一致）"
        out["term"] = _ev(level, ratio, ts.get("date") or ind["vix"]["date"],
                          threshold, change)

    # Velocity: single-day VIX spike.
    spec = ALERT_THRESHOLDS["vix_jump"]
    v = ind["vix"]
    pct = v.get("change_pct")
    level = _level_for(pct, spec) if pct is not None else "normal"
    threshold = spec["critical"] if level == "critical" else spec["warning"]
    out["vix_jump"] = _ev(level, pct if pct is not None else 0, v["date"], threshold,
                          "VIX %s → %s（vs %s）" % (v.get("prev_close"), v["value"],
                                                   v.get("prev_date")))

    # Velocity: HY OAS fast widening (1-day or 5-day, bp).
    spec = ALERT_THRESHOLDS["hy_widen"]
    d1 = hy.get("change_bp")
    hist = [h.get("value") for h in (hy.get("history") or []) if h.get("value") is not None]
    d5 = (hist[-1] - hist[-6]) if len(hist) >= 6 else None
    hit = ((d1 is not None and d1 >= spec["warning"])
           or (d5 is not None and d5 >= spec["warning_5d"]))
    out["hy_widen"] = _ev("warning" if hit else "normal", d1 if d1 is not None else 0,
                          hy["date"], spec["warning"],
                          "单日 %s，5 日 %s（阈值：单日 +%dbp / 5 日 +%dbp）"
                          % (_fmt_bp(d1), _fmt_bp(d5), spec["warning"], spec["warning_5d"]))

    if payload.get("intraday"):
        for key, ev in out.items():
            if key not in ("hy_oas", "hy_widen"):
                ev["intraday"] = True
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
    """Shared alert content for every channel: (title, body)."""
    spec = ALERT_THRESHOLDS[key]
    unit = spec.get("unit", "")
    level_cn = LEVEL_LABEL[ev["level"]]
    title = "【监控报警·%s】%s" % (level_cn, spec["label"])
    if key == "cross":
        title = "【监控报警·%s】综合预警" % level_cn
        body = ("触发条件：%s\n"
                "VIX 当前值：%s（日期 %s）" % (ev["change"], ev["value"], ev["date"]))
    elif key == "term":
        body = ("VIX / VIX3M：%.3f（阈值 %.2f，>1 即近端高于远端，期限结构倒挂）\n"
                "%s\n日期：%s" % (ev["value"], ev["threshold"], ev["change"], ev["date"]))
    elif key == "vix_jump":
        body = ("VIX 单日涨幅：%+.2f%%（阈值 +%s%%）\n%s\n日期：%s"
                % (ev["value"], ev["threshold"], ev["change"], ev["date"]))
    elif key == "hy_widen":
        body = "HY OAS 走阔：%s\n日期：%s" % (ev["change"], ev["date"])
    else:
        body = ("当前值：%s%s（阈值 %s%s）\n"
                "日期：%s\n"
                "变化：%s" % (ev["value"], unit, ev["threshold"], unit,
                                ev["date"], ev["change"]))
    if ev.get("intraday"):
        body += "\n（盘中读数，收盘前仍可能变化）"
    body = "%s\n查看仪表盘：%s" % (body, DASHBOARD_URL)
    return title, body


def format_recovery_parts(key, ev):
    """"解除" notice for a level alert that was pushed and has now cleared."""
    spec = ALERT_THRESHOLDS[key]
    unit = spec.get("unit", "")
    title = "【监控报警·解除】%s" % ("综合预警" if key == "cross" else spec["label"])
    if key == "cross":
        body = "综合预警条件已不再满足（VIX %s，日期 %s）" % (ev["value"], ev["date"])
    elif key == "term":
        body = ("VIX / VIX3M 回到 %.3f，期限结构恢复升水\n%s\n日期：%s"
                % (ev["value"], ev["change"], ev["date"]))
    else:
        body = ("当前值：%s%s，已回落到预警线 %s%s 下方\n日期：%s"
                % (ev["value"], unit, spec["warning"], unit, ev["date"]))
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

def _atomic_write_json(path, obj):
    """Write JSON atomically: temp file in the same dir + fsync + os.replace.

    A run killed mid-write (API timeout, OOM, reboot) must never leave a
    truncated data.json: the page would fall back to its snapshot and the
    next run would lose every cached value used for per-indicator fallback.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    data = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _write_state(state_path, state):
    _atomic_write_json(state_path, state)


def _channels_configured():
    return {
        "feishu": bool(os.environ.get("FEISHU_WEBHOOK_URL")),
        "bark": bool(os.environ.get("BARK_URL")),
    }


def _send_channel(ch, title, body, template, bark_level):
    if ch == "feishu":
        return send_feishu_card(title, body, template)
    return send_bark(title, body, bark_level)


FETCH_ALERT_TEXT = {
    "freshness": {
        "title": "【监控报警·预警】数据未更新",
        "detail_label": "详情",
        "note": ("以上指标的最新收盘日期落后于最近一个交易日，数据源可能尚未更新"
                 "或返回了旧数据；仪表盘已在对应卡片标注“日期落后”，请及时检查。"),
    },
}
DEFAULT_FETCH_NOTE = ("仪表盘该项暂时保留上一次成功拉取的数据（带日期），"
                      "其他正常数据源已继续更新，请及时检查。")


def _fetch_alert_send(key, label, detail, state, now_utc, source_key=None):
    """Send a fetch-failure / freshness alert on Feishu + Bark with 24h dedup."""
    now_s = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    alerts = state.setdefault("alerts", {})
    old = alerts.get(key, {}) or {}
    old_level = old.get("level", "normal")
    old_channels = old.get("channels", {}) or {}
    channels_configured = _channels_configured()
    state["channels_configured"] = channels_configured
    text = FETCH_ALERT_TEXT.get(source_key or "", {})
    title = text.get("title", "【监控报警·预警】数据获取失败")
    body = ("数据源：%s\n%s：%s\n时间：%s\n%s\n查看仪表盘：%s"
            % (label, text.get("detail_label", "失败原因"), detail, now_s,
               text.get("note", DEFAULT_FETCH_NOTE), DASHBOARD_URL))
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
        "episode_sent": bool(old.get("episode_sent")) and old_level != "normal",
    }
    sent = []
    for ch in ("feishu", "bark"):
        ch_sent_at = _parse_utc(channels[ch].get("last_sent_at_utc"))
        if not _channel_should_send(old_level, "warning", ch_sent_at, now_utc):
            continue
        if not channels_configured[ch]:
            print("  fetch alert %s (no %s configured, not sent)" % (key, ch))
            continue
        if _send_channel(ch, title, body, "orange", "warning"):
            channels[ch] = {"last_sent_at_utc": now_s, "last_sent_value": detail}
            sent.append(ch)
    if sent:
        entry["last_sent_at_utc"] = max(c["last_sent_at_utc"] for c in channels.values()
                                        if c.get("last_sent_at_utc"))
        entry["last_sent_value"] = detail
        entry["episode_sent"] = True
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
        source_key=source_key,
    )
    print("  fetch failure %s: %s (alert sent via: %s)"
          % (source_key, detail, sent or "none"))
    return sent


def record_fetch_success(source_key, state, now_utc, detail=None):
    """Mark a source healthy; clear its alert and send a recovery notice if
    a failure alert was pushed during this episode."""
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
    alerts = state.setdefault("alerts", {})
    old = alerts.get(key, {}) or {}
    if old.get("level") != "normal":
        label = FETCH_SOURCES.get(source_key, source_key)
        alerts[key] = {
            "level": "normal",
            "level_label": "正常",
            "value": "ok",
            "date": now_utc.date().isoformat(),
            "threshold": "-",
            "change": label,
            "last_sent_at_utc": old.get("last_sent_at_utc"),
            "last_sent_value": old.get("last_sent_value"),
            "channels": old.get("channels", {}),
            "episode_sent": False,
        }
        if old.get("level") == "warning" and old.get("episode_sent"):
            title = "【监控恢复】%s" % ("数据已恢复更新" if source_key == "freshness"
                                      else "数据源已恢复")
            body = "数据源：%s\n恢复时间：%s\n查看仪表盘：%s" % (label, now_s, DASHBOARD_URL)
            configured = _channels_configured()
            got = [ch for ch in ("feishu", "bark")
                   if configured[ch] and _send_channel(ch, title, body, "green", "recovery")]
            print("  fetch recovered %s (notice sent via: %s)" % (source_key, got or "none"))


def process_alerts(payload, state_path, now_utc, group_errors=None,
                   group_providers=None):
    """Evaluate, dedup and (maybe) send alerts on Feishu (card) + Bark."""
    group_errors = group_errors or {}
    group_providers = group_providers or {}
    state = load_alert_state(state_path)
    now_s = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    channels_configured = _channels_configured()

    fetch_status = dict(state.get("fetch_status", {}) or {})
    fetch_status.pop("yahoo", None)
    state["fetch_status"] = fetch_status
    alerts = state.setdefault("alerts", {})
    alerts.pop("fetch_yahoo", None)
    for gkey in FETCH_GROUPS:
        if gkey in group_errors:
            record_fetch_failure_in_state(gkey, group_errors[gkey], state, now_utc)
        else:
            record_fetch_success(
                gkey, state, now_utc, detail=group_providers.get(gkey))

    alerts = state.setdefault("alerts", {})
    prev_levels = {k: (v or {}).get("level", "normal")
                   for k, v in alerts.items() if isinstance(v, dict)}
    evaluated = evaluate_alerts(payload, prev_levels)
    sent = []
    for key, ev in evaluated.items():
        spec = ALERT_THRESHOLDS[key]
        old = alerts.get(key, {}) or {}
        old_level = old.get("level", "normal")
        old_channels = old.get("channels", {}) or {}
        new_level = ev["level"]
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
            "episode_sent": (bool(old.get("episode_sent")) and old_level != "normal"
                             and new_level != "normal"),
        }

        if new_level == "normal":
            # "解除" notice: only for level alerts that were actually pushed.
            if old_level != "normal" and old.get("episode_sent") and not spec.get("event"):
                title, body = format_recovery_parts(key, ev)
                got = []
                for ch in ("feishu", "bark"):
                    if not channels_configured[ch]:
                        continue
                    if _send_channel(ch, title, body, "green", "recovery"):
                        channels[ch] = {"last_sent_at_utc": now_s,
                                        "last_sent_value": ev["value"]}
                        got.append(ch)
                if got:
                    entry["last_sent_at_utc"] = now_s
                    sent.append(key + ":resolved")
            alerts[key] = entry
            continue

        title, body = format_alert_parts(key, ev)
        template = {"warning": "orange", "critical": "red"}.get(new_level, "blue")
        any_sent = False
        for ch in ("feishu", "bark"):
            ch_sent_at = _parse_utc(channels[ch].get("last_sent_at_utc"))
            if not _channel_should_send(old_level, new_level, ch_sent_at, now_utc):
                continue
            if not channels_configured[ch]:
                print("  alert %s=%s (no %s configured, not sent)"
                      % (key, new_level, ch))
                continue
            if _send_channel(ch, title, body, template, new_level):
                channels[ch] = {"last_sent_at_utc": now_s,
                                "last_sent_value": ev["value"]}
                any_sent = True
        if any_sent:
            entry["last_sent_at_utc"] = max(c["last_sent_at_utc"] for c in channels.values()
                                            if c.get("last_sent_at_utc"))
            entry["last_sent_value"] = ev["value"]
            entry["episode_sent"] = True
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
# 波动情况 bands. Shipped to the page in payload["config"]["status_bands"];
# the page's DEFAULT_STATUS_BANDS (used only for its offline snapshot) must
# match -- tests/test_parity.py enforces it. JSON-safe on purpose: a band
# maximum of None means "no upper bound"; calm = [min, max) of the "normal"
# bands where a big single-day move overrides the label with 异动; jump is
# the size of that move (pct = percent change, abs = absolute change).
STATUS_BANDS = {
    "vix": {"bands": [[20, "平静", "b-green"], [30, "警惕", "b-amber"],
                      [40, "紧张", "b-orange"], [None, "恐慌", "b-red"]],
            "calm": [None, 20], "jump": {"pct": 10}},
    "vvix": {"bands": [[70, "偏低", "b-green"], [120, "正常", "b-green"],
                       [140, "偏高", "b-amber"], [None, "极端", "b-red"]],
             "calm": [70, 120], "jump": {"pct": 10}},
    "skew": {"bands": [[120, "尾部风险定价正常", "b-green"],
                       [140, "尾部风险定价略高", "b-amber"],
                       [None, "尾部风险定价偏高", "b-red"]],
             "calm": [None, 120], "jump": {"pct": 10}},
    "move": {"bands": [[60, "债市平静", "b-green"], [120, "债市常态", "b-green"],
                       [140, "债市偏高", "b-amber"], [None, "债市紧张", "b-red"]],
             "calm": [60, 120], "jump": {"pct": 10}},
    "hy_oas": {"bands": [[300, "信用偏紧", "b-green"], [500, "信用常态", "b-green"],
                         [800, "信用走阔", "b-amber"], [1000, "信用紧张", "b-orange"],
                         [None, "信用极端", "b-red"]],
               "calm": [300, 500], "jump": {"abs": 25}},
}
_STATUS_SEV = {"b-green": 0, "b-amber": 1, "b-orange": 2, "b-red": 3}
_VERDICT_TITLES = {"b-green": "整体平静", "b-amber": "有所升温，未到恐慌",
                   "b-orange": "压力明显", "b-red": "高度紧张"}


def _in_range(v, rng):
    lo, hi = rng
    return (lo is None or v >= lo) and (hi is None or v < hi)


def status_label(key, value, change_pct=None, change_abs=None):
    """波动情况 label + class for one indicator (the page uses the same bands)."""
    spec = STATUS_BANDS[key]
    label, cls = spec["bands"][-1][1], spec["bands"][-1][2]
    for maximum, lab, c in spec["bands"]:
        if maximum is None or value < maximum:
            label, cls = lab, c
            break
    jump = spec.get("jump") or {}
    if _in_range(value, spec["calm"]):
        if ("pct" in jump and change_pct is not None
                and abs(change_pct) >= jump["pct"]):
            return {"label": "异动", "cls": "b-amber"}
        if ("abs" in jump and change_abs is not None
                and abs(change_abs) >= jump["abs"]):
            return {"label": "异动", "cls": "b-amber"}
    return {"label": label, "cls": cls}


def _signed(value, decimals=2):
    return "%s%.*f" % ("+" if value > 0 else "", decimals, value)


def build_status_message(payload, stale=False):
    """(title, body, template) for a manual status push. No state is touched."""
    ind = payload["indicators"]
    hy = ind["hy_oas"]
    hy_pct = (hy["change_bp"] / hy["prev_value_bp"] * 100
              if hy.get("prev_value_bp") else None)
    st = {
        "vix": status_label("vix", ind["vix"]["value"], ind["vix"]["change_pct"]),
        "vvix": status_label("vvix", ind["vvix"]["value"], ind["vvix"]["change_pct"]),
        "skew": status_label("skew", ind["skew"]["value"], ind["skew"]["change_pct"]),
        "move": status_label("move", ind["move"]["value"], ind["move"]["change_pct"]),
        "hy_oas": status_label("hy_oas", hy["value_bp"], hy_pct, hy.get("change_bp")),
    }
    worst_cls = max((st[k]["cls"] for k in ("vix", "move", "hy_oas")),
                    key=lambda c: _STATUS_SEV[c])
    spx = payload["spx"]
    spx_abs = (spx["value"] - spx["prev_close"]
               if spx.get("prev_close") is not None else None)
    stale_labels = {
        "vix": "VIX", "vvix": "VVIX", "skew": "SKEW",
        "move": "MOVE", "hy_oas": "HY OAS", "spx": "标普 500",
        "vix1d": "VIX1D", "vix9d": "VIX9D", "vix3m": "VIX3M", "vix6m": "VIX6M",
    }
    stale_keys = set(payload.get("stale_indicators") or [])
    for k in ("vix", "vvix", "skew", "move"):
        if ind[k].get("stale"):
            stale_keys.add(k)
    if hy.get("stale"):
        stale_keys.add("hy_oas")
    if spx.get("stale"):
        stale_keys.add("spx")
    ts = payload.get("term_structure") or {}
    for tk in (ts.get("stale_tenors") or []):
        stale_keys.add(tk)
    has_stale = stale or bool(stale_keys)

    lines = ["综合判断：%s" % _VERDICT_TITLES[worst_cls]]
    comp = payload.get("composite") or {}
    if comp.get("score") is not None:
        lines.append("情绪压力分：%s / 100（近一年分位加权）" % comp["score"])
    if has_stale:
        lines.append("注意：部分数据源本次拉取失败，相应项保留上次成功数据")
    for k in ("vix", "vvix", "skew", "move"):
        lines.append("%s %s（%s，%s%%）· %s" % (
            ALERT_THRESHOLDS[k]["label"],
            ind[k]["value"],
            _signed(ind[k]["value"] - ind[k]["prev_close"]),
            _signed(ind[k]["change_pct"]), st[k]["label"]))
    lines.append("%s %sbp（%sbp，%s%%）· %s" % (
        ALERT_THRESHOLDS["hy_oas"]["label"],
        hy["value_bp"], _signed(hy["change_bp"], 0),
        _signed(hy_pct) if hy_pct is not None else "—", st["hy_oas"]["label"]))
    lines.append("标普 500 %s（%s，%s%%）" % (
        format(spx["value"], ","),
        _signed(spx_abs) if spx_abs is not None else "—",
        _signed(spx["change_pct"])))
    ts_vals = [ts.get(k) for k in ("vix1d", "vix9d", "vix3m", "vix6m")]
    if all(v is not None for v in ts_vals):
        ts_line = "期限结构：1D %.2f / 9D %.2f / 3M %.2f / 6M %.2f" % tuple(ts_vals)
        if ts.get("vix") and ts.get("vix3m"):
            ratio = ts["vix"] / ts["vix3m"]
            ts_line += "（VIX/VIX3M %.2f，%s）" % (ratio, "倒挂" if ratio > 1 else "升水")
        if ts.get("date_mismatch"):
            ts_line += "（各期限日期不一致）"
        lines.append(ts_line)
    lines.append("数据：VIX %s，MOVE %s，HY OAS %s" % (
        ind["vix"]["date"], ind["move"]["date"], hy["date"]))
    stale_notes = [stale_labels[k] for k in (
        "vix", "vvix", "skew", "move", "hy_oas", "spx",
        "vix1d", "vix9d", "vix3m", "vix6m",
    ) if k in stale_keys]
    if stale_notes:
        lines.append("（%s 为上次缓存值，本次未拉到）" % " / ".join(stale_notes))
    lagging = [NODE_LABELS[k] for k in ("vix", "vvix", "skew", "move", "spx", "hy_oas")
               if (ind.get(k) or payload.get(k) or {}).get("lagging")]
    if lagging:
        lines.append("（%s 收盘日期落后于最近交易日 %s）"
                     % (" / ".join(lagging), payload.get("expected_close_date", "")))
    if payload.get("intraday"):
        lines.append("（当前为盘中读数，收盘前仍可能变化）")
    lines.append("查看仪表盘：%s" % DASHBOARD_URL)
    template = {"b-green": "green", "b-amber": "yellow",
                "b-orange": "orange", "b-red": "red"}[worst_cls]
    return "【手动推送】市场监控状态", "\n".join(lines), template


def send_manual_push(mode, payload=None, stale=False,
                     channels=("feishu", "bark")):
    """Send a manual push on the given channels. No dedup, no state writes.

    Stdout format matches api/api_server.py's _PUSH_RE:
        manual push <mode> (<channels>): feishu=<bool> bark=<bool>
    """
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
    out_dir = Path(os.environ.get("MM_OUT_DIR") or (root / "dashboard"))
    out_path = out_dir / "data.json"
    state_path = out_dir / "alert_state.json"

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

    prev_payload = None
    if out_path.exists():
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
        eq_used = sorted({
            sources_map[k]
            for k in ("vix", "vvix", "skew", "vix1d", "vix9d", "vix3m", "vix6m", "spx")
            if k in sources_map
        })
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

    stale_indicators = sorted(
        stale_eq
        | ({"move"} if move_stale else set())
        | ({"hy_oas"} if hy_stale else set())
    )

    term_dates = {
        "vix1d": pairs["vix1d"][-1]["date"],
        "vix9d": pairs["vix9d"][-1]["date"],
        "vix": pairs["vix"][-1]["date"],
        "vix3m": pairs["vix3m"][-1]["date"],
        "vix6m": pairs["vix6m"][-1]["date"],
    }
    term_stale_tenors = sorted(
        k for k in ("vix1d", "vix9d", "vix", "vix3m", "vix6m") if k in stale_eq
    )
    term_structure_payload = {
        "vix1d": pairs["vix1d"][-1]["value"],
        "vix9d": pairs["vix9d"][-1]["value"],
        "vix": pairs["vix"][-1]["value"],
        "vix3m": pairs["vix3m"][-1]["value"],
        "vix6m": pairs["vix6m"][-1]["value"],
        "date": pairs["vix"][-1]["date"],
        "dates": term_dates,
        "date_mismatch": len(set(term_dates.values())) > 1,
    }
    if term_stale_tenors:
        term_structure_payload["stale"] = True
        term_structure_payload["stale_tenors"] = term_stale_tenors

    now_utc = dt.datetime.now(dt.timezone.utc)
    beijing = now_utc.astimezone(dt.timezone(dt.timedelta(hours=8)))

    # 4. Stats (1y percentile, 5d / 20d change) + composite stress score.
    def _prev_stats(k):
        try:
            node = prev_payload["spx"] if k == "spx" else prev_payload["indicators"][k]
            return node.get("stats")
        except (KeyError, TypeError, AttributeError):
            return None

    stats = {k: compute_stats(pairs[k], _prev_stats(k))
             for k in ("vix", "vvix", "skew", "move", "spx")}
    stats["hy_oas"] = compute_stats(fred_rows, _prev_stats("hy_oas"), is_bp=True)

    # 5. Credit tiers context (IG / CCC OAS), optional.
    credit_tiers = {}
    if os.environ.get("CREDIT_TIERS", "1").strip().lower() not in ("0", "false", "no", "off"):
        credit_tiers = resolve_credit_tiers(prev_payload)

    hy_node = {
        "value_bp": hy_last["value"],
        "value_pct": round(hy_last["value"] / 100, 2),
        "date": hy_last["date"],
        "prev_value_bp": hy_prev["value"],
        "prev_date": hy_prev["date"],
        "change_bp": hy_change_bp,
        "history": fred_rows[-DISPLAY_KEEP:],
        "stale": hy_stale,
        "source": hy_src,
    }
    if stats.get("hy_oas"):
        hy_node["stats"] = stats["hy_oas"]

    payload = {
        "updated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at_beijing": beijing.strftime("%Y-%m-%d %H:%M Beijing"),
        "source": _describe_sources(sources_map),
        "sources_used": sources_map,
        "stale_indicators": stale_indicators,
        "market_status": "computed in browser from US ET time",
        "indicators": {
            "vix": indicator(pairs["vix"], stale=("vix" in stale_eq),
                             source=sources_map.get("vix"), stats=stats["vix"]),
            "vvix": indicator(pairs["vvix"], stale=("vvix" in stale_eq),
                              source=sources_map.get("vvix"), stats=stats["vvix"]),
            "skew": indicator(pairs["skew"], stale=("skew" in stale_eq),
                              source=sources_map.get("skew"), stats=stats["skew"]),
            "move": indicator(pairs["move"], stale=move_stale,
                              source=move_src, stats=stats["move"]),
            "hy_oas": hy_node,
        },
        "spx": indicator(pairs["spx"], stale=("spx" in stale_eq),
                         source=sources_map.get("spx"), stats=stats["spx"]),
        "term_structure": term_structure_payload,
        "composite": composite_score(stats),
        "credit_tiers": credit_tiers,
        "config": {
            "status_bands": STATUS_BANDS,
            "alert_thresholds": ALERT_THRESHOLDS,
            "fresh_allowed_lag": FRESH_ALLOWED_LAG,
        },
    }

    # 6. Freshness: data dates vs the latest NYSE session.
    expected, fresh_issues = annotate_freshness(payload, now_utc, set(stale_indicators))
    if fresh_issues:
        group_errors["freshness"] = ("预期收盘日 %s：%s"
                                     % (expected, "；".join(fresh_issues)))[:300]
    else:
        group_providers["freshness"] = "ok (expected %s)" % expected

    _atomic_write_json(out_path, payload)
    print("Wrote %s" % (out_path,))
    for k in ("vix", "vvix", "skew", "move"):
        ind = payload["indicators"][k]
        hl = ""
        if ind.get("day_low") is not None and ind.get("day_high") is not None:
            hl = ", L %s / H %s" % (ind["day_low"], ind["day_high"])
        print("  %s: %s on %s (prev %s, %s%%%s) [%s%s%s]"
              % (k, ind["value"], ind["date"], ind["prev_close"],
                 ind["change_pct"], hl, sources_map.get(k),
                 ", STALE" if ind.get("stale") else "",
                 ", LAG %s" % ind["lag_days"] if ind.get("lag_days") else ""))
    hy = payload["indicators"]["hy_oas"]
    print("  hy_oas: %sbp on %s (prev %sbp %s) [%s%s]"
          % (hy["value_bp"], hy["date"], hy["prev_value_bp"], hy["prev_date"],
             hy_src, ", STALE" if hy_stale else ""))
    print("  spx: %s on %s [%s]"
          % (payload["spx"]["value"], payload["spx"]["date"], sources_map.get("spx")))
    print("  term: %s [%s]" % (payload["term_structure"], sources_map.get("vix1d")))
    print("  expected close: %s; freshness issues: %s"
          % (expected, fresh_issues or "none"))
    print("  composite: %s" % (payload["composite"],))
    if credit_tiers:
        print("  credit tiers: %s" % ", ".join(
            "%s %sbp (%s)%s" % (k, v.get("value_bp"), v.get("date"),
                               " STALE" if v.get("stale") else "")
            for k, v in credit_tiers.items()))

    new_state, sent = process_alerts(
        payload,
        state_path,
        now_utc,
        group_errors=group_errors,
        group_providers=group_providers,
    )
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
        any_stale = bool(stale_indicators or hy_stale)
        send_manual_push("status", payload, stale=any_stale, channels=channels)


if __name__ == "__main__":
    main()
