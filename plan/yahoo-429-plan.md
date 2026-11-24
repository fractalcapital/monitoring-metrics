# 数据源与 Yahoo 429 限流：处理计划

> 状态：观察期（2026-11-24 起）。先不改动，观察 429 发生的频率，再按本文的决策标准选方案。
> 相关代码：`scripts/fetch_dashboard.py`（拉取）、`alert/notify.py`（飞书 / Bark 发送）、`scripts/push_data.sh`（发布）、`api/deploy.sh`（VPS 部署）、`api/deploy-mac.sh`（Mac 拉取部署）。

## 1. 背景

实时仪表盘（`dashboard/`）每天美股收盘后更新两次（北京时间 05:30、07:30），数据来自两个公开源：

| 指标 | 来源 | 接口 |
| --- | --- | --- |
| VIX、VVIX、SKEW、MOVE、标普 500、期限结构（VIX1D / 9D / 3M / 6M） | Yahoo | `query1 / query2.finance.yahoo.com`，`/v8/finance/chart/{标的}?range=1mo&interval=1d`，共 9 个标的 |
| HY OAS | FRED | `fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2` |

每天最少请求量：9 个标的 × 2 次 = 18 次（不含重试）。

## 2. 问题

Yahoo 对机房 IP 限流（HTTP 429）。VPS（Bandwagon Host，`144.34.135.74`）和 GitHub Actions Runner 都会被 429，拉取失败时仪表盘保留上一次的数据，并发一次“数据获取失败”预警（24 小时内同一来源不重复报）。

这不是请求太频繁的问题（一天才拉两次），而是 **IP 声誉问题**：Yahoo 把机房 IP 列入限流名单。重试、换 query1 / query2 只能偶尔撞上不被限的窗口，治标不治本。

影响：仪表盘数据不能稳定刷新，只能显示上一次成功拉到的收盘价（当前是 2026-11-23 的收盘价）。

## 3. 现状（已有的缓解措施）

- 重试：429 后按 10s / 30s / 60s 退避重试，query1 失败换 query2。
- 兜底：Yahoo 失败时保留 `data.json` 的旧数据继续展示，不让页面断档。
- 预警：拉取失败发飞书 / Bark 提醒（注明来源、错误、时间，24 小时去重）；恢复后清除状态。
- 拉取直连：`fetch_dashboard.py` 与 `alert/notify.py` 已改成不走系统代理（`ProxyHandler({})`），拉取永远用本机自己的出口 IP，不会被代理带偏。
- Mac 拉取脚本已就绪：`api/deploy-mac.sh`（launchd，每天 05:30 / 07:30），但尚未部署（见第 5 节）。

## 4. 根因

Yahoo 按出口 IP 限流。机房 / 云服务商 IP（VPS、GitHub Actions）被重点限流；家庭宽带（住宅 IP）每天只发十几次请求时一般不被限流。决定成败的是**拉取请求的出口 IP**，不是请求次数、不是 User-Agent、不是换 query1 / query2。

## 5. 已尝试 / 已排除的方案

| 方案 | 结果 | 结论 |
| --- | --- | --- |
| CBOE 官方 CDN（VIX 系列备用源） | 2026-11-24 从机房 IP 实测，`cdn.cboe.com/api/global/delayed_quotes/indices/{SYM}.json` 返回 `AccessDenied`（403） | 从 VPS 上走不通，暂不采用 |
| FRED 兜底 VIX / 标普 | 实测可用（`VIXCLS`、`SP500`），但比 Yahoo 慢约 1 个工作日，且 FRED 没有 VVIX、SKEW、MOVE 和期限结构 | 只能兜底一部分，见候选方案 C |
| Mac Studio 拉取（家里宽带 IP） | 脚本已写好并推送，但实测发现该 Mac 的出口 IP 就是 VPS 的 `144.34.135.74`（整机流量走 VPS），Yahoo 照样 429；给 query1 / query2 加直连规则后仍 429 | 关键待确认：是代理层面（可绕过）还是 VPN 隧道层面（绕不过）。诊断命令 `curl --noproxy "*" -s https://ifconfig.me` 尚未跑，见第 7 节 |
| tvDatafeed（TradingView 抓取） | 用户已明确先不用 | 不采用 |
| 换免费 API（Twelve Data / Alpha Vantage） | 仅讨论过，未注册实测 | 待验证覆盖范围，见候选方案 D |

## 6. 候选方案

### A. 继续观察（当前选择）

什么都不改，让 VPS + Actions 继续跑，靠兜底和预警顶着，观察一周 429 的频率。

- 优点：零改动、零成本。
- 缺点：数据新鲜度看运气。
- 适用：观察期内 429 只是偶发，大部分定时拉取能拿到新数据。

### B. Mac 拉取（家里宽带 IP）

在 Mac Studio（或 Mac mini）上跑拉取，用家里宽带的住宅 IP 拿 Yahoo 数据，VPS 只保留 API 服务，VPS 定时拉取在 Mac 稳定后停掉。

- 前置条件：先跑诊断 `curl --noproxy "*" -s https://ifconfig.me`。
  - 显示家宽 IP → 是代理层面的问题，拉取脚本已绕过代理，直接部署 `api/deploy-mac.sh` 即可。
  - 仍显示 `144.34.135.74` → 是 VPN 隧道层面（很可能 Tailscale 出口节点设成了 VPS），代理规则改不了出口 IP，需要给 Yahoo 做分流排除，或换方案。
- 优点：最彻底、免费、当天收盘价全覆盖，不用注册新服务。
- 缺点：依赖 Mac 在 05:30 / 07:30 处于唤醒状态（脚本可设 05:25 唤醒）；家里网络本身要能直连 Yahoo（若被墙挡住则此路不通）。

### C. FRED + Yahoo 混用

VIX、标普、HY OAS 走 FRED（稳定，但慢约 1 个工作日），VVIX、SKEW、MOVE、期限结构继续走 Yahoo。

- 优点：VPS 上不用动架构，核心指标不再依赖 Yahoo。
- 缺点：VIX / 标普要慢一天（大跌当天仪表盘显示的是昨天的值）；Yahoo 那几个指标还是会 429，不能根治。
- 工作量：改 `fetch_dashboard.py`，给每个指标配数据源优先级（Yahoo 优先 → FRED 兜底）。

### D. 换免费行情 API

把 Yahoo 换成免费档的市场数据 API，VPS 架构不动，只换数据源。

- Twelve Data：免费档每天 800 次，够用（每天需 18 次 + 重试）。
- Alpha Vantage：免费档每天 25 次，够用但偏紧（18 次 + 重试就接近上限）。
- 待验证：MOVE 和期限结构（VIX1D / 9D / 3M / 6M）这两个 API 有没有；需要注册一个免费 key（放 `.env`，与飞书 / Bark 密钥同等对待，绝不进仓库）。
- 优点：彻底绕开 Yahoo 的 IP 限流，VPS 上直接可用。
- 缺点：多一个外部依赖和密钥；免费档随时可能收紧。

### E. 增加重试窗口（纯缓解）

在现有两次拉取之外再加一次（比如北京时间 09:30），多一次撞上不被限流窗口的机会。

- 优点：改动最小（VPS cron + Actions 各加一行）。
- 缺点：本质还是看运气，不能保证新鲜度。

### F. 住宅代理（付费，最后手段）

买住宅代理 IP 走 Yahoo。用户倾向免费方案，此项仅在以上全都行不通时再议，不主动推进。

## 7. 待确认事项

1. Mac 直连诊断：`curl --noproxy "*" -s https://ifconfig.me`，看纯直连的出口 IP 是家宽还是 `144.34.135.74`（区分代理层面还是 VPN 隧道层面）。
2. 家宽直连 Yahoo 是否通：`curl -s -o /dev/null -w "%{http_code}\n" "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d"`，返回 200 才说明直连可用。
3. 方案 D 的覆盖验证：注册后实测 Twelve Data / Alpha Vantage 有没有 MOVE 和期限结构。
4. 观察期数据：每天两次定时拉取各记一次成功 / 失败（成功指拿到当天收盘价），一周后汇总。

## 8. 决策标准

观察一周（到 2026-12-01 左右）后按以下顺序判断：

1. 若 Yahoo 在 ≥50% 的定时拉取中能拿到当天收盘价 → 维持方案 A，必要时加方案 E 的一次重试。
2. 若 Mac 诊断显示直连出口是家宽 IP，且直连 Yahoo 返回 200 → 部署方案 B（Mac 拉取），VPS 定时拉取在 Mac 稳定一两天后停掉。
3. 若 Mac 直连走不通（隧道层面或 Yahoo 被墙），且 429 仍频繁 → 在方案 C 与 D 之间选：先验证 D 的覆盖，覆盖全则选 D，否则选 C。
4. 若以上全都行不通 → 再议方案 F。

## 9. 成功标准

- 定时拉取拿到当天收盘价的比例 ≥80%（一周统计）。
- “数据获取失败”预警一周内不超过 2 次。
- 仪表盘展示的数据日期与最新交易日一致（HY OAS 因 FRED 滞后可晚一个工作日）。

## 10. 时间线

| 时间 | 事项 |
| --- | --- |
| 2026-11-24 | 计划写入；进入观察期 |
| 2026-11-24 ~ 2026-12-01 | 观察 429 频率；有空时跑第 7 节的 Mac 诊断（两条命令） |
| 2026-12-01 左右 | 按第 8 节决策，实施选定方案 |
| 实施后一周 | 按第 9 节验收 |

---

*本文只记录计划与决策依据，不含任何密钥。飞书 webhook、Bark 地址、API token、GitHub PAT 只放在 GitHub Secrets 与服务器 `.env` 里。*
