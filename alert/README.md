# 报警推送服务（alert/）

这个目录只负责**把报警消息推出去**：飞书机器人卡片（schema 2.0）与 Bark 手机推送，双通道并行。

## 文件说明

- `notify.py` — `send_feishu_card(title, body, template)`、`send_feishu(text)`（纯文本备用）、`send_bark(title, body, level)`。只负责发送，不决定何时触发。
- `__init__.py` — 导出发送函数。

## 触发线与去重（位于 `scripts/fetch_dashboard.py`）

| 报警项 | 预警 | 严重 |
| --- | --- | --- |
| VIX | ≥ 35 | ≥ 40 |
| VVIX | ≥ 140 | — |
| SKEW | ≥ 150 | — |
| MOVE | ≥ 140 | — |
| HY OAS | ≥ 500bp | ≥ 1000bp |
| VIX 期限结构倒挂 | VIX / VIX3M ≥ 1.0 | ≥ 1.1 |
| 股债综合预警 | VIX ≥ 30 且 (VVIX ≥ 140 或 MOVE ≥ 140) | — |
| VIX 单日急升 | 单日 ≥ +25% | 单日 ≥ +50% |
| HY OAS 急速走阔 | 单日 ≥ +25bp 或 5 日 ≥ +50bp | — |

- 状态原子写入 `MM_OUT_DIR/alert_state.json`（生产 VPS 为 `/var/lib/monitoring/data/alert_state.json`，本地默认 `dashboard/alert_state.json`）。
- **防刷屏与回滞**：仅在新触发、级别升级、或同级持续超过 24 小时时推送；水平类报警带 5% 回滞带（期限倒挂 0.02），避免读数在阈值附近来回抖动重复报警。
- **恢复解除通知**：曾发过推送的水平类报警（及数据源故障/日期落后）恢复正常时，会发一条绿色飞书卡片与 Bark `passive` 恢复通知。
- **运维报警**：数据源拉取失败、收盘日期落后预期交易日（`fetch_freshness`），以及 GitHub Actions 外部新鲜度巡检（`freshness-watchdog.yml`，同时覆盖 VPS API 可用性与数据新鲜度）都会通过同一通道通知。

## 密钥管理

推送地址**只从环境变量读取**（VPS `/opt/monitoring-metrics/.env` 及 GitHub Secrets 供外部新鲜度巡检使用），绝不写入任何代码、文档或前端页面：

- `FEISHU_WEBHOOK_URL`：飞书自定义机器人 webhook 地址。
- `BARK_URL`：Bark 推送 base 地址（`https://<你的 Bark 服务器>/<key>`，不含标题和正文）。Bark 请求格式为 `{base}/{标题}/{正文}?group=monitoring-metrics&level=critical|active|passive`。

## 一个已验证的坑：User-Agent

两个请求都必须带 `User-Agent: curl/8.5.0`。Python 默认 UA 会导致飞书超时、Bark 的 Cloudflare 返回 `403/1010`，请勿修改。

## 本地测试

不要在本地用真 webhook / 真 Bark 地址发测试消息。可直接运行单元测试（自动启停本机回环 mock 服务验证 payload 与 UA）：

```bash
python3 -m unittest tests/test_api_and_notify.py -v
```
