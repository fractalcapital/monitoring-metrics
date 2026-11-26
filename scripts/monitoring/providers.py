"""Market data providers (Futu CLI, CBOE CDN, FRED, Stooq, Yahoo curl_cffi, TradingView, CSV)."""
import csv
import datetime as dt
import io
import json
import math
import os
import shutil
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

from .calendar import _latest_us_weekday_iso, _normalize_date
from .config import (
    CBOE_BASE,
    CBOE_FILES,
    CREDIT_TIER_SERIES,
    DEFAULT_FUTU_SYMBOLS,
    FRED_API_BASE,
    FRED_CSV_BASE,
    HISTORY_KEEP,
    PLAIN_UA,
    STOOQ_SPX_URL,
    TRADING_TOOLS_MOVE_URL,
    TV_SCAN_URL,
    TV_SYMBOL_URL,
    UA,
    YAHOO_SYMBOLS,
)
from .series import (
    _finalize_pairs,
    _prev_pairs_for,
    _round_num,
    _sanitize_hl,
    _upsert_history,
    _valid_hl,
    compute_stats,
)

# Always fetch direct, ignoring any OS proxy (e.g. a Mac routing other traffic
# through a VPS proxy). On the VPS there is no proxy.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_YAHOO_SESSION_CACHE = {"session": None, "crumb": None, "initialized": False}


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
        "limit": "350",
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
    params = {"range": "2y", "interval": "1d"}
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
    query = "?range=2y&interval=1d"
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


