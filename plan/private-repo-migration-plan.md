# GitHub 仓库转 Private 与静态托管迁移计划

> **状态：待确认并实施（2026-11-26）**。
> **核心结论**：GitHub Free 个人账号将仓库转为 Private 后，GitHub Pages 会立即停用。结合本项目已使用 Cloudflare（域名 + Tunnel）与纯静态 HTML 前端的特点，推荐**零成本迁移至 Cloudflare Pages**，并按“先建新站 → 切 DNS → 再转 Private”的顺序实现零停机切换。
> 相关文件：`CNAME`、`README.md`、`api/api_server.py`、`api/deploy.sh`、`.github/workflows/ci.yml`、`.github/workflows/freshness-watchdog.yml`。

## 1. 背景与目标

随着监控指标、分析文档、运维记录与历史数据不断积累，继续保持 `fractalcapital/monitoring-metrics` 为 Public 仓库存在代码/配置/策略暴露顾虑。目标是：

1. 将 GitHub 仓库从 **Public 转为 Private**。
2. 保持前端站点 `https://mon.infidea.app/` 与仪表盘 `https://mon.infidea.app/dashboard/` 稳定可用，且 `POST /api/refresh`、`POST /api/push` 等交互不受影响。
3. 明确并按需控制**网页与监控数据的访问边界**（区分“仅隐藏仓库代码”与“连网页/数据也仅自己可见”）。
4. 保持 VPS（`proud-pipes-7`）定时拉取、代码更新及 GitHub Actions 巡检（`freshness-watchdog.yml`）持续正常运行。

## 2. GitHub Pages 政策与关键区分

| GitHub 计划 | Private 仓库能否启用 Pages | 发布后的网页可见性 | 说明 |
| --- | --- | --- | --- |
| **GitHub Free（个人）** | **不能** | — | 转 Private 后原 Pages 站点立即停用下线 |
| **GitHub Pro（\$4/月）/ Team** | 可以 | **依然公网公开** | 仅代码仓库私有，任何知道 URL 的人仍可访问网页 |
| **GitHub Enterprise Cloud（组织）** | 可以 | 可设为私有鉴权 | 成本高，个人项目不适用 |

> **关键认知**：**仓库私有 ≠ 网页私有 ≠ 数据私有**。即使仓库转 Private，只要前端仍公网托管、且 `https://api.infidea.app/api/data` 仍公开只读，知道域名的人依然能访问页面与实时 JSON。

## 3. 本仓库转 Private 的影响面盘点

逐项核查当前代码库后，有 5 处直接受影响或需要顺手收口的点：

1. **静态页面托管失效（最高优先级）**
   - 当前通过根 `CNAME`（`mon.infidea.app`）依赖 GitHub Pages 发布。Free 账号转 Private 后站点会立即 404。
2. **根目录缺少 `index.html` 与目录暴露边界**
   - GitHub Pages（Jekyll）会自动将根 `README.md` 渲染为 `/` 首页，但 Cloudflare Pages / Nginx 不会——若直接发布仓库根目录，`https://mon.infidea.app/` 会返回 404。
   - 同时，直接把整个仓库根目录作为静态站点输出目录，会导致 `api/`、`scripts/`、`docs/`、`plan/`、`tests/` 等源码与计划文档可通过 URL 直接下载（即使仓库本身已是 Private）。
   - **处理方式**：新增轻量根 `index.html`（目录导航页或跳转到 `/dashboard/`），并加一步极简构建脚本，仅拷贝 `index.html`、`dashboard/`、`metrics/` 到输出目录（如 `dist/`）供静态托管发布。
3. **VPS 与备用机拉取代码鉴权（`api/deploy.sh`）**
   - `api/deploy.sh` 目前采用匿名 HTTPS `git clone https://github.com/fractalcapital/monitoring-metrics.git` 与 `git pull --ff-only`，且 `api/README.md` 中的快速部署命令使用 `raw.githubusercontent.com`。
   - 仓库转 Private 后，匿名 HTTPS clone/pull 与 raw 下载都会返回 404/401。
   - **处理方式**：在 VPS（以及可选的 Mac Studio）配置仅对本仓库有效的**只读 GitHub SSH Deploy Key**，并将 `/opt/monitoring-metrics` 的 `origin` 改为 SSH 地址 `git@github.com:fractalcapital/monitoring-metrics.git`。
4. **GitHub Actions 免费额度（低风险）**
   - 当前有两个 workflow：`.github/workflows/ci.yml`（push/PR 触发）与 `.github/workflows/freshness-watchdog.yml`（周二至周六 01:00 UTC 每天跑一次）。
   - Public 仓库 Actions 不限时长；转 Private 后 Free 账号享有 **2,000 分钟/月**（Linux Runner）。按当前频率每月实际消耗约 20–40 分钟，远低于上限，**无需迁移**。
5. **API CORS 与访问控制边界（`api/api_server.py`）**
   - 写端点（`POST /api/refresh`、`POST /api/push`）CORS 固定允许 `ALLOWED_ORIGIN = "https://mon.infidea.app"`；只读端点（`GET /health`、`GET /api/data`、`GET /api/alert-state`）当前公开无鉴权且 `Access-Control-Allow-Origin: *`。
   - 只要新托管继续绑定 `https://mon.infidea.app`，CORS **无需改动**；但若后续要限制外人访问网页和数据，需同步考虑 Cloudflare Access 及巡检放行（见第 6 节 Phase 5）。

## 4. 候选方案对比

| 维度 | 方案 A：Cloudflare Pages（推荐） | 方案 B：现有 VPS + Cloudflare Tunnel | 方案 C：升级 GitHub Pro | 方案 D：Vercel / Netlify |
| --- | --- | --- | --- | --- |
| **费用** | 免费（不限带宽） | 免费（复用现有 VPS） | \$4/月 | 免费（个人版） |
| **支持 GitHub Private 仓库** | 原生支持，push 自动部署 | 需 VPS 配 SSH Key + 拉取同步 | 支持 | 原生支持，push 自动部署 |
| **绑定 `mon.infidea.app`** | 极快（域名已在 Cloudflare） | 极快（Tunnel 加一条 ingress） | 无需改 DNS | 需改 DNS |
| **能否做“网页也私有”** | **可以**（免费套 Cloudflare Access，50 用户内免费） | **可以**（同样可套 Cloudflare Access） | 不可以（网页仍公开） | 免费版不支持密码/SSO 保护 |
| **可用性与运维成本** | 全球边缘 CDN，零服务器运维；即使 VPS 宕机页面与静态兜底仍可打开 | 与 VPS 单点耦合，需维护 Web 服务（Nginx/Caddy）与同步 | 零迁移成本 | 多引入一个第三方平台 |

### 推荐选择：方案 A（Cloudflare Pages）

- 域名和 Tunnel 本来就在 Cloudflare，不引入新账号/新平台。
- 与 VPS 解耦：VPS 只管后端 API 和定时拉取，即使 VPS 维护或宕机，前端页面仍可正常打开并展示兜底快照。
- 既满足当下“免费 + Private 仓库自动发布”，又保留后续一键开启 **Cloudflare Access** 实现“网页与数据全私有”的升级空间。

## 5. 实施计划（零停机顺序）

> **核心原则**：先在新平台跑通并切完域名，确认线上无异常后，**最后一步**再把 GitHub 仓库设为 Private。

### Phase 0：明确访问边界目标（先决策）

在两种安全级别中二选一（可先选级别 1，后续随时升级到级别 2）：

- **级别 1（仅代码私有，网页公开）**：仓库 Private + Cloudflare Pages 公开托管 `mon.infidea.app` + `api.infidea.app` 维持现状。
- **级别 2（代码 + 网页 + 数据全私有）**：在级别 1 基础上，启用 Cloudflare Zero Trust（Access）保护 `mon.infidea.app`（及可选 `api.infidea.app`），仅允许本人邮箱/GitHub 登录访问。

### Phase 1：整理静态发布边界（代码仓库改动）

1. **新增根入口 `index.html`**
   - 替代原 GitHub Pages 自动渲染的 `README.md`，提供简洁的指标导航首页（或直接提供跳转到 `/dashboard/` 与 6 个指标页的入口，风格与现有页面一致，支持亮/暗色）。
2. **新增极简发布打包脚本**（例如 `scripts/build_site.sh`）
   - 将需要公开的前端文件复制到 `dist/`：
     - `index.html`
     - `dashboard/index.html`、`dashboard/data.json`、`dashboard/alert_state.json`
     - `metrics/`（仅 `.html` 及必要静态资源，不复制多余脚本）
   - 确保 `api/`、`scripts/`、`alert/`、`docs/`、`plan/`、`tests/`、`.github/` **不会进入 `dist/`**，杜绝私有代码与运维文档通过静态站点泄露。
   - 在 `.gitignore` 中加入 `dist/`。
3. **补充/更新单元测试与文档**
   - 跑 `pytest` / 现有 CI 测试，确保新增脚本和根页面不破坏既有测试。

### Phase 2：准备 VPS 私有仓库拉取能力

1. 在 VPS（`proud-pipes-7`）上为 `monitoring` 用户（或部署用户）生成专用 SSH 密钥（`ed25519`）。
2. 在 GitHub 仓库 **Settings → Deploy keys** 添加该公钥，**不勾选 Allow write access**（保持最小权限，只读即可）。
3. 在 VPS `/opt/monitoring-metrics` 将 remote 改为 SSH：
   ```bash
   git -C /opt/monitoring-metrics remote set-url origin git@github.com:fractalcapital/monitoring-metrics.git
   git -C /opt/monitoring-metrics fetch --dry-run
   ```
4. 同步更新 `api/deploy.sh` 与 `api/README.md` 中的仓库克隆说明（注明私有仓库需先配置 Deploy Key 或改用 SSH URL）。
5. 若启用了 Mac Studio 备用机，同样确认本地 git remote 走 SSH。

### Phase 3：配置 Cloudflare Pages 并零停机切 DNS

1. 登录 Cloudflare Dashboard → **Workers & Pages → Create → Pages → Connect to Git**。
2. 授权 Cloudflare Pages 访问 GitHub（可仅授权 `monitoring-metrics` 单仓库，且对 Private 仓库同样生效）。
3. 配置构建参数：
   - Production branch：`main`
   - Build command：`bash scripts/build_site.sh`
   - Build output directory：`dist`
4. 首次部署完成后，用 `*.pages.dev` 预览域名验证：
   - `/` 首页正常打开（无 404）
   - `/dashboard/` 正常渲染且能拉取 `GET https://api.infidea.app/api/data`
   - `/metrics/vix/` 等 6 个指标页均可正常打开
   - 确认 `/scripts/fetch_dashboard.py`、`/plan/yahoo-429-plan.md`、`/api/api_server.py` 均返回 404（未泄露后端与文档）
   - *注：预览域名下 `POST /api/refresh` / `POST /api/push` 会被 CORS 拦截属预期行为，切到 `mon.infidea.app` 后恢复。*
5. 在 Cloudflare Pages 项目中添加自定义域名 `mon.infidea.app`：
   - 将 Cloudflare DNS 中 `mon.infidea.app` 原指向 GitHub Pages 的 CNAME/A 记录切换为 Cloudflare Pages 提供的目标 CNAME（开启代理橙云）。
   - 等待 TLS 证书签发并生效（通常 1–5 分钟）。
6. 线上域名验收：
   - 访问 `https://mon.infidea.app/` 与 `https://mon.infidea.app/dashboard/` 正常加载。
   - 在仪表盘触发一次手动刷新或测试推送，确认 `POST /api/refresh`、`POST /api/push` 在 `https://mon.infidea.app` 下 CORS 正常放行。

### Phase 4：将 GitHub 仓库转为 Private 并清理

1. 在 GitHub 仓库 **Settings → Pages** 先停用 GitHub Pages（或直接执行转 Private，GitHub 会自动解除 Pages 与自定义域名绑定）。
2. 在 GitHub 仓库 **Settings → General → Danger Zone → Change repository visibility**，切换为 **Private**。
3. 清理仓库根目录不再需要的 `CNAME` 文件（可选，建议一并删除以免误导后续维护）。
4. 更新 `README.md` 与 `api/README.md` 中关于“GitHub Pages”的描述为“Cloudflare Pages”。
5. 转 Private 后做全链路复核：
   - 推送一个小提交到 `main`，验证 Cloudflare Pages 自动触发构建与发布。
   - 验证 `.github/workflows/ci.yml` 正常执行，手动 `workflow_dispatch` 触发一次 `.github/workflows/freshness-watchdog.yml` 确认通过。
   - 在 VPS 上执行一次 `git -C /opt/monitoring-metrics pull --ff-only`，验证 SSH Deploy Key 可正常拉取私有仓库更新。

### Phase 5（可选）：若选择“级别 2 — 网页与数据全私有”

仅当希望外人也无法打开 `mon.infidea.app` 或读取 `api.infidea.app/api/data` 时执行：

1. **保护前端**：在 Cloudflare Zero Trust → Access → Applications 中添加 Self-hosted 应用 `mon.infidea.app`，策略仅允许本人邮箱（OTP 验证码）或 GitHub 账号登录。
2. **保护 API（可选）**：若同时给 `api.infidea.app` 套 Cloudflare Access：
   - 浏览器端 `dashboard/index.html` 请求 `api.infidea.app` 需携带凭证（`credentials: 'include'`）且 `api_server.py` 需返回 `Access-Control-Allow-Credentials: true` 并收敛 `Access-Control-Allow-Origin` 为 `https://mon.infidea.app`（不能为 `*`）；更简洁的方案是通过 Cloudflare Pages Functions / 同域路由把 `/api/*` 挂在 `mon.infidea.app/api/*` 下，天然同源共享 Access Cookie。
   - 为 `.github/workflows/freshness-watchdog.yml` 配置一对 **Cloudflare Access Service Token**（`CF-Access-Client-Id` / `CF-Access-Client-Secret` 存入 GitHub Secrets），并在 Access 策略中为 `/api/data` 和 `/health` 放行该 Service Token，避免外部巡检被登录页拦截。

## 6. 回滚方案

- **切换 DNS 后若新站异常**：在转 Private 之前，直接把 Cloudflare DNS 中 `mon.infidea.app` 改回 GitHub Pages 记录即可秒级回滚。
- **已转 Private 后若需回退**：若只是静态托管问题，可临时启用方案 B（VPS + Cloudflare Tunnel 托管 `dist/`），无需将仓库重新公开。

## 7. 验收标准

- [ ] GitHub 仓库可见性为 **Private**，未登录状态下访问仓库页面返回 404。
- [ ] `https://mon.infidea.app/`、`https://mon.infidea.app/dashboard/` 及 6 个 `metrics/*` 页面加载正常。
- [ ] `https://mon.infidea.app/api/api_server.py`、`https://mon.infidea.app/scripts/fetch_dashboard.py`、`https://mon.infidea.app/plan/yahoo-429-plan.md` 等非前端路径返回 404。
- [ ] 仪表盘实时数据加载正常，手动刷新与飞书/Bark 推送按钮工作正常。
- [ ] 推送新 commit 到 Private 仓库的 `main` 分支后，Cloudflare Pages 在 1–2 分钟内自动完成构建上线。
- [ ] VPS `git pull --ff-only` 通过只读 SSH Deploy Key 正常工作。
- [ ] GitHub Actions `ci.yml` 与 `freshness-watchdog.yml` 在 Private 仓库下执行通过。

## 8. 待确认事项

1. **托管方案确认**：是否采用推荐的 **方案 A（Cloudflare Pages）**？
2. **访问边界确认**：本次先做 **级别 1（仅代码私有，网页公开）**，还是一步到位做 **级别 2（加 Cloudflare Access，网页与数据也私有）**？
3. **根首页 `/` 交互偏好**：`https://mon.infidea.app/` 希望做成指标卡片导航页，还是直接自动跳转到 `/dashboard/`？

---

*本文只记录迁移计划与架构决策，不含任何密钥。Deploy Key 私钥、API Token、Cloudflare Access Service Token 等敏感信息仅保存在服务器本地或 GitHub / Cloudflare Secrets 中，绝不提交进仓库。*
