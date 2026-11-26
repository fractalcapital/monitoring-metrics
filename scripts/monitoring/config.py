"""Shared constants, source URLs, status bands and alert thresholds."""
import datetime as dt

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

HISTORY_KEEP = 300   # ~1.2 trading years kept in memory for rolling 1y percentile & 30d composite curve
DISPLAY_KEEP = 30    # points written to data.json history (sparklines + cache)
STATS_WINDOW = 252   # trading days used for the 1y percentile rank
STATS_MIN_WINDOW = 120
# Intraday high/low sanity bounds, relative to min / max(close, prev close).
# Generous on the upside (VIX can triple intraday, e.g. 2024-08-05), strict
# on the downside (a VIX low 40% under both closes is a bad tick).
HL_LOW_FLOOR = 0.70
HL_HIGH_CAP = 3.00

KNOWN_SPECIAL_CLOSURES = {
    "2025-01-09",  # National Day of Mourning (President Carter)
}

COMPOSITE_WEIGHTS = {"vix": 1.0, "move": 1.0, "hy_oas": 1.0, "vvix": 0.5}

CREDIT_TIER_SERIES = (
    ("ig", "BAMLC0A0CM", "投资级 IG"),
    ("ccc", "BAMLH0A3HYC", "CCC 级"),
)

FRESH_ALLOWED_LAG = {"hy_oas": 2}
FRESH_ALERT_LAG = {"hy_oas": 3}
FRESHNESS_ALERT_AFTER_ET_MIN = 18 * 60
NODE_LABELS = {"vix": "VIX", "vvix": "VVIX", "skew": "SKEW", "move": "MOVE",
               "spx": "标普 500", "hy_oas": "HY OAS"}

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
