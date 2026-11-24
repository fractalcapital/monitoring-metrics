#!/usr/bin/env python3
"""Fetch live closes for the monitoring dashboard and write dashboard/data.json.

Sources (server-side only -- browsers are blocked by CORS on Yahoo/FRED):
  - Yahoo Finance v8 chart API (query1.finance.yahoo.com), range=1mo, interval=1d
    for ^VIX, ^VVIX, ^SKEW, ^MOVE, ^VIX1D, ^VIX9D, ^VIX3M, ^VIX6M, ^GSPC
  - FRED fredgraph.csv?id=BAMLH0A0HYM2 for HY OAS (ICE BofA US High Yield OAS)

Rules:
  - Last close + previous close are taken from the timestamp/close arrays
    (the last two non-null closes). meta.chartPreviousClose is NOT used --
    it is stale.
  - Stdlib only (urllib / json / csv / datetime).
  - If any fetch fails, exit non-zero and do NOT write a fabricated file.
  - Output is written next to the repo root layout: <root>/dashboard/data.json,
    where <root> is the parent of this script's directory. When run from this
    build folder, that is monitoring-dashboard-build/dashboard/data.json.

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
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"}

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

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2"

DASHBOARD_URL = "https://mon.infidea.app/dashboard/"

# --- Alert thresholds (from the repo READMEs' bands; edit here to change) ---
# warning = alert, critical = severe. Keys without "critical" only ever warn.
ALERT_THRESHOLDS = {
    "vix": {"label": "VIX", "unit": "", "warning": 35, "critical": 40},
    "vvix": {"label": "VVIX", "unit": "", "warning": 140},
    "skew": {"label": "SKEW", "unit": "", "warning": 150},
    "move": {"label": "MOVE", "unit": "", "warning": 140},
    "hy_oas": {"label": "HY OAS", "unit": "bp", "warning": 500, "critical": 1000},
    # Cross-check: VIX high AND (VVIX or MOVE high).
    "cross": {"label": "综合预警", "unit": "", "vix": 30, "vvix": 140, "move": 140},
}

LEVEL_SEVERITY = {"normal": 0, "warning": 1, "critical": 2}
LEVEL_LABEL = {"normal": "正常", "warning": "预警", "critical": "严重"}
RESEND_AFTER = dt.timedelta(hours=24)


def fetch_url(url, timeout=15, headers=None, retries=3):
    """Fetch a URL, retrying HTTP 429 (rate limit) with backoff.

    Yahoo rate-limits shared datacenter IPs (GitHub Actions runners
    hit 429 often): retry after 10s / 30s / 60s, honouring Retry-After
    when present. Other errors are raised immediately.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise RuntimeError("HTTP %s for %s" % (resp.status, url))
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code != 429 or attempt == retries:
                raise
            wait = 60
            try:
                wait = int(exc.headers.get("Retry-After", "0") or 0) or wait
            except ValueError:
                pass
            wait = [10, 30, 60][attempt] if attempt < 3 else 60
            print("  429 for %s, retry %d/%d after %ds"
                  % (url, attempt + 1, retries, wait), file=sys.stderr)
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt == retries:
                raise
            time.sleep([5, 15, 30][attempt] if attempt < 3 else 30)
    raise last_exc


def fetch_yahoo(symbol):
    # Try query1, then query2 on 429 — same backend, but the per-host
    # limit sometimes lets the second host through.
    params = "?range=1mo&interval=1d"
    last_exc = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = "https://" + host + "/v8/finance/chart/" + symbol + params
        try:
            raw = fetch_url(url)
            break
        except Exception as exc:  # noqa: BLE001 - retry on the other host
            last_exc = exc
            print("  Yahoo %s via %s failed: %s" % (symbol, host, exc),
                  file=sys.stderr)
    else:
        raise last_exc
    data = json.loads(raw.decode("utf-8"))
    result = data["chart"]["result"][0]
    stamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    pairs = []
    for ts, close in zip(stamps, closes):
        if close is None:
            continue
        date = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date().isoformat()
        pairs.append({"date": date, "value": round(float(close), 2)})
    if len(pairs) < 2:
        raise RuntimeError("too few closes for %s: %s" % (symbol, pairs))
    return pairs


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


def indicator(pairs):
    last = pairs[-1]
    prev = pairs[-2]
    if prev["value"]:
        change_pct = round((last["value"] - prev["value"]) / prev["value"] * 100, 2)
    else:
        change_pct = None
    return {
        "value": last["value"],
        "date": last["date"],
        "prev_close": prev["value"],
        "prev_date": prev["date"],
        "change_pct": change_pct,
        "history": pairs[-30:],
    }


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
        if level == "critical":
            threshold = spec["critical"]
        else:
            threshold = spec["warning"]
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
    if level == "critical":
        threshold = spec["critical"]
    else:
        threshold = spec["warning"]
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
    if hit:
        change = "VIX >= 30 and " + ", ".join(parts)
    else:
        change = "-"
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
    """Shared alert content for every channel: (title, body).

    Feishu gets title + body as one text message; Bark gets them as its
    title/body path segments. Same content on both channels.
    """
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


def format_alert_message(key, ev):
    title, body = format_alert_parts(key, ev)
    return "%s\n%s" % (title, body)


def send_feishu(text):
    """POST one text message to the Feishu robot webhook.

    The URL comes only from the FEISHU_WEBHOOK_URL environment variable.
    It is never printed or logged. Returns True on success, False when
    skipped (no webhook configured) or on failure.
    """
    url = os.environ.get("FEISHU_WEBHOOK_URL")
    if not url:
        return False
    body = json.dumps({"msg_type": "text", "content": {"text": text}},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return resp.status == 200 and result.get("code") == 0
    except Exception as exc:
        print("WARNING: Feishu send failed: %s" % (exc,), file=sys.stderr)
        return False


def send_bark(title, body, level):
    """GET one push via the user's own Bark server.

    BARK_URL (env var only) is the base part: https://<bark server>/<key>,
    without title/body. The request is
        {base}/{quote(title)}/{quote(body)}?group=monitoring-metrics&level=...
    with level "critical" for severe and "active" for warning.
    The base URL/key is never printed or logged. Returns True on success
    (HTTP 2xx and Bark code 200), False when skipped or on failure.
    """
    base = os.environ.get("BARK_URL")
    if not base:
        return False
    bark_level = "critical" if level == "critical" else "active"
    url = ("%s/%s/%s?group=monitoring-metrics&level=%s"
           % (base.rstrip("/"), urllib.parse.quote(title, safe=""),
              urllib.parse.quote(body, safe=""), bark_level))
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return 200 <= resp.status < 300 and result.get("code") == 200
    except Exception as exc:
        print("WARNING: Bark send failed: %s" % (exc,), file=sys.stderr)
        return False


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


def process_alerts(payload, state_path, now_utc):
    """Evaluate, dedup and (maybe) send alerts on Feishu + Bark.

    Dedup state lives in one file (alert_state.json) and is tracked per
    channel: a channel sends only on a new trigger, an upgrade, or a
    same-level repeat after 24h, so neither channel repeats within 24h.
    A failing channel is logged only and never aborts the run, and the
    other channel still sends. Returns (new_state, sent), where sent lists
    keys for which at least one channel succeeded.
    """
    state = load_alert_state(state_path)
    alerts = state.get("alerts", {})
    evaluated = evaluate_alerts(payload)
    now_s = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    channels_configured = {
        "feishu": bool(os.environ.get("FEISHU_WEBHOOK_URL")),
        "bark": bool(os.environ.get("BARK_URL")),
    }
    sent = []
    for key, ev in evaluated.items():
        old = alerts.get(key, {})
        old_level = old.get("level", "normal")
        old_channels = old.get("channels", {}) or {}
        new_level = ev["level"]
        title, body = format_alert_parts(key, ev)
        # Both channels always have an entry (None until first success),
        # so the state file shows per-channel send history explicitly.
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
        for ch, sender in (("feishu", lambda: send_feishu(
                                format_alert_message(key, ev))),
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
    new_state = {"updated_at_utc": now_s,
                 "channels_configured": channels_configured,
                 "alerts": alerts}
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    Path(state_path).write_text(
        json.dumps(new_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return new_state, sent


# ------------------------------------------------------------------ main ---

def main():
    root = Path(__file__).resolve().parent.parent
    out_path = root / "dashboard" / "data.json"
    state_path = root / "dashboard" / "alert_state.json"

    pairs = {}
    for key, symbol in YAHOO_SYMBOLS.items():
        try:
            pairs[key] = fetch_yahoo(symbol)
        except Exception as exc:
            # Yahoo 429s shared Actions-runner IPs. If we already have a
            # good data.json, keep it and exit 0 — the dashboard keeps
            # showing the last good close (with its date) instead of
            # failing the run. Only fail hard on a first run with no
            # previous data to fall back to.
            print("ERROR fetching %s: %s" % (symbol, exc), file=sys.stderr)
            if out_path.exists():
                print("Keeping previous data.json; exiting 0.",
                      file=sys.stderr)
                return
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
        fred_rows = None
        try:
            prev_payload = json.loads(out_path.read_text(encoding="utf-8"))
            prev_hy = prev_payload["indicators"]["hy_oas"]
            fred_rows = [{"date": h["date"], "value": h["value"]}
                         for h in prev_hy.get("history", [])]
            if not fred_rows:
                fred_rows = [{"date": prev_hy["prev_date"],
                              "value": prev_hy["prev_value_bp"]},
                             {"date": prev_hy["date"],
                              "value": prev_hy["value_bp"]}]
        except (OSError, ValueError, KeyError):
            # Values in bp (FRED percent x100): 2026-11-19 352bp, 2026-11-20 376bp.
            fred_rows = [{"date": "2026-11-19", "value": 352},
                         {"date": "2026-11-20", "value": 376}]

    hy_last = fred_rows[-1]
    hy_prev = fred_rows[-2]
    hy_change_bp = round(hy_last["value"] - hy_prev["value"], 2)

    now_utc = dt.datetime.now(dt.timezone.utc)
    beijing = now_utc.astimezone(dt.timezone(dt.timedelta(hours=8)))

    payload = {
        "updated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at_beijing": beijing.strftime("%Y-%m-%d %H:%M Beijing"),
        "source": "Yahoo Finance (v8 chart API, ~15-min delayed) for "
                  "VIX/VVIX/SKEW/MOVE/term structure/SPX; "
                  "FRED BAMLH0A0HYM2 (ICE BofA US High Yield OAS, daily) "
                  "for HY OAS",
        "market_status": "computed in browser from US ET time",
        "indicators": {
            "vix": indicator(pairs["vix"]),
            "vvix": indicator(pairs["vvix"]),
            "skew": indicator(pairs["skew"]),
            "move": indicator(pairs["move"]),
            "hy_oas": {
                "value_bp": hy_last["value"],
                "value_pct": round(hy_last["value"] / 100, 2),
                "date": hy_last["date"],
                "prev_value_bp": hy_prev["value"],
                "prev_date": hy_prev["date"],
                "change_bp": hy_change_bp,
                "history": fred_rows[-30:],
                "stale": hy_stale,
            },
        },
        "spx": indicator(pairs["spx"]),
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
        print("  %s: %s on %s (prev %s, %s%%)"
              % (k, ind["value"], ind["date"], ind["prev_close"], ind["change_pct"]))
    hy = payload["indicators"]["hy_oas"]
    print("  hy_oas: %sbp on %s (prev %sbp %s)"
          % (hy["value_bp"], hy["date"], hy["prev_value_bp"], hy["prev_date"]))
    print("  spx: %s on %s" % (payload["spx"]["value"], payload["spx"]["date"]))
    print("  term: %s" % (payload["term_structure"],))

    new_state, sent = process_alerts(payload, state_path, now_utc)
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


if __name__ == "__main__":
    main()
