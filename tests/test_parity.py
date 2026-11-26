"""Cross-language parity (Python vs dashboard/index.html JS) and HTML link checks."""
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import fetch_dashboard as fd  # noqa: E402


def _extract_js_snippet():
    html = (REPO / "dashboard" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"<script>([\s\S]*)</script>", html)
    assert m, "missing <script> in dashboard/index.html"
    js = m.group(1)
    # Strip DOM-touching immediate invocations so we can evaluate pure logic in Node.
    # Extract the status bands block + NYSE calendar + alert logic cleanly.
    start1 = js.index("const DEFAULT_STATUS_BANDS = {")
    end1 = js.index("/* 胶囊条简写标签")
    start2 = js.index("/* ---- NYSE 交易日历")
    end2 = js.index("/* ---- 美股开闭市与美东时钟")
    start3 = js.index("const DEFAULT_ALERT_THRESHOLDS = {")
    end3 = js.index("function fmtBJ(")
    return "\n".join([js[start1:end1], js[start2:end2], js[start3:end3]])


@unittest.skipUnless(shutil.which("node"), "node is required for JS parity tests")
class PythonJsParityTests(unittest.TestCase):
    def test_status_bands_calendar_and_alerts_match(self):
        snippet = _extract_js_snippet()
        status_cases = [
            ("vix", 15.0, 2.0, 0.3),
            ("vix", 15.0, 12.0, 1.8),    # calm + >=10% -> 异动
            ("vix", 26.62, -25.83, -9.27),  # not calm -> 警惕
            ("vix", 35.0, 5.0, 1.6),
            ("vix", 42.0, 20.0, 7.0),
            ("vvix", 65.0, 15.0, 8.5),   # <70 not in calm [70,120)
            ("vvix", 110.0, -14.0, -18.0),  # calm -> 异动
            ("vvix", 135.0, 2.0, 2.5),
            ("vvix", 145.0, 2.0, 3.0),
            ("skew", 115.0, 11.0, 11.5),  # calm -> 异动
            ("skew", 130.0, 1.0, 1.2),
            ("skew", 142.06, 2.08, 2.9),
            ("move", 55.0, 12.0, 6.0),
            ("move", 97.54, -1.32, -1.3),
            ("move", 100.0, 12.0, 10.7),  # calm -> 异动
            ("move", 125.0, 3.0, 3.6),    # 债市偏高
            ("move", 145.0, 5.0, 7.0),
            ("hy_oas", 280, 12.0, 30),    # <300 not calm -> 信用偏紧
            ("hy_oas", 372, 2.48, 9),     # calm, <25bp -> 信用常态
            ("hy_oas", 372, 7.2, 25),     # calm, >=25bp -> 异动
            ("hy_oas", 600, 1.0, 6),
            ("hy_oas", 850, 1.0, 8),
            ("hy_oas", 1050, 1.0, 10),
        ]
        years = [2022, 2025, 2026, 2027]
        lag_cases = [
            ("2026-11-25", "2026-11-25"),
            ("2026-11-25", "2026-11-26"),
            ("2026-11-25", "2026-11-27"),
            ("2026-11-20", "2026-11-25"),
        ]
        node_script = (
            snippet
            + "\nconst input = JSON.parse(require('fs').readFileSync(0,'utf8'));\n"
            + "const out = {\n"
            + "  status_bands: DEFAULT_STATUS_BANDS,\n"
            + "  statuses: input.status_cases.map(([k,v,p,a]) => statusFor(k,v,p,a)),\n"
            + "  holidays: Object.fromEntries(input.years.map(y => [y, [...nyseHolidays(y)].sort()])),\n"
            + "  early: Object.fromEntries(input.years.map(y => [y, [...nyseEarlyCloses(y)].sort()])),\n"
            + "  lags: input.lag_cases.map(([a,b]) => nyseTradingLag(a,b)),\n"
            + "};\n"
            + "console.log(JSON.stringify(out));\n"
        )
        proc = subprocess.run(
            ["node", "-e", node_script],
            input=json.dumps({"status_cases": status_cases, "years": years, "lag_cases": lag_cases}),
            capture_output=True, text=True, check=True,
        )
        js_out = json.loads(proc.stdout)

        # 1. Exact STATUS_BANDS config match
        self.assertEqual(js_out["status_bands"], fd.STATUS_BANDS)

        # 2. Exact statusFor parity on every test case
        for case, js_res in zip(status_cases, js_out["statuses"]):
            py_res = fd.status_label(*case)
            self.assertEqual(js_res, py_res, "mismatch on %r" % (case,))

        # 3. Exact NYSE holiday & early-close calendar parity
        for y in years:
            py_hol = sorted(d.isoformat() for d in fd._nyse_holidays(y))
            py_early = sorted(d.isoformat() for d in fd._nyse_early_closes(y))
            self.assertEqual(js_out["holidays"][str(y)], py_hol, "holidays %d" % y)
            self.assertEqual(js_out["early"][str(y)], py_early, "early %d" % y)

        # 4. Exact trading lag parity
        for (a, b), js_lag in zip(lag_cases, js_out["lags"]):
            self.assertEqual(js_lag, fd._trading_lag(a, b))


class HtmlLinkChecks(unittest.TestCase):
    def test_relative_links_resolve(self):
        pages = [REPO / "dashboard" / "index.html"] + sorted((REPO / "metrics").glob("*/index.html"))
        self.assertGreaterEqual(len(pages), 7)  # dashboard + 6 metric pages incl. skew
        href_re = re.compile(r'href="([^"]+)"')
        meta_link_re = re.compile(r'link:\s*"(\.\./metrics/[^"]+)"')
        for page in pages:
            text = page.read_text(encoding="utf-8")
            for href in href_re.findall(text):
                if "${" in href or href.startswith(("http://", "https://", "#", "data:", "mailto:")):
                    continue
                target = (page.parent / href.split("#")[0]).resolve()
                self.assertTrue(target.exists(), "%s links to missing %s" % (page, href))
            for link in meta_link_re.findall(text):
                target = (page.parent / link).resolve()
                self.assertTrue(target.exists(), "%s META link missing %s" % (page, link))


if __name__ == "__main__":
    unittest.main()
