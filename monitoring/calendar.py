"""Date normalization, NYSE trading calendar (holidays + early closes) and freshness."""
import datetime as dt
import os
import re

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from .config import (
    FRESH_ALERT_LAG,
    FRESH_ALLOWED_LAG,
    FRESHNESS_ALERT_AFTER_ET_MIN,
    KNOWN_SPECIAL_CLOSURES,
    NODE_LABELS,
)


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


# Ad-hoc full-day closures live in config.KNOWN_SPECIAL_CLOSURES (imported above).


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


