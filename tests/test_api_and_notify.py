"""Unit tests for api/api_server.py and alert/notify.py (local mock server only)."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "api"))

import api_server  # noqa: E402
from alert import notify  # noqa: E402


class ApiServerTests(unittest.TestCase):
    def setUp(self):
        api_server._hits.clear()
        api_server._global_hits.clear()

    def test_client_ip_prefers_cf_connecting_ip(self):
        # Spoofed X-Forwarded-For must be ignored when CF-Connecting-IP is present
        headers = {
            "X-Forwarded-For": "1.2.3.4, 9.9.9.9",
            "CF-Connecting-IP": "203.0.113.7",
        }
        self.assertEqual(api_server._client_ip(headers, "127.0.0.1"), "203.0.113.7")
        # Without CF-Connecting-IP, ignore spoofable X-Forwarded-For and use peer IP
        headers_only_xff = {"X-Forwarded-For": "1.2.3.4"}
        self.assertEqual(api_server._client_ip(headers_only_xff, "127.0.0.1"), "127.0.0.1")

    def test_rate_limit_per_scope(self):
        self.assertIsNone(api_server._rate_limited("/api/push", "10.0.0.1", "feishu"))
        # Same scope within 60s -> blocked
        self.assertIsNotNone(api_server._rate_limited("/api/push", "10.0.0.1", "feishu"))
        # Different channel scope -> allowed
        self.assertIsNone(api_server._rate_limited("/api/push", "10.0.0.1", "bark"))

    def test_run_timeout_kills_process_group(self):
        code, out, err = api_server._run(
            ["bash", "-c", "sleep 30 & wait"], timeout=0.4)
        self.assertIsNone(code)
        self.assertIn("timed out", err)

    def test_parse_push_results(self):
        out = "  manual push status (feishu,bark): feishu=True bark=False\n"
        self.assertEqual(api_server._parse_push_results(out),
                         {"feishu": True, "bark": False})


class _FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class NotifyTests(unittest.TestCase):
    def test_unset_env_returns_false(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(notify.send_feishu_card("t", "b"))
            self.assertFalse(notify.send_bark("t", "b", "warning"))

    def test_feishu_card_and_bark_levels(self):
        captured = []

        def fake_open(req, timeout=None):
            method = req.get_method()
            ua = req.get_header("User-agent")
            body = json.loads(req.data.decode("utf-8")) if req.data else None
            captured.append((method, req.full_url, ua, body))
            if method == "POST":
                return _FakeResp(200, {"code": 0})
            return _FakeResp(200, {"code": 200})

        with mock.patch.dict(os.environ, {
            "FEISHU_WEBHOOK_URL": "https://example.invalid/feishu",
            "BARK_URL": "https://example.invalid/barkkey",
        }), mock.patch.object(notify._NO_PROXY_OPENER, "open", side_effect=fake_open):
            ok_f = notify.send_feishu_card(
                "【测试】",
                "综合判断：警惕\nVIX 28.29（+1.93，+7.32%）· 警惕\n查看仪表盘：https://mon.infidea.app/dashboard/",
                "orange",
            )
            self.assertTrue(ok_f)
            ok_b1 = notify.send_bark("标题", "正文", "critical")
            ok_b2 = notify.send_bark("解除", "已恢复", "recovery")
            self.assertTrue(ok_b1 and ok_b2)

        self.assertEqual(len(captured), 3)
        method, url, ua, body = captured[0]
        self.assertEqual((method, url, ua), ("POST", "https://example.invalid/feishu", "curl/8.5.0"))
        self.assertEqual(body["msg_type"], "interactive")
        self.assertEqual(body["card"]["schema"], "2.0")
        md = body["card"]["body"]["elements"][0]["content"]
        self.assertIn('<font color="red">+1.93，+7.32%</font>', md)
        self.assertEqual(body["card"]["body"]["elements"][1]["tag"], "button")

        self.assertEqual(captured[1][2], "curl/8.5.0")
        self.assertIn("level=critical", captured[1][1])
        self.assertIn("level=passive", captured[2][1])


if __name__ == "__main__":
    unittest.main()
