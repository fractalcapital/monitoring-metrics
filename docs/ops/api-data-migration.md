# VPS 运维日志：仪表盘数据迁移至 API 直出（取消日常 Git 提交）

> 日期：2026-11-26  
> 目标主机：`proud-pipes-7`  
> 相关提交：`725949a`、`0f08903`

## 1. 背景与方案选型

此前仪表盘托管在 GitHub Pages，前端同源读取仓库内的 `dashboard/data.json`。VPS 每次定时拉取到新行情后，需要通过 `scripts/push_data.sh` 反向执行 `git pull --rebase` → `git commit` → `git push` 写回 `main` 分支。长期运维存在以下痛点：

- 仓库 `main` 分支充斥大量 `chore: update dashboard data` 自动提交，干扰代码历史；
- VPS 需长期维护 GitHub Fine-grained PAT（存在定期轮换、过期导致线上数据停更的风险）；
- VPS 反向推库与日常代码发版容易产生 rebase 冲突或脏工作区；
- 数据更新需额外等待 GitHub Pages 构建（1–2 分钟）。

### 方案对比与决策

| 方案 | 运维复杂度 | 是否消除 Git PAT / 推库 | 生效速度 | 结论 |
| --- | --- | --- | --- | --- |
| **方案 A：VPS API 直出（`GET /api/data`）** | 最低（复用现有 `api.infidea.app`，零新增组件） | 是 | 秒级 | **采用** |
| 方案 B：Cloudflare R2 公开桶 | 中（新增桶与 R2 API 密钥） | 是 | 秒级 | 备选（适合大流量公开访问） |
| 方案 C：独立 `data` 分支 | 中（仍需维护 PAT 与推送链路） | 否 | 1–2 分钟 | 不推荐 |

由于拉取任务、Futu CLI、飞书/Bark 告警及手动刷新均已运行在 `proud-pipes-7` + Cloudflare Tunnel 上，采用 **方案 A** 可在不引入任何新外部依赖的前提下，彻底砍掉最脆弱的 Git 反向推库链路。

## 2. 改造后的架构与数据流

1. **服务端抓取与落盘**：
   - `scripts/push_data.sh` 移除全部 `git pull / add / commit / push` 与推库失败报警逻辑，仅保留并发锁（`flock` / 锁目录）、调用 `scripts/fetch_dashboard.py` 原子写入与可选 `HEALTHCHECK_URL` 心跳。
   - 运行时产物目录由 `.env` 中的 `MM_OUT_DIR=/var/lib/monitoring/data` 指定，与 `/opt/monitoring-metrics` 代码仓库彻底分离。
2. **API 只读直出（`api/api_server.py`）**：
   - 新增公开只读端点 `GET / HEAD /api/data` 与 `GET / HEAD /api/alert-state`（无需 `X-Api-Token`）。
   - 返回头包含 `ETag`（支持 `If-None-Match` → `304 Not Modified`）、`Cache-Control: public, max-age=60, stale-while-revalidate=300` 及 `Access-Control-Allow-Origin: *`。
   - 读取文件前校验 JSON 合法性，防止异常文件被当作 `200` 下发。
   - `POST /api/refresh` 完成后直接随结果返回最新 `data` 与 `alert_state`，写端点仍保持 `X-Api-Token`、`ALLOWED_ORIGIN` 与基于 `CF-Connecting-IP` 的限速保护。
3. **前端四级加载与兜底（`dashboard/index.html`）**：
   - 主链路：优先请求 `https://api.infidea.app/api/data` 与 `/api/alert-state`（带 8 秒超时保护），成功后写入浏览器 `localStorage`。
   - 降级链路（当 API 暂不可用时）：比较浏览器 `localStorage` 缓存与同源静态 `./data.json` 的 `updated_at_utc`，优先展示更新的一份；若均不可用则回退内置 `SNAPSHOT`。
4. **外部巡检升级（`.github/workflows/freshness-watchdog.yml`）**：
   - 从读取仓库内 `dashboard/data.json` 改为直接请求 `https://api.infidea.app/api/data`（3 次重试）。
   - 同时覆盖 **VPS API 存活** 与 **收盘日期新鲜度** 两项监控，异常时经飞书与 Bark 告警。

## 3. 目录与配置布局

| 路径 | 属主 / 权限 | 说明 |
| --- | --- | --- |
| `/opt/monitoring-metrics` | `monitoring:monitoring` | 纯代码仓库，后续发版只需单向 `git pull --ff-only`，不再产生本地数据修改 |
| `/opt/monitoring-metrics/.env` | `monitoring:monitoring` (`0600`) | 新增 `MM_OUT_DIR=/var/lib/monitoring/data`；systemd 与 cron 共用 |
| `/var/lib/monitoring/data/data.json` | `monitoring:monitoring` | 运行时最新收盘与配置数据（由 `GET /api/data` 提供） |
| `/var/lib/monitoring/data/alert_state.json` | `monitoring:monitoring` | 运行时预警去重与通道状态（由 `GET /api/alert-state` 提供） |
| `/var/lib/monitoring/data/monitoring-metrics-fetch.lock` | `monitoring:monitoring` | `push_data.sh` 并发执行锁文件 |
| `dashboard/data.json`（仓库内） | git 跟踪 | 仅保留作静态兜底与新机初始化种子，不再随日常行情提交 |

## 4. VPS 升级与迁移步骤

### 从旧架构平滑升级（以 `root` 执行）

```bash
# 1. 拉取最新代码并运行部署脚本（自动创建数据目录、拷贝初始种子、追加 MM_OUT_DIR 并重启服务）
cd /opt/monitoring-metrics
git pull --ff-only
bash /opt/monitoring-metrics/api/deploy.sh <API_TOKEN>

# 2. 验证外网只读接口（应返回 HTTP/2 200、ETag、Cache-Control 与 CORS 头）
curl -sI https://api.infidea.app/api/data
curl -sI https://api.infidea.app/api/alert-state

# 3. 确认代码仓库工作区保持干净
git -C /opt/monitoring-metrics status -s

# 4. 清理不再需要的 GitHub 推送凭据（随后在 GitHub Settings 中撤销原 Fine-grained PAT）
rm -f /opt/monitoring-metrics/.git-credentials
```

### 日常代码发版（升级后）

以后修改后端或抓取脚本并推送到 GitHub 后，VPS 上不再需要重跑 `deploy.sh`，只需两步：

```bash
cd /opt/monitoring-metrics && git pull --ff-only && systemctl restart monitoring-api
```

## 5. 升级中遇到的问题与处理

1. **首次执行 `git pull` 未拿到新架构代码**
   - 原因：本地改动尚未提交推送到 `origin/main`，VPS 拉到的仍是旧提交 `94816b4`。
   - 处理：将改动提交推送至 `origin/main`（`725949a`）后，VPS 重新 `git pull --ff-only` 并运行 `deploy.sh`。
2. **`curl -sI https://api.infidea.app/api/data` 返回 `HTTP/2 501`**
   - 原因：`curl -I` 发送的是 `HEAD` 请求，原 `api_server.py` 仅实现 `do_GET / do_POST / do_OPTIONS`，未实现 `do_HEAD`。
   - 处理：在 `api/api_server.py` 补充 `do_HEAD()`（复用 `do_GET(head_only=True)`，返回完整状态码、ETag、Cache-Control 与 Content-Length 但不输出 body），并补充对应单元测试（`0f08903`）。
3. **`systemctl restart` 后立即 `curl` 出现瞬时 `502`**
   - 原因：`systemctl restart` 返回后 Python 服务进程启动需数十毫秒，紧接其后的首条请求命中启动间隙。
   - 处理：等待 1 秒后再次请求即稳定返回 `HTTP/2 200`（属正常重启窗口，前端也有 `localStorage` 兜底）。

## 6. 最终验收结果（2026-11-26）

- `deploy.sh` 自动完成数据迁移：
  - `Seeded /var/lib/monitoring/data/data.json` 与 `alert_state.json`；
  - `.env` 自动追加 `MM_OUT_DIR=/var/lib/monitoring/data`；
  - `monitoring-api` 启动日志确认 `listening on 127.0.0.1:8080 repo=/opt/monitoring-metrics data_dir=/var/lib/monitoring/data`；
  - 冒烟测试以 `monitoring` 用户成功拉取并输出 `FETCH_OK`，未产生任何 git 提交。
- 公网接口验证结果：
  - `curl -sI https://api.infidea.app/api/data` → `HTTP/2 200`，`content-length: 30434`，`etag: "bd0ba3c423c211c3"`，`cache-control: public, max-age=60, stale-while-revalidate=300`，`access-control-allow-origin: *`；
  - `curl -sI https://api.infidea.app/api/alert-state` → `HTTP/2 200`，`content-length: 7404`，`etag: "e5b78a4f65484810"`，响应头与 `/api/data` 一致。
- 单元测试与语法检查：`python3 -m unittest discover -s tests -v` 全部 19 项通过。

## 7. 日常运维与排障命令

```bash
# 检查 API 存活与最新数据响应头
curl -s https://api.infidea.app/health
curl -sI https://api.infidea.app/api/data
curl -sI https://api.infidea.app/api/alert-state

# 查看运行时数据更新时间
python3 -c 'import json; d=json.load(open("/var/lib/monitoring/data/data.json")); print(d["updated_at_utc"], d["indicators"]["vix"]["date"])'

# 手动触发一次抓取（仅更新 /var/lib/monitoring/data，不改仓库）
su - monitoring -c "cd /opt/monitoring-metrics && /bin/bash ./scripts/push_data.sh"

# 查看服务日志与定时拉取日志
journalctl -u monitoring-api -n 50 --no-pager
tail -n 60 /var/log/monitoring-metrics/fetch.log
```

---

*本文仅记录架构迁移、命令与排障过程，不含任何密钥、API Token、PAT 或 Webhook 地址。*
