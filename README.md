# monitoring-metrics

市场波动与风险监控指标的可视化合集：VIX、VIX 期限结构、VVIX、MOVE、高收益债信用利差（HY OAS）。每个指标一页独立的交互式 HTML，可直接在浏览器打开；右上角都有手动亮色 / 暗色切换（默认跟随系统）。

在线访问：https://mon.infidea.app/（实时仪表盘：https://mon.infidea.app/dashboard/）。

## 页面一览

| 指标 | 页面 | 一句话 |
| --- | --- | --- |
| VIX | [metrics/vix/index.html](metrics/vix/index.html) | CBOE 波动率指数：标普 500 未来 30 天的隐含波动率，“恐慌指数”只是外号 |
| VIX 期限结构 | [metrics/vix-term-structure/index.html](metrics/vix-term-structure/index.html) | 现货 + 不同到期日 VIX 期货连成的曲线：升水 vs 贴水 |
| VVIX | [metrics/vvix/index.html](metrics/vvix/index.html) | 波动率的波动率：VIX 本身预计会怎么动 |
| MOVE | [metrics/move/index.html](metrics/move/index.html) | 债市的 VIX：美债收益率未来一个月的隐含波动 |
| HY OAS | [metrics/hy-oas/index.html](metrics/hy-oas/index.html) | 高收益债信用利差：企业借钱比美债多付多少，测违约担忧 |
| 实时仪表盘 | [dashboard/index.html](dashboard/index.html) | VIX + VVIX + SKEW + MOVE + HY OAS 五项同页实时监控（每天美股收盘后更新两次，05:30 / 07:30 北京时间；飞书 + Bark 预警） |

每个子目录里还有一份对应的 `README.md`，讲该指标的定义、区间、关键读数和常见误读。

## 3 + 2 监控组合

只看 VIX 不够，它只回答“未来 30 天标普 500 预计怎么波动”，不告诉方向，也不反映债市和信用市场。

- **必看 3 个**：VIX + 期限结构、MOVE、高收益债信用利差（HY OAS）
- **进阶再加 2 个**：VVIX、SKEW

## 关键读数（2026-11-20，AI 下跌那轮）

- VIX：盘中摸到 40.03，收 35.9；12 月 VIX 期货约 27.53
- VVIX：收 138.03，低于 2025 年 4 月那次低点的 171
- SKEW：138.84
- 标普 500 当天跌 3.7% 至 6,910.39，纳斯达克 100 跌 5.61%，英伟达跌 14.01%

交叉验证的结论是“触及了恐慌，但没确认”。MOVE 和高收益利差在 11 月 20 日当天没有已核实的读数，相关页面里没有硬填数字。

## 历史峰值（收盘口径，另有说明除外）

- VIX：2008 年 11 月 20 日收 80.86（盘中最高 89.53，2008 年 10 月 24 日）；2020 年 3 月 16 日收 82.69，为历史最高收盘
- VVIX：2020 年 3 月约 220（峰值）；2025 年 4 月 171
- MOVE：2008 年约 264.6；2020 年约 164；2023 年 3 月银行危机约 199
- HY OAS：2008 年约 2,150bp；2020 年约 1,090bp；2002 年约 1,040bp

历史长图里的趋势连线为示意，部分峰值在页内标注为代表性约数，具体口径见各页页脚。

## 本地查看

```bash
# 任选其一，用浏览器打开即可，无需构建
open metrics/vix/index.html
open metrics/vix-term-structure/index.html
open metrics/vvix/index.html
open metrics/move/index.html
open metrics/hy-oas/index.html
open dashboard/index.html
```

页面除 Google Fonts 外均为自包含静态 HTML；离线时字体会回退到系统字体，不影响内容和交互。

## 实时仪表盘：数据、预警与运维

### 数据怎么来

仪表盘不直接调行情接口（浏览器跨域拿不到 Yahoo / FRED 的数据），而是由一个服务端拉取脚本每天收盘后更新两次，再把结果写进 `dashboard/data.json`，页面同源读取：

- 拉取脚本：`scripts/fetch_dashboard.py`，每天北京时间 05:30 和 07:30 各跑一次（美股收盘后 30 分钟，及之后的一次重试）
- 数据源：VIX / VVIX / SKEW / 标普 500 优先走美区 VPS 的 Futu CLI（CBOE CDN / FRED 兜底）；VIX 期限结构走 CBOE 官方 CDN CSV；MOVE 走 Yahoo（`curl_cffi`）→ TradingView → CSV 三级兜底；HY OAS 走 FRED 官方 JSON API（`BAMLH0A0HYM2`）
- 产物：`dashboard/data.json`（最新收盘价）和 `dashboard/alert_state.json`（预警去重状态），由 VPS 定时提交进仓库

### 已知滞后

- **HY OAS 慢一天**：FRED 的数据要到下一个工作日的美国白天才贴出来，所以 HY OAS 的日期通常比 VIX 晚一个工作日（比如 VIX 是 11-24，HY OAS 是 11-23）。这是数据源本身的节奏，不是拉取故障；页面上 HY OAS 旁边标了日期。

### 波动情况与预警

每张卡片都有一个“波动情况”状态：先按区间定级（比如 VIX <20 平静、20–30 警惕、30–40 紧张、≥40 恐慌），再看单日异动（处在常态区间但单日涨跌 ≥10% 的记为“异动”）。预警阈值：VIX ≥35 预警 / ≥40 严重，VVIX ≥140，SKEW ≥150，MOVE ≥140，HY OAS ≥500bp 预警 / ≥1000bp 严重。预警只在新触发或升级时发，同一级别 24 小时内不重复。

预警走两个通道（由 VPS 服务端发，密钥只放在服务器 `.env` 里，仓库里没有）：飞书机器人（卡片消息）和 Bark 移动推送。数据拉取失败也会预警，注明来源、错误和时间。

### 手动刷新与推送

仪表盘上有三个按钮，走 VPS 上的 API（`https://api.infidea.app`）：

- 刷新数据：重新拉取并更新 `data.json`（异步）
- 推送到飞书 / 推送到 Bark：用已缓存的数据发一份当前状态汇总（不重新拉取，1–2 秒）

API 只有这几个端点：`GET /health`、`POST /api/refresh`、`POST /api/push`、`GET /api/job`，有 token 鉴权和限速。

### 拉取跑在哪

- **VPS**（主力）：`proud-pipes-7` 上的定时任务（cron）+ API 服务（`api/`，systemd），部署见 `api/deploy.sh`
- **Mac Studio**（可选备用）：部署见 `api/deploy-mac.sh`（launchd，每天 05:30 / 07:30）

### 仓库结构

- `metrics/`：五个指标的独立页面（各带一份 README）
- `dashboard/`：实时仪表盘页面 + `data.json` + `alert_state.json`
- `scripts/`：拉取脚本（`fetch_dashboard.py`）与发布脚本（`push_data.sh`）
- `alert/`：飞书 + Bark 发送模块（`notify.py`）
- `api/`：VPS 上的 API 服务与部署脚本

## 数据口径与免责

- 本仓库是科普 / 监控用的可视化，不是投资建议。
- VIX、VVIX、SKEW、MOVE、HY OAS 的定义与区间见各子目录 README；阈值是经验区间，不是买卖信号。
- 读数会随市场变化，页面里的 2026-11-20 读数是当时的快照，不代表当前水平。
