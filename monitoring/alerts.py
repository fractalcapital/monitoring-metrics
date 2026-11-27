"""Alert evaluation (hysteresis, term structure, velocity, recovery) and manual pushes."""
import datetime as dt
import json
import math
import os
from pathlib import Path

from .notify import send_bark, send_feishu_card

from .config import (
    ALERT_HISTORY_MAX,
    ALERT_THRESHOLDS,
    DASHBOARD_URL,
    DEFAULT_FETCH_NOTE,
    FETCH_ALERT_TEXT,
    FETCH_GROUPS,
    FETCH_SOURCES,
    HYSTERESIS_PCT,
    LEVEL_LABEL,
    LEVEL_SEVERITY,
    NODE_LABELS,
    RESEND_AFTER,
    STATUS_BANDS,
    _STATUS_SEV,
    _VERDICT_TITLES,
)
from .series import _atomic_write_json


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

    # Composite stress score (0-100): high-stress band (with hysteresis) + 5d surge.
    comp = payload.get("composite") or {}
    if comp.get("score") is not None:
        comp_score = int(comp["score"])
        comp_hist = comp.get("history") or []
        comp_date = (comp_hist[-1].get("date")
                     if comp_hist and isinstance(comp_hist[-1], dict) and comp_hist[-1].get("date")
                     else ind["vix"]["date"])
        spec = ALERT_THRESHOLDS["composite"]
        level = _level_with_hysteresis(
            comp_score, spec, prev_levels.get("composite", "normal"))
        c_parts = []
        if comp.get("change") is not None:
            c_parts.append("较前日 %+d" % int(comp["change"]))
        if comp.get("chg_5d") is not None:
            c_parts.append("5 日 %+d" % int(comp["chg_5d"]))
        comps = comp.get("components") or {}
        if comps:
            comp_names = (("vix", "VIX"), ("move", "MOVE"),
                          ("hy_oas", "HY"), ("vvix", "VVIX"))
            c_parts.append("分项 " + " / ".join(
                "%s %s%%" % (lbl, comps[k]) for k, lbl in comp_names if k in comps))
        out["composite"] = _ev(level, comp_score, comp_date, spec["warning"],
                               "；".join(c_parts) if c_parts else "-")

        spec_j = ALERT_THRESHOLDS["composite_jump"]
        chg_5d = comp.get("chg_5d")
        j_level = _level_for(chg_5d, spec_j) if chg_5d is not None else "normal"
        prev_5d = (comp_score - int(chg_5d)) if chg_5d is not None else None
        j_change = ("5 日 %+d 分（%s → %d）" % (int(chg_5d), prev_5d, comp_score)
                    if chg_5d is not None else "-")
        out["composite_jump"] = _ev(j_level, int(chg_5d) if chg_5d is not None else 0,
                                    comp_date, spec_j["warning"], j_change)

    if payload.get("intraday"):
        for key, ev in out.items():
            if key not in ("hy_oas", "hy_widen"):
                ev["intraday"] = True
    return out


def load_alert_history(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"updated_at_utc": None, "events": []}


def _append_alert_history(history_path, new_events, now_s):
    """Prepend new alert transition events to alert_history.json (capped at ALERT_HISTORY_MAX)."""
    if not new_events or not history_path:
        return
    doc = load_alert_history(history_path)
    existing = list(doc.get("events") or [])
    merged = list(reversed(new_events)) + existing
    _atomic_write_json(history_path, {
        "updated_at_utc": now_s,
        "events": merged[:ALERT_HISTORY_MAX],
    })


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
    elif key == "composite":
        body = ("当前情绪压力分：%s / 100（预警线 ≥%s，近一年分位加权）\n"
                "详情：%s\n日期：%s" % (ev["value"], ev["threshold"], ev["change"], ev["date"]))
    elif key == "composite_jump":
        body = ("情绪压力分 5 日急升：%+d 分（阈值 +%s 分）\n%s\n日期：%s"
                % (int(ev["value"]), ev["threshold"], ev["change"], ev["date"]))
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
    elif key == "composite":
        body = ("综合情绪压力分回落至 %s / 100（已脱离 %s 高压带）\n日期：%s"
                % (ev["value"], spec["warning"], ev["date"]))
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


def _write_state(state_path, state):
    _atomic_write_json(state_path, state)


def _channels_configured():
    return {
        "feishu": bool(os.environ.get("FEISHU_WEBHOOK_URL")),
        "bark": bool(os.environ.get("BARK_URL")),
    }


def _send_channel(ch, title, body, template, bark_level,
                  feishu_sender=None, bark_sender=None):
    if ch == "feishu":
        return (feishu_sender or send_feishu_card)(title, body, template)
    return (bark_sender or send_bark)(title, body, bark_level)


def _fetch_alert_send(key, label, detail, state, now_utc, source_key=None, feishu_sender=None, bark_sender=None):
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
        if _send_channel(ch, title, body, "orange", "warning", feishu_sender, bark_sender):
            channels[ch] = {"last_sent_at_utc": now_s, "last_sent_value": detail}
            sent.append(ch)
    if sent:
        entry["last_sent_at_utc"] = max(c["last_sent_at_utc"] for c in channels.values()
                                        if c.get("last_sent_at_utc"))
        entry["last_sent_value"] = detail
        entry["episode_sent"] = True
    alerts[key] = entry
    return sent


def record_fetch_failure_in_state(source_key, detail, state, now_utc, feishu_sender=None, bark_sender=None):
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
        feishu_sender=feishu_sender,
        bark_sender=bark_sender,
    )
    print("  fetch failure %s: %s (alert sent via: %s)"
          % (source_key, detail, sent or "none"))
    return sent


def record_fetch_success(source_key, state, now_utc, detail=None, feishu_sender=None, bark_sender=None):
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
                   if configured[ch] and _send_channel(ch, title, body, "green", "recovery", feishu_sender, bark_sender)]
            print("  fetch recovered %s (notice sent via: %s)" % (source_key, got or "none"))


def process_alerts(payload, state_path, now_utc, group_errors=None,
                   group_providers=None, feishu_sender=None, bark_sender=None,
                   history_path=None):
    """Evaluate, dedup and (maybe) send alerts on Feishu (card) + Bark."""
    group_errors = group_errors or {}
    group_providers = group_providers or {}
    state = load_alert_state(state_path)
    now_s = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    channels_configured = _channels_configured()
    resolved_history_path = (Path(history_path) if history_path is not None
                             else Path(state_path).with_name("alert_history.json"))
    prior_history_events = (load_alert_history(resolved_history_path).get("events") or [])
    new_history_events = []

    def _latest_history_for(k):
        for item in prior_history_events:
            if isinstance(item, dict) and item.get("key") == k:
                return item
        return None

    fetch_status = dict(state.get("fetch_status", {}) or {})
    fetch_status.pop("yahoo", None)
    state["fetch_status"] = fetch_status
    alerts = state.setdefault("alerts", {})
    alerts.pop("fetch_yahoo", None)
    for gkey in FETCH_GROUPS:
        if gkey in group_errors:
            record_fetch_failure_in_state(gkey, group_errors[gkey], state, now_utc, feishu_sender=feishu_sender, bark_sender=bark_sender)
        else:
            record_fetch_success(
                gkey, state, now_utc, detail=group_providers.get(gkey), feishu_sender=feishu_sender, bark_sender=bark_sender)

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
            got = []
            if old_level != "normal" and old.get("episode_sent") and not spec.get("event"):
                title, body = format_recovery_parts(key, ev)
                for ch in ("feishu", "bark"):
                    if not channels_configured[ch]:
                        continue
                    if _send_channel(ch, title, body, "green", "recovery", feishu_sender, bark_sender):
                        channels[ch] = {"last_sent_at_utc": now_s,
                                        "last_sent_value": ev["value"]}
                        got.append(ch)
                if got:
                    entry["last_sent_at_utc"] = now_s
                    sent.append(key + ":resolved")
            if old_level != "normal" and not spec.get("event"):
                new_history_events.append({
                    "ts_utc": now_s,
                    "key": key,
                    "label": spec["label"],
                    "event": "resolved",
                    "from_level": old_level,
                    "to_level": "normal",
                    "value": ev["value"],
                    "unit": spec.get("unit", ""),
                    "threshold": ev["threshold"],
                    "date": ev["date"],
                    "change": ev["change"],
                    "channels": got,
                })
            alerts[key] = entry
            continue

        title, body = format_alert_parts(key, ev)
        template = {"warning": "orange", "critical": "red"}.get(new_level, "blue")
        sent_chs = []
        for ch in ("feishu", "bark"):
            ch_sent_at = _parse_utc(channels[ch].get("last_sent_at_utc"))
            if not _channel_should_send(old_level, new_level, ch_sent_at, now_utc):
                continue
            if not channels_configured[ch]:
                print("  alert %s=%s (no %s configured, not sent)"
                      % (key, new_level, ch))
                continue
            if _send_channel(ch, title, body, template, new_level, feishu_sender, bark_sender):
                channels[ch] = {"last_sent_at_utc": now_s,
                                "last_sent_value": ev["value"]}
                sent_chs.append(ch)
        if sent_chs:
            entry["last_sent_at_utc"] = max(c["last_sent_at_utc"] for c in channels.values()
                                            if c.get("last_sent_at_utc"))
            entry["last_sent_value"] = ev["value"]
            entry["episode_sent"] = True
            sent.append(key)

        ev_kind = None
        if old_level == "normal":
            ev_kind = "triggered"
        elif LEVEL_SEVERITY[new_level] > LEVEL_SEVERITY[old_level]:
            ev_kind = "upgraded"
        elif spec.get("event"):
            prev_h = _latest_history_for(key)
            if not prev_h or prev_h.get("date") != ev["date"] or prev_h.get("to_level") != new_level:
                ev_kind = "triggered"
        if ev_kind:
            new_history_events.append({
                "ts_utc": now_s,
                "key": key,
                "label": spec["label"],
                "event": ev_kind,
                "from_level": old_level,
                "to_level": new_level,
                "value": ev["value"],
                "unit": spec.get("unit", ""),
                "threshold": ev["threshold"],
                "date": ev["date"],
                "change": ev["change"],
                "channels": sent_chs,
            })
        alerts[key] = entry

    new_state = {
        "updated_at_utc": now_s,
        "channels_configured": channels_configured,
        "fetch_status": state.get("fetch_status", {}),
        "alerts": alerts,
    }
    _write_state(state_path, new_state)
    _append_alert_history(resolved_history_path, new_history_events, now_s)
    return new_state, sent


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


def cross_market_attribution(payload):
    """Derive a structured cross-market attribution summary (equity vs bond vs credit)."""
    ind = payload["indicators"]
    hy = ind["hy_oas"]
    hy_pct = (hy["change_bp"] / hy["prev_value_bp"] * 100
              if hy.get("prev_value_bp") else None)
    st_vix = status_label("vix", ind["vix"]["value"], ind["vix"].get("change_pct"))
    st_move = status_label("move", ind["move"]["value"], ind["move"].get("change_pct"))
    st_hy = status_label("hy_oas", hy["value_bp"], hy_pct, hy.get("change_bp"))

    eq_hot = _STATUS_SEV[st_vix["cls"]] >= 1
    bond_hot = _STATUS_SEV[st_move["cls"]] >= 1
    credit_hot = _STATUS_SEV[st_hy["cls"]] >= 1

    def _worst(*classes):
        return max(classes, key=lambda c: _STATUS_SEV[c])

    if eq_hot and bond_hot and credit_hot:
        regime = "股债信用三端共振"
        tone = _worst(st_vix["cls"], st_move["cls"], st_hy["cls"])
        summary = "权益波动、美债波动与高收益信用利差同步抬升，系统性风险特征显著"
    elif eq_hot and bond_hot:
        regime = "股债双升 · 信用暂稳"
        tone = _worst(st_vix["cls"], st_move["cls"])
        summary = "股市与美债波动同步走高，但高收益信用利差仍在常态，偏利率冲击主导"
    elif eq_hot and credit_hot:
        regime = "股信联动 · 债市平稳"
        tone = _worst(st_vix["cls"], st_hy["cls"])
        summary = "权益波动与高收益信用利差同向升温，企业违约担忧抬头，美债波动暂稳"
    elif eq_hot:
        regime = "权益单边波动 · 债信未确认"
        tone = st_vix["cls"]
        summary = "压力主要集中在股市端，美债波动与信用利差仍处常态，暂未演化为系统性危机"
    elif bond_hot and credit_hot:
        regime = "债信同步升温 · 股市平静"
        tone = _worst(st_move["cls"], st_hy["cls"])
        summary = "美债波动与信用利差率先走高，股市尚未充分定价，需警惕风险滞后传导"
    elif bond_hot:
        regime = "债市单边波动 · 股信平稳"
        tone = st_move["cls"]
        summary = "美债收益率波动率先抬升，股市与企业信用暂稳，留意利率压力向权益传导"
    elif credit_hot:
        regime = "信用率先走阔 · 股债平静"
        tone = st_hy["cls"]
        summary = "高收益信用利差走阔或异动，股债波动率仍处常态，需警惕信用面隐蔽压力"
    else:
        regime = "全市场整体平静"
        tone = "b-green"
        summary = "股、债、信用三端均处常态区间，跨市场暂无系统性风险信号"

    notes = []
    ts = payload.get("term_structure") or {}
    if ts.get("vix") and ts.get("vix3m"):
        ratio = float(ts["vix"]) / float(ts["vix3m"])
        if ratio >= 1.0:
            ratio_disp = math.floor(ratio * 100.0 + 0.5) / 100.0
            notes.append("VIX 期限结构倒挂（VIX/VIX3M %.2f），近端恐慌集中" % ratio_disp)
    vvix_val = (ind.get("vvix") or {}).get("value")
    if vvix_val is not None and float(vvix_val) >= 140:
        notes.append("VVIX 达 %.2f，波动率本身剧烈摆动" % float(vvix_val))
    skew_val = (ind.get("skew") or {}).get("value")
    if not (eq_hot or bond_hot or credit_hot) and skew_val is not None and float(skew_val) >= 140:
        notes.append("SKEW %.1f 偏高属平静期机构逢低买尾部保险，非当下恐慌" % float(skew_val))

    if notes:
        summary = "%s；%s。" % (summary, "；".join(notes))
    else:
        summary += "。"

    return {
        "regime": regime,
        "tone": tone,
        "summary": summary,
        "pillars": {
            "equity": {"label": st_vix["label"], "cls": st_vix["cls"], "hot": eq_hot},
            "bond": {"label": st_move["label"], "cls": st_move["cls"], "hot": bond_hot},
            "credit": {"label": st_hy["label"], "cls": st_hy["cls"], "hot": credit_hot},
        },
    }


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
    attr = payload.get("attribution") or cross_market_attribution(payload)
    if attr and attr.get("regime"):
        lines.append("跨市场归因：%s（%s）" % (attr["regime"], attr["summary"].rstrip("。")))
    comp = payload.get("composite") or {}
    if comp.get("score") is not None:
        comp_extra = []
        if comp.get("change") is not None:
            comp_extra.append("较前日 %s" % _signed(comp["change"], 0))
        if comp.get("chg_5d") is not None:
            comp_extra.append("5日 %s" % _signed(comp["chg_5d"], 0))
        suffix = ("，" + "，".join(comp_extra)) if comp_extra else ""
        lines.append("情绪压力分：%s / 100（近一年分位加权%s）" % (comp["score"], suffix))
    if has_stale:
        lines.append("注意：部分数据源本次拉取失败，相应项保留上次成功数据")
    for k in ("vix", "vvix", "skew", "move"):
        extra = ""
        if k == "vix":
            vst = ind["vix"].get("stats") or {}
            if vst.get("rv20") is not None and vst.get("vrp") is not None:
                extra = " · RV20 %.1f / VRP %+.1f" % (float(vst["rv20"]), float(vst["vrp"]))
        lines.append("%s %s（%s，%s%%）· %s%s" % (
            ALERT_THRESHOLDS[k]["label"],
            ind[k]["value"],
            _signed(ind[k]["value"] - ind[k]["prev_close"]),
            _signed(ind[k]["change_pct"]), st[k]["label"], extra))
    lines.append("%s %sbp（%sbp，%s%%）· %s" % (
        ALERT_THRESHOLDS["hy_oas"]["label"],
        hy["value_bp"], _signed(hy["change_bp"], 0),
        _signed(hy_pct) if hy_pct is not None else "—", st["hy_oas"]["label"]))
    spx_st = spx.get("stats") or {}
    spx_extra_parts = []
    if spx_st.get("rsi_14") is not None:
        spx_extra_parts.append("RSI %.1f" % float(spx_st["rsi_14"]))
    if spx_st.get("vs_ma200_pct") is not None:
        spx_extra_parts.append("距MA200 %+.1f%%" % float(spx_st["vs_ma200_pct"]))
    spx_extra = (" · " + " · ".join(spx_extra_parts)) if spx_extra_parts else ""
    lines.append("标普 500 %s（%s，%s%%）%s" % (
        format(spx["value"], ","),
        _signed(spx_abs) if spx_abs is not None else "—",
        _signed(spx["change_pct"]),
        spx_extra))
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
                     channels=("feishu", "bark"), feishu_sender=None, bark_sender=None):
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
        results["feishu"] = (feishu_sender or send_feishu_card)(title, body, template)
    if "bark" in channels:
        results["bark"] = (bark_sender or send_bark)(title, body, "warning")
    print("  manual push %s (%s): %s"
          % (mode, ",".join(channels),
             " ".join("%s=%s" % (k, v) for k, v in results.items())))
    return bool(any(results.values()))


