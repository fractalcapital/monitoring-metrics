"""Unit tests for scripts/fetch_dashboard.py (stdlib only, no network)."""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import fetch_dashboard as fd  # noqa: E402


def _make_payload(vix=20.0, vvix=100.0, skew=130.0, move=95.0, hy=350,
                  vix_pct=0.0, hy_chg=0, vix3m=22.0, date="2026-11-25",
                  hy_date="2026-11-24", hy_hist=None, intraday=False):
    if hy_hist is None:
        hy_hist = [{"date": "2026-11-%02d" % d, "value": hy - (6 - i)}
                   for i, d in enumerate((17, 18, 19, 20, 23, 24))]
        hy_hist[-1]["value"] = hy
    p = {
        "indicators": {
            "vix": {"value": vix, "date": date, "prev_close": round(vix / (1 + vix_pct / 100), 2) if vix_pct != -100 else vix,
                    "prev_date": "2026-11-24", "change_pct": vix_pct},
            "vvix": {"value": vvix, "date": date, "prev_close": vvix, "prev_date": "2026-11-24", "change_pct": 0.0},
            "skew": {"value": skew, "date": date, "prev_close": skew, "prev_date": "2026-11-24", "change_pct": 0.0},
            "move": {"value": move, "date": date, "prev_close": move, "prev_date": "2026-11-24", "change_pct": 0.0},
            "hy_oas": {"value_bp": hy, "value_pct": round(hy / 100, 2), "date": hy_date,
                       "prev_value_bp": hy - hy_chg, "prev_date": "2026-11-23",
                       "change_bp": hy_chg, "history": hy_hist},
        },
        "spx": {"value": 7180.0, "date": date, "prev_close": 7200.0,
                "prev_date": "2026-11-24", "change_pct": -0.28},
        "term_structure": {
            "vix1d": vix + 1.0, "vix9d": vix + 0.5, "vix": vix,
            "vix3m": vix3m, "vix6m": vix3m + 0.5, "date": date,
            "dates": {k: date for k in ("vix1d", "vix9d", "vix", "vix3m", "vix6m")},
            "date_mismatch": False,
        },
    }
    if intraday:
        p["intraday"] = True
    return p


class NormalizeDateTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(fd._normalize_date(20261125), "2026-11-25")
        self.assertEqual(fd._normalize_date("20261125"), "2026-11-25")
        self.assertEqual(fd._normalize_date("2026-11-25T16:00:00Z"), "2026-11-25")
        self.assertEqual(fd._normalize_date("1/5/2026"), "2026-01-05")
        self.assertEqual(fd._normalize_date("2026/11/5"), "2026-11-05")
        self.assertEqual(fd._normalize_date(1764028800), "2025-11-25")
        self.assertEqual(fd._normalize_date(1764028800000), "2025-11-25")
        self.assertIsNone(fd._normalize_date(""))
        self.assertIsNone(fd._normalize_date("bad-date"))


class SanitizeHLTests(unittest.TestCase):
    def test_drops_flat_and_bad_ticks(self):
        pairs = [
            {"date": "2026-11-20", "value": 35.89, "high": 40.03, "low": 26.94},
            # Known bad Futu tick: low 15.39 is < 26.62 * 0.70 (18.63)
            {"date": "2026-11-23", "value": 26.62, "high": 35.08, "low": 15.39},
            # Flat H == L (Futu SKEW style)
            {"date": "2026-11-24", "value": 139.16, "high": 139.16, "low": 139.16},
            # Valid wide intraday spike (e.g. Aug 2024 VIX 65 intraday, close 38)
            {"date": "2026-11-25", "value": 38.0, "high": 65.7, "low": 34.0},
        ]
        out = fd._sanitize_hl(pairs)
        self.assertEqual(out[0]["high"], 40.03)
        self.assertEqual(out[0]["low"], 26.94)
        self.assertNotIn("high", out[1])
        self.assertNotIn("low", out[1])
        self.assertEqual(out[1]["value"], 26.62)  # close preserved
        self.assertNotIn("high", out[2])
        self.assertEqual(out[3]["high"], 65.7)
        self.assertEqual(out[3]["low"], 34.0)


class CalendarAndFreshnessTests(unittest.TestCase):
    def test_nyse_holidays_and_early_closes(self):
        hol2026 = fd._nyse_holidays(2026)
        self.assertIn(dt.date(2026, 11, 26), hol2026)  # Thanksgiving
        self.assertIn(dt.date(2026, 4, 3), hol2026)    # Good Friday 2026
        self.assertIn(dt.date(2026, 6, 19), hol2026)   # Juneteenth
        self.assertIn(dt.date(2026, 7, 3), hol2026)    # July 4 Sat -> Fri Jul 3
        self.assertNotIn(dt.date(2021, 12, 31), fd._nyse_holidays(2021))  # NYD Sat rule
        self.assertIn(dt.date(2025, 1, 9), fd._nyse_holidays(2025))  # Carter mourning

        early2026 = fd._nyse_early_closes(2026)
        self.assertIn(dt.date(2026, 11, 27), early2026)  # Black Friday
        self.assertIn(dt.date(2026, 12, 24), early2026)  # Christmas Eve
        self.assertNotIn(dt.date(2026, 7, 3), early2026)  # July 3 is full holiday in 2026

    def test_expected_close_and_lag(self):
        # Thanksgiving 2026-11-26 17:00 ET (22:00 UTC) -> expected close is Wed 2026-11-25
        thx = dt.datetime(2026, 11, 26, 22, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(fd._expected_close_date(thx), "2026-11-25")
        # Black Friday 2026-11-27 13:20 ET (18:20 UTC) -> early close 13:00 + 15m passed
        bf = dt.datetime(2026, 11, 27, 18, 20, tzinfo=dt.timezone.utc)
        self.assertEqual(fd._expected_close_date(bf), "2026-11-27")
        # Lag from Wed 11-25 to Fri 11-27 is 1 trading day (Thu 11-26 is holiday)
        self.assertEqual(fd._trading_lag("2026-11-25", "2026-11-27"), 1)

    def test_annotate_freshness_grace_and_intraday(self):
        # Wed 2026-11-25 16:30 ET (21:30 UTC) -> inside post-close grace (<18:00 ET)
        p = _make_payload(date="2026-11-24", hy_date="2026-11-23")
        expected, issues = fd.annotate_freshness(
            p, dt.datetime(2026, 11, 25, 21, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(expected, "2026-11-25")
        self.assertEqual(issues, [])  # 1-day lag forgiven during grace window

        # Wed 2026-11-25 18:30 ET (23:30 UTC) -> grace over, 1-day lag alerts
        expected2, issues2 = fd.annotate_freshness(
            p, dt.datetime(2026, 11, 25, 23, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(expected2, "2026-11-25")
        self.assertTrue(any("VIX" in x for x in issues2))
        self.assertTrue(p["indicators"]["vix"]["lagging"])

        # Mid-session Wed 2026-11-25 11:00 ET (16:00 UTC) with today's date -> intraday
        p_live = _make_payload(date="2026-11-25", hy_date="2026-11-24")
        fd.annotate_freshness(
            p_live, dt.datetime(2026, 11, 25, 16, 0, tzinfo=dt.timezone.utc))
        self.assertTrue(p_live.get("intraday"))
        self.assertTrue(p_live["indicators"]["vix"].get("intraday"))
        self.assertNotIn("intraday", p_live["indicators"]["hy_oas"])


class ParsingAndUpsertTests(unittest.TestCase):
    def test_parse_csv_and_futu(self):
        csv_text = "DATE,OPEN,HIGH,LOW,CLOSE\n11/24/2026,27,32,26,26.36\n11/25/2026,28,35,27.5,28.29\n"
        out = fd._parse_csv_series(csv_text)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[-1]["date"], "2026-11-25")
        self.assertEqual(out[-1]["high"], 35.0)

        futu_text = "WARN banner line\n" + json.dumps({
            "kline_list": [
                {"date": 20261124, "close_price": 26.36, "high_price": 32.38, "low_price": 25.98},
                {"date": 20261125, "close_price": 28.29, "high_price": 34.95, "low_price": 27.49},
            ]
        })
        fout = fd.parse_futu_kline(futu_text)
        self.assertEqual([x["value"] for x in fout], [26.36, 28.29])

    def test_upsert_holiday_guard(self):
        hist = [
            {"date": "2026-11-24", "value": 98.84},
            {"date": "2026-11-25", "value": 97.54},
        ]
        # Same close and prev_close on Thanksgiving -> keeps 2026-11-25
        out = fd._upsert_history(hist, "2026-11-26", 97.54, 98.84)
        self.assertEqual(out[-1]["date"], "2026-11-25")
        self.assertEqual(len(out), 2)
        # New close on next day -> appends
        out2 = fd._upsert_history(hist, "2026-11-27", 99.10, 97.54)
        self.assertEqual(out2[-1]["date"], "2026-11-27")
        self.assertEqual(len(out2), 3)


class StatsAndAlertsTests(unittest.TestCase):
    def test_stats_and_composite(self):
        # 200 rising points -> latest is ~100th percentile
        pairs = [{"date": "2026-01-%02d" % ((i % 28) + 1), "value": 10.0 + i * 0.1}
                 for i in range(200)]
        st = fd.compute_stats(pairs)
        self.assertGreaterEqual(st["pct_rank"], 99)
        self.assertAlmostEqual(st["chg_5d"], 0.5, places=2)
        self.assertAlmostEqual(st["chg_20d"], 2.0, places=2)

        # Short series preserves prior percentile with stale flag
        short_st = fd.compute_stats(pairs[-10:], prev_stats={"pct_rank": 77, "window": 252})
        self.assertEqual(short_st["pct_rank"], 77)
        self.assertTrue(short_st["pct_rank_stale"])

        comp = fd.composite_score({
            "vix": {"pct_rank": 80}, "move": {"pct_rank": 40},
            "hy_oas": {"pct_rank": 60}, "vvix": {"pct_rank": 60},
            "skew": {"pct_rank": 100},  # ignored
        })
        self.assertEqual(comp["score"], 60)
        self.assertNotIn("skew", comp["components"])

    def test_composite_history_and_modular_imports(self):
        # Verify the modular package exports match the facade
        from monitoring import alerts, calendar, config, providers, series
        self.assertIs(fd.STATUS_BANDS, config.STATUS_BANDS)
        self.assertIs(fd._nyse_holidays, calendar._nyse_holidays)
        self.assertIs(fd.build_composite, series.build_composite)
        self.assertIs(fd.resolve_move, providers.resolve_move)
        self.assertIs(fd.evaluate_alerts, alerts.evaluate_alerts)

        # Build 260 trading-day synthetic series where all 4 components rise
        # monotonically, with HY OAS lagging 1 day behind VIX.
        base = dt.date(2025, 11, 1)
        dates = []
        d = base
        while len(dates) < 260:
            if d.weekday() < 5:
                dates.append(d.isoformat())
            d += dt.timedelta(days=1)

        pairs_map = {
            "vix": [{"date": ds, "value": 12.0 + i * 0.1} for i, ds in enumerate(dates)],
            "move": [{"date": ds, "value": 70.0 + i * 0.2} for i, ds in enumerate(dates)],
            "vvix": [{"date": ds, "value": 80.0 + i * 0.2} for i, ds in enumerate(dates)],
            # HY OAS lags by 1 trading day (missing the very last date)
            "hy_oas": [{"date": ds, "value": 300 + i} for i, ds in enumerate(dates[:-1])],
        }
        stats = {k: fd.compute_stats(pairs_map[k], is_bp=(k == "hy_oas")) for k in pairs_map}
        comp = fd.build_composite(stats, pairs_map, keep=30)
        self.assertIsNotNone(comp)
        self.assertEqual(len(comp["history"]), 30)
        self.assertEqual(comp["history"][-1]["date"], dates[-1])
        self.assertEqual(comp["history"][-1]["value"], comp["score"])
        self.assertGreaterEqual(comp["score"], 99)
        self.assertIn("change", comp)
        self.assertIn("chg_5d", comp)
        # Ensure no look-ahead bias: even 29 days earlier (monotonically rising),
        # trailing window rank is still near 100, NOT suppressed by future values.
        self.assertGreaterEqual(comp["history"][0]["value"], 99)

    def test_hysteresis_term_velocity_and_recovery(self):
        # VIX/VIX3M = 28.29 / 26.29 = 1.076 -> term warning
        p = _make_payload(vix=28.29, vix3m=26.29)
        ev = fd.evaluate_alerts(p)
        self.assertEqual(ev["term"]["level"], "warning")
        self.assertEqual(ev["vix"]["level"], "normal")

        # Hysteresis: VIX 35 -> warning; drops to 34 (> 35*0.95=33.25) -> stays warning; 33 -> normal
        p35 = _make_payload(vix=35.0)
        self.assertEqual(fd.evaluate_alerts(p35)["vix"]["level"], "warning")
        p34 = _make_payload(vix=34.0)
        self.assertEqual(fd.evaluate_alerts(p34, {"vix": "warning"})["vix"]["level"], "warning")
        p33 = _make_payload(vix=33.0)
        self.assertEqual(fd.evaluate_alerts(p33, {"vix": "warning"})["vix"]["level"], "normal")

        # Velocity alerts
        p_vel = _make_payload(vix=26.0, vix_pct=28.0, hy=410, hy_chg=26)
        ev_vel = fd.evaluate_alerts(p_vel)
        self.assertEqual(ev_vel["vix_jump"]["level"], "warning")
        self.assertEqual(ev_vel["hy_widen"]["level"], "warning")

        # End-to-end process_alerts: send on trigger, dedup within 24h, recovery on clear
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "alert_state.json"
            sent_feishu = []
            sent_bark = []
            with mock.patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": "http://fake", "BARK_URL": "http://fake"}), \
                 mock.patch.object(fd, "send_feishu_card", side_effect=lambda *a: (sent_feishu.append(a), True)[1]), \
                 mock.patch.object(fd, "send_bark", side_effect=lambda *a: (sent_bark.append(a), True)[1]):
                t0 = dt.datetime(2026, 11, 25, 22, 0, tzinfo=dt.timezone.utc)
                _, s1 = fd.process_alerts(_make_payload(vix=36.0, vix3m=40.0), state_path, t0)
                self.assertIn("vix", s1)
                # Same level 2 hours later -> deduped
                _, s2 = fd.process_alerts(_make_payload(vix=36.0, vix3m=40.0), state_path, t0 + dt.timedelta(hours=2))
                self.assertEqual(s2, [])
                # Still inside hysteresis band (34.0) -> still warning, no recovery
                _, s3 = fd.process_alerts(_make_payload(vix=34.0, vix3m=40.0), state_path, t0 + dt.timedelta(hours=3))
                self.assertEqual(s3, [])
                # Clears below band (32.0) -> recovery notice sent!
                _, s4 = fd.process_alerts(_make_payload(vix=32.0, vix3m=40.0), state_path, t0 + dt.timedelta(hours=4))
                self.assertIn("vix:resolved", s4)
                self.assertTrue(any("解除" in x[0] for x in sent_feishu))
                self.assertTrue(any(x[2] == "recovery" for x in sent_bark))
            # Atomic write leaves no .tmp files behind
            self.assertEqual([p.name for p in Path(td).iterdir()], ["alert_state.json"])


if __name__ == "__main__":
    unittest.main()
