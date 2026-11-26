# VPS 运维日志：切换非 root `monitoring` 用户与 Futu CLI 权限迁移

> 日期：2026-11-26  
> 目标主机：`proud-pipes-7`  
> 相关提交：`45110f3`、`4a1e5ce`、`9b30050`、`3508397`、`1a9a5e3`、`6aecce5`

## 1. 背景与目标

原部署下 `monitoring-api.service` 与定时拉取 cron 均以 `root` 运行。为降低安全面并提升长期稳定性，本次完成以下改造：

- 创建专用系统用户 `monitoring`，以非 root 身份运行 API 服务与定时拉取任务。
- 对 systemd 服务开启权限加固（`NoNewPrivileges=yes`、`ProtectSystem=strict`、最小 `ReadWritePaths`）。
- 为 `monitoring` 分配独立家目录 `/var/lib/monitoring`（与 git 仓库隔离），打通 `futu` CLI 的 OAuth 认证，保留 VIX / VVIX / SKEW / SPX 盘中实时数值与最高/最低点抓取能力。
- 配置 `logrotate` 与美股工作日 cron（`30 21,23 * * 1-5` UTC）。

## 2. 最终目录与权限布局

| 路径 | 属主 / 权限 | 说明 |
| --- | --- | --- |
| `/opt/monitoring-metrics` | `monitoring:monitoring` | 仓库目录；`.env` 与 `.git-credentials` 均为 `0600` |
| `/opt/monitoring-metrics/.git/monitoring-metrics-push.lock` | `monitoring:monitoring` | `push_data.sh` 并发锁文件与推库失败计数（不进 git） |
| `/var/lib/monitoring` | `monitoring:monitoring` (`0700`) | `monitoring` 独立 `$HOME`，存放 `.futu/`（与仓库隔离，防泄露） |
| `/var/log/monitoring-metrics/fetch.log` | `monitoring:monitoring` | cron 拉取日志，由 `/etc/logrotate.d/monitoring-metrics` 按周轮转保留 8 份 |
| `/usr/local/bin/futu` | `root:root` (`0755`) | 供所有用户执行的 Futu CLI 二进制（原 `/root/.futu/bin/futu` 的副本） |
| `/etc/systemd/system/monitoring-api.service` | `root:root` | 以 `User=monitoring` 运行，可写白名单：`/opt/monitoring-metrics`、`/var/lib/monitoring`、`/tmp`、`/var/log/monitoring-metrics` |

## 3. 迁移步骤（新机复现参考）

```bash
# 1. 部署脚本创建 monitoring 用户、配置 systemd / logrotate / 工作日 cron
bash /opt/monitoring-metrics/api/deploy.sh <API_TOKEN>

# 2. 将 Futu CLI 拷贝到公共路径（勿放开 /root 目录权限）
install -m 755 /root/.futu/bin/futu /usr/local/bin/futu

# 3. 初始化 monitoring 专属 Futu 配置（只拷 config.json，不拷 root 的 token.json）
mkdir -p /var/lib/monitoring/.futu
cp /root/.futu/config.json /var/lib/monitoring/.futu/config.json
sed -i 's|/root/|/var/lib/monitoring/|g' /var/lib/monitoring/.futu/config.json
chown -R monitoring:monitoring /var/lib/monitoring
chmod 700 /var/lib/monitoring /var/lib/monitoring/.futu
chmod 600 /var/lib/monitoring/.futu/config.json

# 4. 确保 .env 中 FUTU_CLI 指向公共路径，重启服务
sed -i 's|/root/.futu/bin/futu|/usr/local/bin/futu|g' /opt/monitoring-metrics/.env
systemctl restart monitoring-api

# 5. 以 monitoring 用户独立完成一次 Futu OAuth 登录（生成 monitoring 专属加密 token.json）
su - monitoring -c "futu auth login"

# 6. 验证 Futu 与完整拉取推送链路
su - monitoring -c "futu kline US..VIX --ktype 2 --num 3 --format json"
su - monitoring -c "cd /opt/monitoring-metrics && /bin/bash ./scripts/push_data.sh"
```

## 4. 迁移中遇到的问题与解决记录

1. **`./scripts/push_data.sh: Permission denied`**
   - 原因：文件重写后 git 模式回退为 `100644`。
   - 处理：在仓库恢复可执行位 `100755`，并在 `deploy.sh` 中补 `chmod +x` 及显式调用 `/bin/bash ./scripts/push_data.sh`。
2. **`monitoring-api` 未切换到新用户**
   - 原因：`systemctl enable --now` 不会重启已经处于 running 状态的旧进程。
   - 处理：`deploy.sh` 改为 `systemctl enable` + `systemctl restart`。
3. **`/tmp/monitoring-metrics-push.lock: Permission denied`**
   - 原因：此前 `root` 跑定时任务在 `/tmp`（带 sticky bit）残留了 `root` 属主锁文件，`monitoring` 无权打开。
   - 处理：锁文件与推库失败计数迁入仓库 `.git/` 下，既跟随仓库属主，又不受 systemd `PrivateTmp` 影响；`deploy.sh` 清理旧的 `/tmp` 残留文件。
4. **`usermod: user monitoring is currently used by process ...`**
   - 原因：修改 `monitoring` 家目录时 systemd 服务正在运行。
   - 处理：先 `systemctl stop monitoring-api`，再 `usermod -d /var/lib/monitoring monitoring`，完成后重启服务；新部署已直接在 `useradd` 阶段指定 `/var/lib/monitoring`。
5. **`futu` 报 `load token encryption key: exec: "dbus-launch": executable file not found`**
   - 原因：无桌面环境 Linux 上 `futu` 使用「主机名 + 用户名」派生密钥加密 `token.json`；把 `root` 的 `token.json` 直接拷给 `monitoring` 会因用户名变化解密失败，进而回退调用不存在的 `dbus-launch`。
   - 处理：只拷贝 `config.json`（替换 `token_file` 路径为 `/var/lib/monitoring/.futu/token.json`），再以 `su - monitoring -c "futu auth login"` 独立登录生成专属 `token.json`。
6. **配置好 `futu` 后拉取仍走了 `[cboe]`**
   - 原因：`.env` 中保留了旧的 `FUTU_CLI=/root/.futu/bin/futu`，`monitoring` 无法访问 `/root`，`_futu_bin()` 返回 `None` 静默跳过。
   - 处理：将 `.env` 中的 `FUTU_CLI` 改为 `/usr/local/bin/futu` 并重启 `monitoring-api`。
7. **兜底链路中 SPX 落后 1 个交易日**
   - 原因：原 SPX 兜底顺序为 `Futu → FRED → Stooq → Yahoo`，FRED 的 `SP500` 天然晚 1 个工作日，触发了新上线的新鲜度告警。
   - 处理：调整为 `Futu → Yahoo (curl_cffi) → Stooq → FRED`，确保无 Futu 时 SPX 也能拿到当日收盘与盘中高低。

## 5. 最终验收结果（2026-11-26）

- `monitoring-api.service` 以 `User=monitoring` 正常运行，`/health` 返回 `{"ok": true}`。
- `push_data.sh` 冒烟测试输出：
  - `vix`（`L 27.49 / H 34.95`）、`vvix`（`L 114.49 / H 131.48`）、`skew`、`spx` 均走 `[futu]`（`2026-11-25`）；
  - `term` 走 `[cboe]`（`date_mismatch: False`），`move` 走 `[yahoo_cffi]`，`hy_oas` + `ig` / `ccc` 走 `[fred_api]`；
  - `expected close: 2026-11-25; freshness issues: none`，综合压力分 `93/100`；
  - `FETCH_OK` + `PUSH_OK`，成功提交并推送至 `origin/main`（`6aecce5`）。

## 6. 日常运维常用命令

```bash
# 查看 API 服务状态与实时日志
systemctl status monitoring-api --no-pager
journalctl -u monitoring-api -n 50 --no-pager

# 查看定时拉取日志与 cron 配置
tail -n 60 /var/log/monitoring-metrics/fetch.log
crontab -u monitoring -l

# 手动以 monitoring 用户拉取并发布一次
su - monitoring -c "cd /opt/monitoring-metrics && /bin/bash ./scripts/push_data.sh"

# 如未来 Futu OAuth token 失效，重新登录
su - monitoring -c "futu auth login"
```

---

*本文仅记录运维架构、命令与排障过程，不含任何密钥、Token 或 Webhook 地址。*
