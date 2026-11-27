# VPS 日常运维速查手册（Runbook）

> 目标主机：`proud-pipes-7`  
> 运行用户：`monitoring`（API 服务、定时拉取、Futu OAuth 凭据与运行时数据目录均归属该用户）  
> 历史迁移记录见：[monitoring-user.md](monitoring-user.md)、[api-data-migration.md](api-data-migration.md)

---

## 1. 代码发版与重启服务

推送代码到 GitHub `main` 分支后，在 VPS 上拉取代码并重启 API 服务：

```bash
# 拉取最新代码并重启 API 服务（以 root 执行）
su - monitoring -c "git -C /opt/monitoring-metrics pull --ff-only" \
  && systemctl restart monitoring-api

# 确认服务状态正常（active (running)）
systemctl status monitoring-api --no-pager

# 如涉及数据结构 / history.json / 告警逻辑变动，顺手触发一次拉取
su - monitoring -c "cd /opt/monitoring-metrics && /bin/bash ./scripts/push_data.sh"
```

> **提示**：用 `su - monitoring -c "git -C /opt/monitoring-metrics pull --ff-only"` 拉代码可以保持仓库内文件属主始终为 `monitoring:monitoring`；如果直接以 `root` 执行了 `git pull`，可补一句 `chown -R monitoring:monitoring /opt/monitoring-metrics`。

---

## 2. 手动触发数据拉取（切勿直接用 root 跑）

**正确方式**（必须切换为 `monitoring` 用户）：

```bash
su - monitoring -c "cd /opt/monitoring-metrics && /bin/bash ./scripts/push_data.sh"
```

### 如果不小心用 `root` 跑了 `bash push_data.sh`

会出现两个问题：
1. `root` 环境读不到 `/var/lib/monitoring/.futu/` 里的登录态，导致 `futu` 全部报 `exit 1` 并退回 CBOE / Yahoo 兜底；
2. `/var/lib/monitoring/data/` 下新写入的 JSON 文件和锁文件属主会变成 `root:root`，导致后续 `monitoring` 用户的 cron 与网页端「刷新」按钮报权限错误。

**修复命令**：

```bash
# 1. 把运行时数据目录属主改回 monitoring
chown -R monitoring:monitoring /var/lib/monitoring/data

# 2. 用 monitoring 用户重跑一次，确认 [futu] 恢复且输出 FETCH_OK
su - monitoring -c "cd /opt/monitoring-metrics && /bin/bash ./scripts/push_data.sh"
```

---

## 3. Futu CLI Token 过期与故障排查

当拉取日志出现 `[vix] futu failed` 或 `vix / vvix / skew / spx` 全部退回 `[cboe]` / `[yahoo_cffi]` 时：

```bash
# 1. 以 monitoring 身份直接测试一条 K 线，查看真实报错（不截断）
su - monitoring -c "futu kline US..VIX --ktype 2 --num 3 --format json"

# 2. 若提示未登录 / OAuth token 过期，重新登录即可（生成 monitoring 专属加密 token.json）
su - monitoring -c "futu auth login"

# 3. 验证单条查询恢复正常
su - monitoring -c "futu kline US..VIX --ktype 2 --num 3 --format json"

# 4. 触发一次完整拉取，确认输出里 vix / vvix / skew / spx 均回到 [futu]
su - monitoring -c "cd /opt/monitoring-metrics && /bin/bash ./scripts/push_data.sh"
```

> **注意**：不要把 `/root/.futu/token.json` 复制给 `monitoring`——Linux 下 `futu` 按「主机名 + 用户名」派生加密密钥，跨用户拷贝会解密失败。必须用 `su - monitoring -c "futu auth login"` 独立完成登录。

---

## 4. 公网 API 与数据验收

```bash
# 1. 健康检查
curl -s https://api.infidea.app/health; echo

# 2. 验证实时数据与 1 年历史接口（核对 HTTP/2 200、content-encoding: gzip、etag）
curl -sI --compressed https://api.infidea.app/api/data
curl -sI --compressed https://api.infidea.app/api/history
curl -sI --compressed https://api.infidea.app/api/alert-state

# 3. 查看告警历史轨迹（最近触发 / 升级 / 解除记录）
curl -s https://api.infidea.app/api/alert-history | head -c 500; echo

# 4. 查看本地落盘数据的时间戳、来源与各指标最新收盘日
python3 -c '
import json
d = json.load(open("/var/lib/monitoring/data/data.json"))
print("updated_at_beijing:", d.get("updated_at_beijing"))
print("sources_used:", d.get("sources_used"))
print("dates:", {k: v.get("date") for k, v in d["indicators"].items()}, "spx:", d["spx"].get("date"))
'
```

---

## 5. 服务日志、定时任务与推送测试

```bash
# 查看 API 服务实时/最近日志
journalctl -u monitoring-api -n 80 --no-pager
journalctl -u monitoring-api -f

# 查看定时拉取日志（美股工作日 UTC 21:30 / 23:30，北京时间次日 05:30 / 07:30）
tail -n 80 /var/log/monitoring-metrics/fetch.log

# 查看 monitoring 用户的 cron 配置
crontab -u monitoring -l

# 测试飞书 + Bark 推送通道连通性（不修改任何数据文件）
su - monitoring -c "cd /opt/monitoring-metrics && MANUAL_PUSH=test /bin/bash ./scripts/push_data.sh"

# 手动用已缓存数据推送一次当前状态汇总
su - monitoring -c "cd /opt/monitoring-metrics && MANUAL_PUSH=status-cached /bin/bash ./scripts/push_data.sh"
```

---

## 6. 关键路径与属主一览

| 路径 | 预期属主 / 权限 | 说明 |
| --- | --- | --- |
| `/opt/monitoring-metrics` | `monitoring:monitoring` | 代码仓库目录（单向 `git pull --ff-only`，不存运行时数据） |
| `/opt/monitoring-metrics/.env` | `monitoring:monitoring` (`0600`) | 密钥与环境变量（`API_TOKEN`、`FRED_API_KEY`、`FEISHU_WEBHOOK_URL`、`BARK_URL`、`MM_OUT_DIR`、`FUTU_CLI`） |
| `/var/lib/monitoring/.futu/` | `monitoring:monitoring` (`0700`) | `monitoring` 专属 Futu CLI `config.json` 与加密 `token.json` |
| `/var/lib/monitoring/data/data.json` | `monitoring:monitoring` | 30 日实时仪表盘数据（`GET /api/data`） |
| `/var/lib/monitoring/data/history.json` | `monitoring:monitoring` | 近 1 年 7 条历史序列，供 `90D / 1Y` 懒加载（`GET /api/history`） |
| `/var/lib/monitoring/data/alert_state.json` | `monitoring:monitoring` | 预警回滞、24h 去重与数据源健康状态（`GET /api/alert-state`） |
| `/var/lib/monitoring/data/alert_history.json` | `monitoring:monitoring` | 最近 100 条告警触发 / 升级 / 解除轨迹（`GET /api/alert-history`） |
| `/var/lib/monitoring/data/monitoring-metrics-fetch.lock` | `monitoring:monitoring` | `push_data.sh` 并发执行锁 |
| `/var/log/monitoring-metrics/fetch.log` | `monitoring:monitoring` | cron 拉取日志（由 `logrotate` 按周轮转保留 8 份） |
| `/usr/local/bin/futu` | `root:root` (`0755`) | 全局可执行的 Futu CLI 二进制 |

---

*本文仅记录常用运维与排障命令，不含任何密钥、Token 或 Webhook 地址。*
