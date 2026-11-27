#!/usr/bin/env python3
"""CLI entry point for fetching market risk closes and updating dashboard/data.json.

All domain logic lives in `scripts/monitoring/`:
  - `config.py`    constants, source URLs, status bands and alert thresholds
  - `calendar.py`  date normalization, NYSE holiday / early-close calendar, freshness
  - `series.py`    H/L sanity checks, upsert, 1y percentile stats, composite stress curve, atomic JSON
  - `providers.py` Futu CLI, CBOE CDN, FRED, Stooq, Yahoo curl_cffi, TradingView, CSV
  - `alerts.py`    hysteresis, term-structure / velocity / recovery alerts, manual pushes

This module stays as the stable CLI and public import facade so cron,
`push_data.sh`, `api_server.py`, `freshness-watchdog.yml` and tests keep working.
"""
import concurrent.futures
import datetime as dt
import json
import os
import sys
import threading
from pathlib import Path

# Ensure repo root (for `alert`) and scripts/ (for `monitoring`) are importable
# regardless of working directory.
_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
for _p in (str(_REPO_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from alert.notify import send_bark, send_feishu_card  # noqa: E402
from monitoring import alerts as _alerts  # noqa: E402
from monitoring.alerts import (  # noqa: E402, F401
    _channel_should_send,
    _level_for,
    _level_with_hysteresis,
    build_status_message,
    cross_market_attribution,
    evaluate_alerts,
    format_alert_parts,
    format_recovery_parts,
    load_alert_history,
    load_alert_state,
    record_fetch_failure_in_state,
    record_fetch_success,
    status_label,
)
from monitoring.calendar import (  # noqa: E402, F401
    _close_minutes,
    _easter_sunday,
    _expected_close_date,
    _extra_closures,
    _is_nyse_holiday,
    _is_trading_day,
    _latest_us_weekday_iso,
    _market_session_now,
    _normalize_date,
    _nyse_early_closes,
    _nyse_holidays,
    _prev_trading_day,
    _trading_lag,
    _us_eastern_now,
    annotate_freshness,
)
from monitoring.config import (  # noqa: E402, F401
    ALERT_HISTORY_MAX,
    ALERT_THRESHOLDS,
    COMPOSITE_WEIGHTS,
    CREDIT_TIER_SERIES,
    DASHBOARD_URL,
    DISPLAY_KEEP,
    FETCH_GROUPS,
    FETCH_SOURCES,
    FRESH_ALERT_LAG,
    FRESH_ALLOWED_LAG,
    HISTORY_FILE_KEEP,
    HISTORY_KEEP,
    HL_HIGH_CAP,
    HL_LOW_FLOOR,
    HYSTERESIS_PCT,
    LEVEL_LABEL,
    LEVEL_SEVERITY,
    NODE_LABELS,
    RESEND_AFTER,
    STATS_MIN_WINDOW,
    STATS_WINDOW,
    STATUS_BANDS,
)
from monitoring.providers import (  # noqa: E402, F401
    _futu_bin,
    _futu_symbols,
    _parse_csv_series,
    _parse_futu_quote_json,
    _parse_tradingview_move,
    _parse_yahoo_chart_json,
    _try_providers,
    fetch_cboe,
    fetch_fred_series,
    fetch_futu,
    fetch_stooq_spx,
    fetch_trading_tools_move,
    fetch_tradingview_move,
    fetch_url,
    fetch_yahoo,
    parse_futu_kline,
    resolve_credit_tiers,
    resolve_equities_and_term,
    resolve_hy_oas,
    resolve_move,
)
from monitoring.series import (  # noqa: E402, F401
    _align_composite_history,
    _atomic_write_json,
    _describe_sources,
    _finalize_pairs,
    _prev_pairs_for,
    _round_num,
    _sanitize_hl,
    _upsert_history,
    _valid_hl,
    build_composite,
    build_history_payload,
    build_term_history_snapshots,
    composite_score,
    compute_composite_history,
    compute_ma,
    compute_rsi,
    compute_rv20,
    compute_stats,
    enrich_derived_stats,
    indicator,
)


def process_alerts(payload, state_path, now_utc, group_errors=None,
                   group_providers=None, history_path=None):
    """Evaluate, dedup and send alerts (forwards this module's senders for tests)."""
    return _alerts.process_alerts(
        payload,
        state_path,
        now_utc,
        group_errors=group_errors,
        group_providers=group_providers,
        feishu_sender=send_feishu_card,
        bark_sender=send_bark,
        history_path=history_path,
    )


def send_manual_push(mode, payload=None, stale=False, channels=("feishu", "bark")):
    return _alerts.send_manual_push(
        mode,
        payload=payload,
        stale=stale,
        channels=channels,
        feishu_sender=send_feishu_card,
        bark_sender=send_bark,
    )


def main():
    out_dir = Path(os.environ.get("MM_OUT_DIR") or (_REPO_ROOT / "dashboard"))
    out_path = out_dir / "data.json"
    history_path = out_dir / "history.json"
    state_path = out_dir / "alert_state.json"
    alert_history_path = out_dir / "alert_history.json"

    mode = os.environ.get("MANUAL_PUSH", "").strip().lower()
    channels = tuple(
        c.strip().lower()
        for c in os.environ.get("MANUAL_CHANNELS", "feishu,bark").split(",")
        if c.strip().lower() in ("feishu", "bark")) or ("feishu", "bark")
    if mode not in ("", "none", "test", "status", "status-cached"):
        print("WARNING unknown MANUAL_PUSH=%r, ignoring" % (mode,), file=sys.stderr)
        mode = ""
    if mode == "test":
        send_manual_push("test", channels=channels)
        return
    if mode == "status-cached":
        if not out_path.exists():
            print("ERROR status-cached: no data.json to push from", file=sys.stderr)
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

    # 1-3. Run independent market data groups concurrently:
    #      equities/term structure, MOVE, HY OAS, and optional credit tiers.
    ref_date_ready = threading.Event()
    ref_date_box = [None]
    want_tiers = os.environ.get("CREDIT_TIERS", "1").strip().lower() not in (
        "0", "false", "no", "off")

    def _run_equities():
        try:
            res = resolve_equities_and_term(prev_payload)
            p_map = res[0]
            if "vix" in p_map and p_map["vix"]:
                ref_date_box[0] = p_map["vix"][-1]["date"]
            return res
        finally:
            ref_date_ready.set()

    def _lazy_ref_date():
        # Only waits if MOVE actually falls back to TradingView.
        ref_date_ready.wait(timeout=45)
        return ref_date_box[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        f_eq = pool.submit(_run_equities)
        f_move = pool.submit(resolve_move, prev_payload, reference_date=_lazy_ref_date)
        f_hy = pool.submit(resolve_hy_oas, prev_payload)
        f_tiers = pool.submit(resolve_credit_tiers, prev_payload) if want_tiers else None

        try:
            pairs, sources_map, stale_eq, eq_errors = f_eq.result()
        except Exception as exc:
            print("FATAL equities fetch failed with no fallback: %s" % exc, file=sys.stderr)
            sys.exit(1)

        try:
            move_pairs, move_src, move_stale, move_err = f_move.result()
        except Exception as exc:
            print("FATAL MOVE fetch failed with no fallback: %s" % exc, file=sys.stderr)
            sys.exit(1)

        fred_rows, hy_src, hy_stale, hy_err = f_hy.result()
        credit_tiers = f_tiers.result() if f_tiers is not None else {}

    if eq_errors:
        group_errors["equities"] = "; ".join(eq_errors)[:300]
    else:
        eq_used = sorted({
            sources_map[k]
            for k in ("vix", "vvix", "skew", "vix1d", "vix9d", "vix3m", "vix6m", "spx")
            if k in sources_map
        })
        group_providers["equities"] = "+".join(eq_used)

    pairs["move"] = move_pairs
    sources_map["move"] = move_src
    if move_stale and move_err:
        group_errors["move"] = move_err[:300]
    else:
        group_providers["move"] = move_src

    pairs["hy_oas"] = fred_rows
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
        k: pairs[k][-1]["date"]
        for k in ("vix1d", "vix9d", "vix", "vix3m", "vix6m")
    }
    term_stale_tenors = sorted(
        k for k in ("vix1d", "vix9d", "vix", "vix3m", "vix6m") if k in stale_eq
    )
    prev_term_compare = (
        (prev_payload.get("term_structure") or {}).get("compare")
        if isinstance(prev_payload, dict) else None
    )
    term_compare = build_term_history_snapshots(
        pairs, offsets=(5, 20), prev_compare=prev_term_compare)
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
    if term_compare:
        term_structure_payload["compare"] = term_compare
    if term_stale_tenors:
        term_structure_payload["stale"] = True
        term_structure_payload["stale_tenors"] = term_stale_tenors

    now_utc = dt.datetime.now(dt.timezone.utc)
    beijing = now_utc.astimezone(dt.timezone(dt.timedelta(hours=8)))

    # 4. Stats (1y percentile, 5d / 20d change) + rolling composite stress curve
    def _prev_stats(k):
        try:
            node = prev_payload["spx"] if k == "spx" else prev_payload["indicators"][k]
            return node.get("stats")
        except (KeyError, TypeError, AttributeError):
            return None

    stats = {k: compute_stats(pairs[k], _prev_stats(k))
             for k in ("vix", "vvix", "skew", "move", "spx")}
    stats["hy_oas"] = compute_stats(fred_rows, _prev_stats("hy_oas"), is_bp=True)
    enrich_derived_stats(stats, pairs, prev_payload=prev_payload)

    prev_comp = prev_payload.get("composite") if isinstance(prev_payload, dict) else None
    comp_hist_full = compute_composite_history(
        pairs, prev_composite=prev_comp, keep=HISTORY_FILE_KEEP)
    comp = build_composite(
        stats, pairs, prev_composite=prev_comp,
        keep=DISPLAY_KEEP, precomputed_history=comp_hist_full)
    if comp is not None and pairs.get("vix"):
        comp_hist_full = _align_composite_history(
            comp_hist_full, comp["score"], pairs["vix"][-1]["date"], HISTORY_FILE_KEEP)

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
        "composite": comp,
        "credit_tiers": credit_tiers,
        "config": {
            "status_bands": STATUS_BANDS,
            "alert_thresholds": ALERT_THRESHOLDS,
            "fresh_allowed_lag": FRESH_ALLOWED_LAG,
        },
    }

    # 6. Cross-market attribution + freshness: data dates vs the latest NYSE session
    payload["attribution"] = cross_market_attribution(payload)
    expected, fresh_issues = annotate_freshness(payload, now_utc, set(stale_indicators))
    if fresh_issues:
        group_errors["freshness"] = ("预期收盘日 %s：%s"
                                     % (expected, "；".join(fresh_issues)))[:300]
    else:
        group_providers["freshness"] = "ok (expected %s)" % expected

    _atomic_write_json(out_path, payload)
    print("Wrote %s" % (out_path,))
    history_payload = build_history_payload(
        pairs,
        composite_history=comp_hist_full,
        updated_at_utc=payload["updated_at_utc"],
        keep=HISTORY_FILE_KEEP,
    )
    _atomic_write_json(history_path, history_payload)
    print("Wrote %s (%d series)" % (history_path, len(history_payload["series"])))
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
    spx_st = payload["spx"].get("stats") or {}
    vix_st = payload["indicators"]["vix"].get("stats") or {}
    print("  spx: %s on %s [%s] (RSI14=%s, MA200=%s/%s%%, RV20=%s, VRP=%s)"
          % (payload["spx"]["value"], payload["spx"]["date"], sources_map.get("spx"),
             spx_st.get("rsi_14"), spx_st.get("ma200"), spx_st.get("vs_ma200_pct"),
             spx_st.get("rv20"), vix_st.get("vrp")))
    print("  term: %s [%s]" % (payload["term_structure"], sources_map.get("vix1d")))
    print("  expected close: %s; freshness issues: %s"
          % (expected, fresh_issues or "none"))
    if comp:
        print("  composite: score=%s (chg=%s, 5d=%s, pts=%d, components=%s)"
              % (comp.get("score"), comp.get("change"), comp.get("chg_5d"),
                 len(comp.get("history") or []), comp.get("components")))
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
        history_path=alert_history_path,
    )
    print("Wrote %s" % (state_path,))
    print("  channels configured: feishu=%s bark=%s"
          % (new_state["channels_configured"]["feishu"],
             new_state["channels_configured"]["bark"]))
    for key, entry in new_state["alerts"].items():
        print("  alert %s: %s (value %s, last sent %s)"
              % (key, entry["level"], entry["value"], entry["last_sent_at_utc"]))
    print("  alerts sent this run: %s" % (sent if sent else "none"))
    if mode == "status":
        any_stale = bool(stale_indicators or hy_stale)
        send_manual_push("status", payload, stale=any_stale, channels=channels)


if __name__ == "__main__":
    main()
