#!/usr/bin/env python3
"""Push-alert delivery: Feishu robot webhook + Bark mobile push.

This module owns ONLY the sending. Thresholds, dedup and fetch-failure
logic live in scripts/fetch_dashboard.py; this module never decides
whether to send, it only sends what it is asked to send.

Safety rules (do not break them):
  - Both addresses come ONLY from environment variables:
    FEISHU_WEBHOOK_URL and BARK_URL. They are never written into any
    file in this repo (the repo is public) and never printed or logged.
  - Feishu: POST JSON {"msg_type": "text", "content": {"text": ...}}.
  - Bark: GET {base}/{quote(title)}/{quote(body)}
    ?group=monitoring-metrics&level=critical|active, where base is the
    BARK_URL env var (https://<bark server>/<key>, no title/body).
  - Both requests MUST carry User-Agent: curl/8.5.0. Verified
    2026-11-24: the default Python-urllib User-Agent makes Feishu hang
    (read timeout) and makes Bark's Cloudflare return 403/1010, so
    real alerts sent from GitHub Actions would fail silently.
  - Both functions return True on success, False when skipped (no env
    var) or on failure, and never raise.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

__all__ = ["send_feishu", "send_bark"]


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
        url, data=body,
        headers={"Content-Type": "application/json",
                 # Feishu hangs (read timeout) on the default Python-urllib
                 # User-Agent from datacenter IPs — verified: curl UA
                 # returns 200 in <1s, Python-urllib UA times out.
                 "User-Agent": "curl/8.5.0"},
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
    # Bark sits behind Cloudflare, which 403s (error 1010) the default
    # Python-urllib User-Agent — verified: curl UA returns code 200.
    req = urllib.request.Request(
        url, method="GET", headers={"User-Agent": "curl/8.5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return 200 <= resp.status < 300 and result.get("code") == 200
    except Exception as exc:
        print("WARNING: Bark send failed: %s" % (exc,), file=sys.stderr)
        return False
