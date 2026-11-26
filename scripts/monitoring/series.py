"""Series cleanup, intraday H/L sanity checks, stats, composite stress score and JSON I/O."""
import bisect
import datetime as dt
import json
import math
import os
from pathlib import Path

from .calendar import _is_nyse_holiday
from .config import (
    COMPOSITE_WEIGHTS,
    DISPLAY_KEEP,
    HISTORY_KEEP,
    HL_HIGH_CAP,
    HL_LOW_FLOOR,
    STATS_MIN_WINDOW,
    STATS_WINDOW,
)


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




def _pct_rank_in_window(window, val):
    below = sum(1 for v in window if v < val)
    equal = sum(1 for v in window if v == val)
    return int(round(100.0 * (below + 0.5 * equal) / len(window)))


def compute_composite_history(pairs_map, prev_composite=None, keep=DISPLAY_KEEP):
    """Build a rolling 0-100 composite stress score series (no look-ahead bias).

    For each of the latest `keep` trading dates in VIX, every component in
    COMPOSITE_WEIGHTS uses its most recent observation on or before that date
    (handling FRED HY OAS's 1-day lag naturally) and computes that value's
    percentile rank against the trailing STATS_WINDOW closes ending on that
    date. Requires total active weight >= 2.0 to emit a point.
    """
    vix_pairs = (pairs_map or {}).get("vix") or []
    target_dates = [p["date"] for p in vix_pairs[-keep:] if p.get("date")]
    if not target_dates:
        return list((prev_composite or {}).get("history") or [])[-keep:]

    indexed = {}
    for key in COMPOSITE_WEIGHTS:
        series = [
            (p["date"], float(p["value"]))
            for p in ((pairs_map or {}).get(key) or [])
            if p.get("date") and p.get("value") is not None
        ]
        if series:
            indexed[key] = ([x[0] for x in series], [x[1] for x in series])

    out = []
    for d in target_dates:
        num = den = 0.0
        for key, weight in COMPOSITE_WEIGHTS.items():
            if key not in indexed:
                continue
            dates, vals = indexed[key]
            pos = bisect.bisect_right(dates, d) - 1
            if pos < 0:
                continue
            start = max(0, pos - STATS_WINDOW + 1)
            window = vals[start : pos + 1]
            if len(window) < STATS_MIN_WINDOW:
                continue
            num += weight * _pct_rank_in_window(window, vals[pos])
            den += weight
        if den >= 2.0:
            out.append({"date": d, "value": int(round(num / den))})

    if not out and isinstance(prev_composite, dict):
        prev_hist = prev_composite.get("history") or []
        if isinstance(prev_hist, list):
            return prev_hist[-keep:]
    return out[-keep:]


def build_composite(stats_map, pairs_map, prev_composite=None, keep=DISPLAY_KEEP):
    """Return composite stress dict with current score, deltas and 30d history."""
    comp = composite_score(stats_map)
    if comp is None:
        return None
    hist = compute_composite_history(pairs_map, prev_composite=prev_composite, keep=keep)
    # Keep the latest history point aligned with the headline score.
    vix_pairs = (pairs_map or {}).get("vix") or []
    latest_date = vix_pairs[-1]["date"] if vix_pairs else None
    if latest_date:
        if hist and hist[-1]["date"] == latest_date:
            hist[-1] = {"date": latest_date, "value": comp["score"]}
        elif not hist or latest_date > hist[-1]["date"]:
            hist.append({"date": latest_date, "value": comp["score"]})
            hist = hist[-keep:]
    if hist:
        comp["history"] = hist
        if len(hist) >= 2:
            comp["prev_score"] = hist[-2]["value"]
            comp["prev_date"] = hist[-2]["date"]
            comp["change"] = comp["score"] - hist[-2]["value"]
        if len(hist) >= 6:
            comp["chg_5d"] = comp["score"] - hist[-6]["value"]
    return comp
