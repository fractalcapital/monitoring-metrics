# 报警推送服务（alert/）

这个目录只干一件事：**把报警消息推出去**。飞书机器人 webhook 和 Bark 手机推送，双通道并行。

## 里面有什么

- `notify.py` — 发送函数：`send_feishu_post(title, body)`（飞书富文本，`msg_type: "post"`，带标题和可点击的仪表盘链接）、`send_feishu(text)`（纯文本备用）、`send_bark(title, body, level)`。只负责发送，不决定发不发。
- `__init__.py` — 从 `.notify` 导出上面两个函数。

## 谁决定发不发

`scripts/fetch_dashboard.py`。触发线在它顶部的 `ALERT_THRESHOLDS` 里：

| 指标 | 预警 | 严重 |
| --- | --- | --- |
| VIX | ≥ 35 | ≥ 40 |
| VVIX | ≥ 140 | — |
| SKEW | ≥ 150 | — |
| MOVE | ≥ 140 | — |
| HY OAS | ≥ 500bp | ≥ 1000bp |
| 综合预警 | VIX ≥ 30 且 VVIX ≥ 140 或 MOVE ≥ 140 | — |

## 去重规则

- 状态记录在 `dashboard/alert_state.json` 里（每项：当前级别、当前值、日期、上次推送时间）。
- 只在**新触发**、**级别升级**（正常→预警、预警→严重）时推送；同一级别 24 小时内不重复推；同一级别超过 24 小时未解除，会再推一次。
- 级别回落到正常时不推送，只更新状态文件。
- 同一个报警事件，飞书和 Bark 同时发、内容一致；其中一个通道失败只记日志，另一个照常发。

## 数据拉取失败也报警

Yahoo 拉不到（例如 429 限流）或 FRED 拉不到时，同样在两个通道上推一条“数据获取失败”报警，写明是哪个数据源、失败原因、时间，并说明仪表盘当前显示的是上一次成功拉取的数据（带日期）。同一个数据源的失败报警 24 小时内只推一次，恢复后状态回到正常，下一次再失败会重新报警。仪表盘报警区下方有一行“数据拉取”状态，Yahoo / FRED 各自正常或失败。

## 怎么接（只做这一步，不要把地址写进任何文件）

推送地址只从环境变量读，工作流把它从 GitHub Secret 传给脚本；页面（`dashboard/index.html`）里没有任何推送相关内容。

1. 飞书：在群里添加自定义机器人，拿到 webhook 地址。
2. Bark：在自己的 Bark 服务器（或 Bark App）里拿到推送地址。需要的只是 base 部分：`https://<你的 Bark 服务器>/<key>`，**不含标题和正文**。
3. 到 GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret，建两个：
   - Name：`FEISHU_WEBHOOK_URL`，值填飞书机器人 webhook 地址。
   - Name：`BARK_URL`，值填 Bark 推送地址的 base 部分（服务器 + key）。
4. Bark 请求格式：`{base}/{标题}/{正文}?group=monitoring-metrics&level=critical|active`，标题和正文都经过 URL 编码；严重用 `critical`，预警用 `active`。
5. 仪表盘的报警区只显示两个通道的“已配置 / 未配置”，绝不显示地址本身。

**不要把飞书 webhook 地址或 Bark 服务器地址 / key 写进代码、注释、README、workflow 或任何测试文件**——仓库是公开的，写进去任何人都能往群里或手机发消息。

## 一个已验证的坑：User-Agent

两个请求都必须带 `User-Agent: curl/8.5.0`。2026-11-24 实测：Python 默认的 User-Agent 会让飞书直接超时、让 Bark 的 Cloudflare 返回 403/1010，真实报警会悄悄失败；换成 curl 的 UA 后两个通道都立刻通了。不要改掉它。

## 本地测试

不要在本地用真 webhook / 真 Bark 地址发测试消息，除非用户明确说要发。验证方法：用本机监听端口做假端点，把 `FEISHU_WEBHOOK_URL` / `BARK_URL` 指向它，调 `send_feishu` / `send_bark`，确认假端点收到且内容正确；不设这两个环境变量时两个函数都返回 False，不发任何请求。
