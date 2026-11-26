# 实时监控仪表盘

页面：[../index.html](../index.html)（`https://mon.infidea.app/` 首页直接打开；本目录 `index.html` 保留作旧路径 `/dashboard/` 的兼容跳转）—— VIX / VVIX / SKEW / MOVE / HY OAS 五项核心指标 + 标普 500 + VIX 期限结构 + IG/CCC 信用分层，一页看完。支持亮/暗双主题、移动端触控查看走势、NYSE 节假日与半日市识别、以及客户端数据过期检测。

状态栏与风控区提供三个手动按钮（均走 VPS 接口 `https://api.infidea.app`，页面不保存任何推送密钥）：
- **刷新**：异步触发服务端重新拉取最新行情并写入运行时数据目录，完成后立即刷新页面（无需等待 GitHub Pages 构建）。
- **飞书推送 / Bark 推送**：同步使用服务端已缓存的 `data.json` 立即发送五项指标状态汇总（1–2 秒返回），通道之间独立限速。

## 波动情况

每张指标卡的徽章和“波动情况”行显示同一套状态，规则由服务端随 `data.json` 的 `config.status_bands` 下发（`tests/test_parity.py` 保障 Python 与前端默认配置完全一致）：**先按水平定档，再加一条“异动”覆盖**。

| 指标 | 水平定档 | 常态区异动触发 |
| --- | --- | --- |
| VIX | <20 平静 · 20–30 警惕 · 30–40 紧张 · ≥40 恐慌 | <20 且单日 \|涨跌幅\| ≥10% |
| VVIX | <70 偏低 · 70–120 正常 · 120–140 偏高 · ≥140 极端 | 70–120 且单日 \|涨跌幅\| ≥10% |
| SKEW | <120 尾部风险定价正常 · 120–140 尾部风险定价略高 · ≥140 尾部风险定价偏高 | <120 且单日 \|涨跌幅\| ≥10% |
| MOVE | <60 债市平静 · 60–120 债市常态 · 120–140 债市偏高 · ≥140 债市紧张 | 60–120 且单日 \|涨跌幅\| ≥10% |
| HY OAS | <300bp 信用偏紧 · 300–500bp 信用常态 · 500–800bp 信用走阔 · 800–1000bp 信用紧张 · ≥1000bp 信用极端 | 300–500bp 且单日 \|变动\| ≥25bp |

综合判断以 VIX / MOVE / HY OAS 三个必看指标中最严重者为准，同时展示由 VIX / MOVE / HY OAS / VVIX 近一年分位加权合成的 **0–100 情绪压力分及近 30 日无前视偏差历史走势图**（不含 SKEW，详细算法见 [docs/composite-stress-score.md](../docs/composite-stress-score.md)）。

## 卡片统计条：1 年分位与 5 日 / 20 日变化

每张指标卡走势图下方展示 `1年分位 XX% · 5日 ... · 20日 ...`（代码见 [scripts/monitoring/series.py](../scripts/monitoring/series.py) 的 `compute_stats`）：

- **「1年分位」是什么意思**：表示当前读数在**过去一年（最近 `252` 个交易日，即 `STATS_WINDOW`）**的收盘价中处于什么相对位置。例如 `1年分位 98%` 意味着当前值高于近一年约 98% 的交易日收盘价，仅低于约 2% 的交易日，处于年内极高位置；`50%` 附近接近近一年中位数，`0%` 附近接近近一年最低点。
- **计算公式（中点分位排名 / mid-rank）**：

  $$\text{1年分位} = \operatorname{round}\!\left(100 \times \frac{\text{低于当前值的天数} + 0.5 \times \text{等于当前值的天数}}{\text{窗口总天数}}\right)$$

  当有效样本不足 `120` 个交易日（`STATS_MIN_WINDOW`，例如个别源降级只返回近 30 日缓存）时，不会用短窗口硬算，而是沿用上一次有效分位并在数字后标注 `*`（`pct_rank_stale`）。
- **5 日 / 20 日变化**：当前收盘相对 5 个 / 20 个交易日前的变动（波动率类指标显示百分比涨跌幅，HY OAS 等利差指标显示绝对基点 `bp` 变动）。
- **阅读注意**：
  1. **相对分位 ≠ 绝对危机**：分位只回答「放在过去一年里高不高」，需要配合卡片下方的**绝对区间条**一起看。例如在低波动年份，VIX 升到 `28.29` 可能已经是近一年 `98%` 分位，但按绝对阈值仍处于 `20–30 警惕` 区，尚未进入 `30–40 紧张` 或 `≥40 恐慌`。
  2. **窗口滚动效应**：分位随最近 252 个交易日滚动更新；当一年前某次极端尖峰滚出窗口时，即使当前读数不变，分位百分比也可能发生变化。
  3. **与综合压力分的关系**：VIX / MOVE / HY OAS / VVIX 的 1 年分位同时作为底层标准化输入，加权合成为看板顶部的「综合情绪压力分」（见 [docs/composite-stress-score.md](../docs/composite-stress-score.md)）。

## 数据来源与更新节奏

| 指标 | 主来源与兜底链 | 频率 |
| --- | --- | --- |
| VIX / VVIX / SKEW | 美区 VPS Futu CLI → CBOE 官方 CDN CSV → Yahoo (`curl_cffi`) | 每个交易日收盘 |
| VIX 期限结构（1D / 9D / 3M / 6M） | CBOE 官方 CDN CSV → Yahoo (`curl_cffi`) | 每个交易日收盘 |
| 标普 500 | Futu CLI → Yahoo (`curl_cffi`) → Stooq → FRED `SP500` | 每个交易日收盘 |
| MOVE | Yahoo (`curl_cffi` + Crumb) → TradingView (`TVC:MOVE`) → TheTrading.Tools CSV | 每个交易日收盘 |
| HY OAS / IG / CCC | FRED 官方 JSON API (`BAMLH0A0HYM2` / `BAMLC0A0CM` / `BAMLH0A3HYC`) → FRED CSV | **每日一次**（通常滞后 1 个工作日） |

- 美区 VPS 定时任务在美股交易日收盘后运行两次（UTC 21:30 / 23:30，周一至周五），拉取约一年历史用于计算 1 年分位与 5 日 / 20 日变化，`data.json` 保留最近 30 日走势，原子写入 `/var/lib/monitoring/data/`（与 git 仓库隔离，不提交 commit）。
- 盘中高低价经过合理性过滤（丢弃平盘 `H == L` 与异常坏 tick）；交易时段内手动刷新会自动标注“盘中”。
- 页面平时每 30 分钟从 `https://api.infidea.app/api/data` 拉取实时数据，美东收盘拉取窗口（16:25–17:05 / 18:25–19:05 ET，半日市 13:25–14:05 ET，自动适配夏/冬令时）每 1 分钟刷新；API 不可用时按浏览器 `localStorage` 缓存 → 静态 `./dashboard/data.json` → 内置快照依次兜底。

## 报警规则

触发线统一维护在 `scripts/fetch_dashboard.py` 的 `ALERT_THRESHOLDS` 并下发到 `data.json`：

| 报警项 | 预警 | 严重 | 说明 |
| --- | --- | --- | --- |
| VIX | ≥ 35 | ≥ 40 | 带 5% 回滞防抖与解除通知 |
| VVIX | ≥ 140 | — | 同上 |
| SKEW | ≥ 150 | — | 同上 |
| MOVE | ≥ 140 | — | 同上 |
| HY OAS | ≥ 500bp | ≥ 1000bp | 同上 |
| VIX 期限结构倒挂 | VIX/VIX3M ≥ 1.0 | ≥ 1.1 | 回滞 0.02，恢复升水时发解除通知 |
| 股债综合预警 | VIX ≥ 30 且 (VVIX ≥ 140 或 MOVE ≥ 140) | — | 带 5% 回滞与解除通知 |
| VIX 单日急升 | ≥ +25% | ≥ +50% | 事件型速度报警 |
| HY OAS 急速走阔 | 单日 ≥ +25bp 或 5 日 ≥ +50bp | — | 事件型速度报警 |

- 同级 24 小时去重，级别升级立刻推送；水平报警回落到回滞带下方后发送“解除”通知（Bark 使用静默 `passive` 级别）。
- 数据源拉取失败或收盘日期落后（`freshness`）时均会报警；此外 `.github/workflows/freshness-watchdog.yml` 作为独立外部巡检，每天在 VPS 定时任务之后直接请求 `https://api.infidea.app/api/data` 核验接口可用性与数据新鲜度。

## 本地查看与测试

```bash
python3 scripts/fetch_dashboard.py            # 生成 dashboard/data.json（未配密钥不发推送）
python3 -m http.server 8000                   # 仓库根目录启动，浏览器打开 http://localhost:8000/
python3 -m unittest discover -s tests -v      # 运行单元测试与 Python/JS 一致性测试
```
