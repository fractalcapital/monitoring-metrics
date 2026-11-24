# monitoring-metrics

市场波动与风险监控指标的可视化合集：VIX、VIX 期限结构、VVIX、MOVE、高收益债信用利差（HY OAS）。每个指标一页独立的交互式 HTML，可直接在浏览器打开；右上角都有手动亮色 / 暗色切换（默认跟随系统）。

## 页面一览

| 指标 | 页面 | 一句话 |
| --- | --- | --- |
| VIX | [vix/index.html](vix/index.html) | CBOE 波动率指数：标普 500 未来 30 天的隐含波动率，“恐慌指数”只是外号 |
| VIX 期限结构 | [vix-term-structure/index.html](vix-term-structure/index.html) | 现货 + 不同到期日 VIX 期货连成的曲线：升水 vs 贴水 |
| VVIX | [vvix/index.html](vvix/index.html) | 波动率的波动率：VIX 本身预计会怎么动 |
| MOVE | [move/index.html](move/index.html) | 债市的 VIX：美债收益率未来一个月的隐含波动 |
| HY OAS | [hy-oas/index.html](hy-oas/index.html) | 高收益债信用利差：企业借钱比美债多付多少，测违约担忧 |
| 实时仪表盘 | [dashboard/index.html](dashboard/index.html) | VIX + VVIX + SKEW + MOVE + HY OAS 五项同页实时监控（Actions 每天收盘后更新两次，页面每 60 秒刷新；飞书 + Bark 预警） |

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
open vix/index.html
open vix-term-structure/index.html
open vvix/index.html
open move/index.html
open hy-oas/index.html
open dashboard/index.html
```

页面除 Google Fonts 外均为自包含静态 HTML；离线时字体会回退到系统字体，不影响内容和交互。

## 数据口径与免责

- 本仓库是科普 / 监控用的可视化，不是投资建议。
- VIX、VVIX、SKEW、MOVE、HY OAS 的定义与区间见各子目录 README；阈值是经验区间，不是买卖信号。
- 读数会随市场变化，页面里的 2026-11-20 读数是当时的快照，不代表当前水平。
