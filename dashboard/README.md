# 实时监控仪表盘

页面：[index.html](index.html) —— VIX / VVIX / SKEW / MOVE / HY OAS 五项指标 + 标普 500 + VIX 期限结构，一页看完。

## 它和各指标页面的区别

各指标页面（`metrics/vix/`、`metrics/vvix/`、`metrics/move/`、`metrics/hy-oas/`、`metrics/vix-term-structure/`）是科普 + 历史长图，读数是固定快照。
本页是**监控用**：每天自动更新到最新收盘，带状态徽章、涨跌幅和近 30 个交易日迷你走势。

## 数据来源与更新节奏

| 指标 | 来源 | 频率 |
| --- | --- | --- |
| VIX / VVIX / SKEW / MOVE | Yahoo Finance v8 chart API（`^VIX`、`^VVIX`、`^SKEW`、`^MOVE`），约 15 分钟延迟 | 每个交易日收盘价 |
| VIX 期限结构（VIX1D / VIX9D / VIX / VIX3M / VIX6M） | Yahoo Finance（`^VIX1D`、`^VIX9D`、`^VIX3M`、`^VIX6M`） | 同上 |
| 标普 500 | Yahoo Finance（`^GSPC`） | 同上 |
| HY OAS | FRED `BAMLH0A0HYM2`（ICE BofA 美国高收益利差） | **每日一次**，盘中不会变 |

- GitHub Actions 每天收盘后运行两次（UTC 21:30 和 23:30，即北京时间次日 05:30 和 07:30），运行 `scripts/fetch_dashboard.py`，拉取后写入 `dashboard/data.json`，有变化才提交。
- 页面每 60 秒同源拉取一次 `./data.json` 刷新显示。
- “实时”指**每天自动更新到最新收盘**，不是盘中逐笔跳动：收盘价在交易时段内不会变，HY OAS 更是日度数据。

## 为什么不用浏览器直接拉 Yahoo / FRED

已实测：浏览器直接请求 Yahoo / FRED / CBOE 会被 CORS 拦截（无 `Access-Control-Allow-Origin`，CBOE 直接 403）。
所以数据必须在**服务端**（这里是 GitHub Actions）拉好、写成 `data.json`，页面只同源读取。不要把 Yahoo / FRED 的 URL 写进页面 JS 里——在浏览器里跑不通。

## 备用方案：Cloudflare Workers 代理（未启用，仅作备选）

如果以后想要**盘中**刷新（而不是等收盘），可以加一个 Cloudflare Workers 做代理：
Worker 服务端请求 Yahoo（不受 CORS 限制），页面请求 Worker 的 URL。注意 Workers 免费版每天 10 万次请求、每次 10ms CPU，
一个页面加载会产生多次请求，流量大了会先触顶。当前需求（每天看一次收盘）用 Actions + `data.json` 已足够，所以 Workers 只作备选，不建。

## 阈值（与各指标页面一致）

| 指标 | 区间 |
| --- | --- |
| VIX | <20 平静 · 20–30 偏高 · 30–40 紧张 · >40 恐慌 |
| VVIX | <70 偏低 · 70–120 正常 · 120–140 偏高 · >140 极端（页面长图分区；README 细分为 <80 偏低 / 80–100 正常 / 100–120 偏高 / 120–140 高 / >140 极端） |
| SKEW | <120 正常 · 120–140 偏高 · >140 高（SKEW 无独立页面，取 CBOE 常见解读：100 为中性，越高尾部定价越贵） |
| MOVE | <60 平静 · 60–100 中等 · 100–140 偏高 · >140 极高 |
| HY OAS | <300bp 偏低 · 300–500bp 正常偏高 · 500–800bp 高 · 800–1000bp 很高 · >1000bp 极端 |

综合判断以 VIX / MOVE / HY OAS 三个必看指标中最严重者为准，VVIX / SKEW 作辅助。

## 报警（飞书机器人，第一阶段）

### 触发线

触发线取各指标 README 的区间，不另设激进值（见 `scripts/fetch_dashboard.py` 顶部的 `ALERT_THRESHOLDS`，改那里即可）：

| 指标 | 预警 | 严重 |
| --- | --- | --- |
| VIX | ≥ 35 | ≥ 40 |
| VVIX | ≥ 140 | — |
| SKEW | ≥ 150 | — |
| MOVE | ≥ 140 | — |
| HY OAS | ≥ 500bp | ≥ 1000bp |
| 综合预警 | VIX ≥ 30 且 VVIX ≥ 140 或 MOVE ≥ 140 | — |

### 去重 / 防刷屏

- 状态记录在 `dashboard/alert_state.json` 里（每项：当前级别、当前值、日期、上次推送时间）。
- 只在**新触发**、**级别升级**（正常→预警、预警→严重）时推送；同一级别 24 小时内不重复推；同一级别超过 24 小时未解除，会再推一次（每日提醒）。
- 级别回落到正常时不推送，只更新状态文件。

### 数据拉取失败也要报警

- Yahoo 拉不到（例如 429 限流）或 FRED 拉不到时，同样在飞书 + Bark 上推一条“数据获取失败”报警，写明是哪个数据源、失败原因、时间，并说明仪表盘当前显示的是上一次成功拉取的数据（带日期），以便及时调整。
- 同一个数据源的失败报警 24 小时内只推一次；恢复后状态回到正常，下一次再失败会重新报警。
- 仪表盘报警区下方新增一行“数据拉取”状态：Yahoo / FRED 各自正常或失败（含失败原因和时间），状态存在 `alert_state.json` 的 `fetch_status` 里。

### 怎么接飞书（只做这一步，不要把 URL 写进任何文件）

1. 飞书群里添加自定义机器人，拿到 webhook 地址。
2. 到 GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret：
   - Name：`FEISHU_WEBHOOK_URL`
   - Secret：粘贴飞书机器人 webhook 地址。
3. 工作流会把这个 Secret 以环境变量传给拉取脚本；脚本只在服务端 POST，页面（`index.html`）里没有任何 webhook 相关内容。
4. **不要把 webhook 地址写进代码、注释、README、workflow 或任何测试文件**——仓库是公开的，写进去任何人都能往群里发消息。

### 怎么接 Bark（手机端提醒，与飞书并行）

Bark 与飞书用同一套触发线、同一份去重状态（`alert_state.json`），同一个报警事件两个通道同时发，内容一致；其中一个通道失败只记日志，另一个照常发。

1. 在自己的 Bark 服务器（或 Bark App）里拿到推送地址。需要的只是 base 部分：`https://<你的 Bark 服务器>/<key>`，**不含标题和正文**。
2. 到 GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret：
   - Name：`BARK_URL`
   - Secret：粘贴 Bark 推送地址的 base 部分（服务器 + key）。
3. 工作流会把这个 Secret 以环境变量传给拉取脚本。脚本用 GET 请求 `{base}/{标题}/{正文}` 推送，标题和正文都经过 URL 编码，查询参数为 `group=monitoring-metrics`，严重级别 `level=critical`（严重）或 `level=active`（预警）。
4. **不要把 Bark 服务器地址或 key 写进代码、注释、README、workflow 或任何测试文件**——仓库是公开的，写进去任何人都能往手机发推送。
5. 仪表盘的报警区只显示两个通道的“已配置 / 未配置”，绝不显示地址本身。

### 本地测试

不要在本地用真 webhook / 真 Bark 地址发测试消息，除非用户明确说要发。本地验证方法：
`FEISHU_WEBHOOK_URL` 与 `BARK_URL` 都不设时跑 `python3 scripts/fetch_dashboard.py`，应正常生成 `data.json` 与 `alert_state.json` 且不发任何请求；
想验证推送逻辑时，用本地假地址（如指向本机监听端口）代替真 webhook / 真 Bark 地址，确认只在触发时发送、重复运行不重复发、一个通道失败不影响另一个。

## 读法

- 只看 VIX 不够：它不告诉方向，也不反映债市和信用。MOVE 高 + VIX 低是最容易漏掉的组合（债市先报警）。
- HY OAS 走扩才是“内伤”信号：股市跌但利差不扩，多半是情绪调整；利差快速走扩，说明融资变贵、违约担忧起来了。
- 期限结构看形状：升水（近低远高）正常；贴水（近高远低）是恐慌集中在眼前。VIX / VIX3M > 1 为倒挂，> 1.05 为明显倒挂。
- 兜底快照：`data.json` 拉取失败时，页面显示 2026-11-23 收盘快照并明确标注日期，绝不冒充实时数据。

## 本地查看

```bash
python3 scripts/fetch_dashboard.py   # 生成 dashboard/data.json
python3 -m http.server -d dashboard 8000  # 再用浏览器打开 http://localhost:8000/
```

直接双击打开 `index.html` 也能看，但会走兜底快照（`file://` 下 `fetch('./data.json')` 不可用）。
