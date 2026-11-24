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

# Alert delivery lives in alert/notify.py. The script runs as
# `python3 scripts/fetch_dashboard.py` from the repo root, so
# sys.path[0] is scripts/ — add the repo root first so `alert` imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from alert.notify import send_bark, send_feishu  # noqa: E402

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

FETCH_SOURCES = {
    "yahoo": "Yahoo Finance（VIX / VVIX / SKEW / MOVE / 标普 500 / 期限结构）",
    "fred": "FRED（HY OAS）",
}


def _write_state(state_path, state):
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    Path(state_path).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def _fetch_alert_send(key, label, detail, state, now_utc):
    """Send a fetch-failure alert on Feishu + Bark with 24h dedup.

    state is the loaded alert_state dict (mutated in place). Returns the
    list of channels that actually sent. No send on first failure is
    impossible — the first failure is exactly when the user needs to
    know; repeats are suppressed for 24h per channel.
    """
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
            "仪表盘当前显示的是上一次成功拉取的数据（带日期），不是最新收盘，"
            "请及时检查数据源。\n"
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
    for ch, sender in (("feishu", lambda: send_feishu("%s\n%s" % (title, body))),
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


def record_fetch_failure(source_key, detail, state_path, now_utc):
    """Record a data-source failure and alert on it (24h dedup).

    Updates state["fetch_status"][source_key] and the
    state["alerts"]["fetch_<source>"] entry, sends on Feishu + Bark
    when due, and writes the state file. Never raises.
    """
    try:
        state = load_alert_state(state_path)
        now_s = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        status = state.setdefault("fetch_status", {})
        prev = status.get(source_key, {}) or {}
        status[source_key] = {
            "ok": False,
            "last_error": detail,
            "last_failed_at_utc": now_s,
            "last_ok_at_utc": prev.get("last_ok_at_utc"),
        }
        state["updated_at_utc"] = now_s
        sent = _fetch_alert_send("fetch_" + source_key,
                                 FETCH_SOURCES[source_key], detail,
                                 state, now_utc)
        _write_state(state_path, state)
        print("  fetch failure %s: %s (alert sent via: %s)"
              % (source_key, detail, sent or "none"))
        return sent
    except Exception as exc:  # noqa: BLE001 - alerting must never abort a run
        print("WARNING record_fetch_failure failed: %s" % (exc,),
              file=sys.stderr)
        return []


def record_fetch_success(source_key, state, now_utc):
    """Mark a source healthy in an already-loaded state dict (in memory
    only; the caller writes the file). Clears the warning level so the
    next failure alerts again."""
    now_s = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    status = state.setdefault("fetch_status", {})
    prev = status.get(source_key, {}) or {}
    status[source_key] = {
        "ok": True,
        "last_error": None,
        "last_failed_at_utc": prev.get("last_failed_at_utc"),
        "last_ok_at_utc": now_s,
    }
    key = "fetch_" + source_key
    old = (state.get("alerts", {}) or {}).get(key, {})
    if old.get("level") != "normal":
        state.setdefault("alerts", {})[key] = {
            "level": "normal",
            "level_label": "正常",
            "value": "ok",
            "date": now_utc.date().isoformat(),
            "threshold": "-",
            "change": FETCH_SOURCES[source_key],
            "last_sent_at_utc": old.get("last_sent_at_utc"),
            "last_sent_value": old.get("last_sent_value"),
            "channels": old.get("channels", {}),
        }


def process_alerts(payload, state_path, now_utc, hy_stale=False):
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
    # A successful Yahoo fetch means the Yahoo source is healthy again;
    # FRED is healthy only when this run actually fetched it (not stale).
    # Fetch-failure entries are preserved, not dropped, so the dashboard
    # keeps showing the last failure until the source recovers.
    fetch_status = dict(state.get("fetch_status", {}) or {})
    state["fetch_status"] = fetch_status
    record_fetch_success("yahoo", state, now_utc)
    if not hy_stale:
        record_fetch_success("fred", state, now_utc)
    alerts = state.get("alerts", {})
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
                 "fetch_status": state.get("fetch_status", {}),
                 "alerts": alerts}
    _write_state(state_path, new_state)
    return new_state, sent


# ------------------------------------------------------- manual push ---
# 波动情况 labels: must mirror the dashboard page's STATUS_BANDS /
# statusFor in dashboard/index.html exactly (same bands, same calm
# predicates, same 异动 override). If the page's rules change, change
# these too, and vice versa.
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
    """(title, body) for a manual status push. No state is touched."""
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
        lines.append("注意：数据拉取失败，以下是上次成功数据")
    lines.append("VIX %s（%s，%s%%）· %s" % (
        ind["vix"]["value"],
        _signed(ind["vix"]["value"] - ind["vix"]["prev_close"]),
        _signed(ind["vix"]["change_pct"]), st["vix"]["label"]))
    lines.append("VVIX %s（%s，%s%%）· %s" % (
        ind["vvix"]["value"],
        _signed(ind["vvix"]["value"] - ind["vvix"]["prev_close"]),
        _signed(ind["vvix"]["change_pct"]), st["vvix"]["label"]))
    lines.append("SKEW %s（%s，%s%%）· %s" % (
        ind["skew"]["value"],
        _signed(ind["skew"]["value"] - ind["skew"]["prev_close"]),
        _signed(ind["skew"]["change_pct"]), st["skew"]["label"]))
    lines.append("MOVE %s（%s，%s%%）· %s" % (
        ind["move"]["value"],
        _signed(ind["move"]["value"] - ind["move"]["prev_close"]),
        _signed(ind["move"]["change_pct"]), st["move"]["label"]))
    lines.append("HY OAS %sbp（%sbp，%s%%）· %s" % (
        hy["value_bp"], _signed(hy["change_bp"], 0),
        _signed(hy_pct) if hy_pct is not None else "—", st["hy_oas"]["label"]))
    lines.append("标普 500 %s（%s，%s%%）" % (
        format(spx["value"], ","),
        _signed(spx_abs) if spx_abs is not None else "—",
        _signed(spx["change_pct"])))
    lines.append("数据：VIX 等 %s，HY OAS %s" % (
        ind["vix"]["date"], hy["date"]))
    if hy.get("stale"):
        lines.append("（HY OAS 为上次值，FRED 本次未拉到）")
    lines.append("查看仪表盘：%s" % DASHBOARD_URL)
    return "【手动推送】市场监控状态", "\n".join(lines)


def send_manual_push(mode, payload=None, stale=False,
                     channels=("feishu", "bark")):
    """Send a manual push on the given channels. No dedup, no state writes.

    mode "test": one test message, no payload needed. mode "status":
    a status summary built from payload. channels selects a subset
    ("feishu" and/or "bark"); the dashboard has one button per channel.
    Returns True if at least one requested channel succeeded. A missing
    channel is skipped, never fatal.
    """
    now_bj = dt.datetime.now(
        dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    if mode == "test":
        title = "【测试】监控报警通道测试"
        body = ("这是一条测试消息，飞书和 Bark 通道已接通。\n"
                "时间：%s（北京时间）\n查看仪表盘：%s"
                % (now_bj, DASHBOARD_URL))
    elif mode == "status":
        title, body = build_status_message(payload, stale=stale)
    else:
        return False
    results = {}
    if "feishu" in channels:
        results["feishu"] = send_feishu("%s\n%s" % (title, body))
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

    # Manual push from the workflow_dispatch input or the VPS API
    # (dashboard push buttons). "test" sends one test message and exits;
    # "status" fetches normally, then sends a status summary.
    # "status-cached" sends the same summary from the existing data.json
    # without fetching: the dashboard buttons use this, because from a
    # datacenter IP the Yahoo fetch takes minutes (429 retries).
    # MANUAL_CHANNELS (comma-separated feishu/bark, default both) selects
    # which channels a manual push goes to; the dashboard has one button
    # per channel and passes it through the VPS API.
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
                # Alert the user that the data could not be fetched, so
                # they can adjust in time (24h dedup per channel).
                _now = dt.datetime.now(dt.timezone.utc)
                record_fetch_failure(
                    "yahoo", "%s: %s" % (symbol, exc),
                    state_path, _now)
                # Still evaluate alerts on the last known closes, so
                # alert_state.json (including channels_configured, which
                # the dashboard displays) stays accurate even when
                # Yahoo is rate-limiting us. Dedup prevents re-sends.
                try:
                    prev_payload = json.loads(
                        out_path.read_text(encoding="utf-8"))
                    now_utc = dt.datetime.now(dt.timezone.utc)
                    new_state, sent = process_alerts(
                        prev_payload, state_path, now_utc, hy_stale=True)
                    print("Wrote %s (stale-data alert check, sent: %s)"
                          % (state_path, sent or "none"))
                    if mode == "status":
                        send_manual_push("status", prev_payload, stale=True,
                                         channels=channels)
                except Exception as exc2:  # noqa: BLE001
                    print("WARNING stale-data alert check failed: %s"
                          % (exc2,), file=sys.stderr)
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
        # Alert the user: HY OAS is now stale. The dashboard keeps the
        # last known value (with its date), but the user asked to know
        # whenever a source cannot be fetched.
        record_fetch_failure(
            "fred", str(exc), state_path,
            dt.datetime.now(dt.timezone.utc))
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

    new_state, sent = process_alerts(payload, state_path, now_utc,
                                     hy_stale=hy_stale)
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
        send_manual_push("status", payload, stale=hy_stale,
                         channels=channels)


if __name__ == "__main__":
    main()
