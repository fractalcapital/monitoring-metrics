#!/usr/bin/env python3
"""Push-alert delivery: Feishu robot webhook + Bark mobile push.

This module owns ONLY the sending. Thresholds, dedup and fetch-failure
logic live in scripts/fetch_dashboard.py; this module never decides
whether to send, it only sends what it is asked to send.

Safety rules (do not break them):
  - Both addresses come ONLY from environment variables:
    FEISHU_WEBHOOK_URL and BARK_URL. They are never written into any
    file in this repo (the repo is public) and never printed or logged.
  - Feishu: interactive card (msg_type "interactive", card schema 2.0,
    per the custom-bot docs): a coloured header (template by severity),
    a markdown body, and a primary button that opens the dashboard.
    Plain text (msg_type "text") stays as a fallback in send_feishu.
    (Rich text / msg_type "post" was tried 2026-11-24 and looked too
    plain, so it was replaced by cards.)
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
import re
import sys
import urllib.parse
import urllib.request

__all__ = ["send_feishu", "send_feishu_card", "send_bark"]

# Always send direct, ignoring any system proxy (same reason as in
# scripts/fetch_dashboard.py): the alerts must go out from the machine's
# own IP, not through a VPS proxy the user may have configured for other
# traffic on a Mac. On the VPS and Actions runners there is no proxy
# configured, so this changes nothing there.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post_feishu(payload):
    """POST one JSON payload to the Feishu robot webhook.

    The URL comes only from the FEISHU_WEBHOOK_URL environment variable.
    It is never printed or logged. Returns True on success (HTTP 200 and
    code 0), False when skipped (no webhook configured) or on failure.
    """
    url = os.environ.get("FEISHU_WEBHOOK_URL")
    if not url:
        return False
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 # Feishu hangs (read timeout) on the default Python-urllib
                 # User-Agent from datacenter IPs — verified: curl UA
                 # returns 200 in <1s, Python-urllib UA times out.
                 "User-Agent": "curl/8.5.0"},
        method="POST")
    try:
        with _NO_PROXY_OPENER.open(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return resp.status == 200 and result.get("code") == 0
    except Exception as exc:
        print("WARNING: Feishu send failed: %s" % (exc,), file=sys.stderr)
        return False


def send_feishu(text):
    """POST one plain-text message to the Feishu robot webhook.

    Kept as the plain-text fallback; the live messages use
    send_feishu_card (interactive card) instead.
    """
    return _post_feishu({"msg_type": "text", "content": {"text": text}})


_CHANGE_RE = re.compile(r"（([+-][^，）]+)，([+-][^%）]+%)）")


def _color_change_spans(line):
    """Wrap a "(±abs，±pct%)" change span in a red/green font tag.

    Chinese market convention: up (positive) red, down (negative)
    green. Only the change span is coloured; the value and the status
    label stay plain. Lines without a change span are untouched.
    """
    def repl(m):
        color = "red" if m.group(1).startswith("+") else "green"
        return "（<font color=\"%s\">%s，%s</font>）" % (color, m.group(1),
                                                        m.group(2))
    return _CHANGE_RE.sub(repl, line)


def send_feishu_card(title, body, template="blue"):
    """POST one interactive card to the Feishu robot webhook.

    Feishu card format (msg_type "interactive", card schema 2.0, per
    the custom-bot docs): a coloured header (template by severity),
    a markdown body, and a primary button that opens the dashboard.
    `body` is plain text (lines separated by \\n); the first line is
    bolded, a trailing "查看仪表盘：<url>" line becomes the button
    instead of a raw URL, and each "(±abs，±pct%)" change span is
    coloured red for up / green for down (Chinese market convention).
    Custom-bot cards only support URL jumps from buttons, no server
    callbacks — the button just opens the link.
    Returns True on success, False when skipped (no webhook
    configured) or on failure.
    """
    lines = body.split("\n")
    dashboard_url = None
    if lines and lines[-1].startswith("查看仪表盘：") and "http" in lines[-1]:
        dashboard_url = lines[-1].split("：", 1)[1].strip()
        lines = lines[:-1]
    lines = [_color_change_spans(ln) for ln in lines]
    if lines:
        lines[0] = "**%s**" % lines[0]
    elements = [
        {"tag": "markdown", "content": "\n".join(lines),
         "text_align": "left", "text_size": "normal_v2"},
    ]
    if dashboard_url:
        elements.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开仪表盘"},
            "type": "primary",
            "width": "default",
            "size": "medium",
            "behaviors": [{"type": "open_url", "default_url": dashboard_url,
                           "pc_url": "", "ios_url": "", "android_url": ""}],
        })
    return _post_feishu({
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {"update_multi": True},
            "body": {"direction": "vertical",
                     "padding": "12px 12px 12px 12px",
                     "elements": elements},
            "header": {"title": {"tag": "plain_text", "content": title},
                       "template": template,
                       "padding": "12px 12px 12px 12px"},
        },
    })


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
        with _NO_PROXY_OPENER.open(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return 200 <= resp.status < 300 and result.get("code") == 200
    except Exception as exc:
        print("WARNING: Bark send failed: %s" % (exc,), file=sys.stderr)
        return False
