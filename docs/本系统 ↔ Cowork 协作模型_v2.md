# 本系统 ↔ Cowork 协作模型 · v2.0

> ⚠️ **2026-06-03 更新 · Cowork 侧改走 HTTP-first（方案A）**
> 本文档的「4 姿势远程版」假设 Cowork 能 SMB 写 `Z:` + Tailscale SSH 跳转，**实测在 Cowork sandbox 里三样（网络盘 / Tailscale / PowerShell）全不可达**。Cowork 实际只有两条通道：① 本地挂载盘 ② 公网 HTTPS。
> **Cowork 与引擎的协作现以公网 admin 端点（`https://ingest.taskon.xyz/admin/*`）+ `scripts/tk.sh` 为准**，详见 [`HTTP-first_方案A_部署与13步映射_v1.md`](HTTP-first_方案A_部署与13步映射_v1.md)。本文档的 SMB/SSH 姿势仅适用于真实 Windows shell 手动驱动场景。

> **本版本对应迁移**：2026-06-02 引擎搬到独立笔记本（Tailscale 主机 `engine` · 100.77.191.62），主笔记本 Cowork 通过 Tailscale + Cloudflare Tunnel 远程调用。
>
> **跟 v1.1 的根本区别**：v1.1 假设 engine docker 跑在主笔记本本地，所有接触面（文件系统 / SQLite / docker exec）走 localhost；v2.0 把 engine 全部物理迁到引擎机，主笔记本只跑 Cowork 客户端 ——所有交互必须经过网络（HTTP + SSH + SMB）。
>
> **保持不变**：5 个动词分工、4 种姿势的抽象层、Flow A / Flow B 双流程节奏、4 条红线、角色 × 动作矩阵。只是"姿势的物理实现"全变了。
>
> **配套文档**：
> - [全流程操作手册_v3.md](全流程操作手册_v3.md) —— 跟本文件同时升级，13 步全部远程化的命令
> - [V1.1_Donald笔记本_验证清单.md](V1.1_Donald笔记本_验证清单.md) —— 迁移后端到端验证 10 步
> - [本系统 ↔ Cowork 协作模型.md](本系统 ↔ Cowork 协作模型.md) —— v1.1 原版（本机部署版本，仅作历史归档）

---

## 1 · 物理拓扑（v2 新增）

```
公网用户
  │
  ├─ https://taskon.xyz/free-diagnostic       (Cloudflare Pages 落地页)
  │                ↓ form POST
  ├─ https://ingest.taskon.xyz/api/...        (CF Tunnel → 引擎机 ingestion 5052)
  ├─ https://l.taskon.xyz/<slug>              (CF Tunnel → 引擎机 shlink 8085)
  │
  ▼
Cloudflare Tunnel (cloudflared 在引擎机 24/7 running)
  │
  ▼
┌──────────────────────────────────────────────────────────────┐
│  引擎笔记本 (Tailscale: engine / 100.77.191.62)              │
│  ────────────────────────────────────────────                │
│  · TaskOn engine    :5051   (docker)                         │
│  · TaskOn ingestion :5052   (docker)                         │
│  · 内嵌 Shlink      :8085   (docker)                         │
│  · 内嵌 PG / Redis  (docker · taskon stack)                 │
│  · Postiz fork      :4007   (含 local-twitter)              │
│  · MPT fork         :8090   (含 8 commits + mcp server)     │
│  · runtime/drafts/  ← SQLite state.db + 文件契约            │
└──────────────────────────────────────────────────────────────┘
              ▲
              │ Tailscale 私网 (100.x.x.x · 端到端加密)
              │ (可选)Windows SMB 共享 \\engine\runtime\
              │ (可选)Tailscale SSH `ssh donald@engine`
              ▼
┌──────────────────────────────────────────────────────────────┐
│  主笔记本 (Tailscale: givenchy / 100.103.29.23)              │
│  ────────────────────────────────────────────                │
│  · Cowork (Claude Desktop) ← 驾驶舱                          │
│  · VS Code · 浏览器 · 没有 docker · 没有 engine              │
│  · 可盖盖子带走 / 关机 / 重装 ── 引擎不停                     │
└──────────────────────────────────────────────────────────────┘
```

**关键洞察**：原 v1.1 假设的"docker compose exec engine python -m jobs.xxx"在主笔记本上**直接跑不了** —— docker 不在本机。所有 engine 触达必须远程化。

---

## 2 · 职责分工（沿用 v1.1，不变）

### 2.1 · 5 个动词记 5 个系统

```
┌─────────────────────────────────────────────────────────────────┐
│  Cowork (主笔记本 Claude Desktop) · "想" 的事                    │
│    选题决策 · 起草 · 评审 · Dashboard · Newsletter 起草          │
├─────────────────────────────────────────────────────────────────┤
│  engine (引擎机 docker)            · "算" 的事                   │
│    评分 · 改写 · UTM · 数据 · 归因 · 报告 · 算法借力提醒          │
├─────────────────────────────────────────────────────────────────┤
│  Postiz (引擎机 :4007)             · "发" 的事                   │
│  MPT (引擎机 :8090)                · "剪" 的事                   │
│  Listmonk + SES (未部署)           · "寄" 的事                   │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 · 3 个角色 × 1 台引擎机（执行视角）

```
┌─────────────────────────────────────────────────────────────────┐
│  主笔记本 (Cowork)  ←→  Donald (人) ←→ 兼职女生 (人)             │
│  ───────────────────────                                         │
│   驾驶舱 · 起草 · 念读 · 排版                                     │
│                          ↕   4 种姿势 (A/B/C/D) · v2 远程版本    │
└──────────────────────────┃─────────────────────────────────────┘
                           ▼ Tailscale 私网 + CF Tunnel 公网
┌─────────────────────────────────────────────────────────────────┐
│  引擎笔记本 engine (docker)                                      │
│  ────────────────                                                │
│   16 cron jobs + 11 HTTP endpoints + 4 source adapters           │
│   永远 headless · 永远不发推 · 永远不 DM · 永远不评 LinkedIn       │
│                                                                  │
│   握手区(双向):                                                   │
│     runtime/state.db        ← 17 张表 (主笔记本通过 SMB/SSH 读)  │
│     runtime/drafts/<piece>/ ← 文件契约 (主笔记本通过 SMB 读写)   │
└─────────────────────────────────────────────────────────────────┘
```

**纪律不变**：Donald 100% 时间在 Cowork 里对话；engine 没有 UI；写库全部走 engine。

---

## 3 · 3 种接触面（v2 远程版本 · 这是本版本最大改动）

```
主笔记本 Cowork
    │
    │ ① 文件契约      ──→ \\engine\runtime\drafts\<piece_id>\*.md
    │   (SMB 远程)        runtime\*.json / config\*.yaml
    │
    │ ② SQLite 只读   ──→ \\engine\runtime\state.db (SMB · sql.js)
    │                     或 https://ingest.taskon.xyz/metrics (公网快查)
    │                     或 tailscale ssh ... sqlite3 ... (SSH 跑 query)
    │
    │ ③ 命令触发      ──→ A) HTTP: curl http://engine:5052/admin/run_*
    │                     B) SSH:  ssh donald@engine "docker compose exec ..."
    │                     C) 包装脚本: scripts\run_*.ps1（自动判定本机/远程跳转）
    ▼
引擎机 engine container (supercronic + 16 cron + 17 张表)
```

**核心纪律**：
1. Cowork 仍然**只 read state.db，never write** —— 写全走 engine job 或 ingestion `/api/...`
2. 跨网络的 docker exec 必须经 Tailscale SSH 隧道 —— **不要**用 RDP（手工操作太慢）
3. 文件契约对主笔记本来说从"本地路径"变成"SMB 路径"（`\\engine\runtime\drafts\...`）
4. 任何"远程"调用都不能假设引擎机在线；脚本必须 fallback

### 3.1 · 一次性配置（让远程调用通畅）

| 配置项 | 在哪台机器 | 干什么 | 状态 |
|---|---|---|---|
| Tailscale 装 + 同账号登 | 主笔记本 + 引擎机 | 组私网 | ✅ 已完成（2026-05-29 验证 ping engine 通） |
| Tailscale MagicDNS | Tailscale admin 后台 | `engine` 主机名解析 | ✅ 已完成 |
| Tailscale SSH 启用 | Tailscale admin 后台 + 引擎机 ACL | `ssh donald@engine` 可用 | ⚠️ 待 Donald 启用（无需装 OpenSSH） |
| SMB 共享 `runtime/` | 引擎机 | 主笔记本能 `\\engine\runtime` 读写 | ⚠️ 待配置 |
| Cloudflare Tunnel | 引擎机 | `ingest.taskon.xyz` + `l.taskon.xyz` 公网入口 | ✅ 已完成 |
| 主笔记本 cloudflared service | 主笔记本 | **必须 Stopped + Disabled** 防抢路由 | ✅ 已完成（2026-05-29） |

#### SMB 共享配置（一次性 · 引擎机管理员 PowerShell）

```powershell
# 引擎机上：开启 SMB 服务（默认已开）
Get-SmbServerConfiguration

# 共享 runtime 目录（按需配只读还是读写 · 这里给主笔记本读写）
$path = "D:\engine-host\taskon\marketing\engine\runtime"
New-SmbShare -Name "runtime" -Path $path -FullAccess "iamdonaldtang@gmail.com" -Description "TaskOn engine runtime · for Cowork"
# 或 -ReadAccess 只读

# 验证
Get-SmbShare -Name "runtime"
```

主笔记本验证：

```powershell
# 主笔记本上
net use Z: \\engine\runtime /persistent:yes
# 或资源管理器地址栏: \\engine\runtime\
ls Z:\drafts
```

#### Tailscale SSH 启用（一次性 · Tailscale admin 后台）

1. 打开 https://login.tailscale.com/admin/acls
2. 在 ACL 里加（如果没有的话）：
   ```json
   {
     "ssh": [{
       "action": "accept",
       "src":    ["autogroup:member"],
       "dst":    ["autogroup:self"],
       "users":  ["donald", "root", "autogroup:nonroot"]
     }]
   }
   ```
3. 引擎机上跑：`tailscale set --ssh`
4. 主笔记本验证：`ssh donald@engine "hostname"`

---

## 4 · 4 种 Cowork "开" engine 姿势（v2 远程版本）

| 姿势 | v1.1 (本机) | **v2.0 (远程)** | 典型场景 |
|---|---|---|---|
| **A · 文件契约** | Cowork 直读 `runtime/drafts/<piece>/` | Cowork 通过 SMB 读 `\\engine\runtime\drafts\<piece>\` | 起 `xthread_final.md` · 念 `voice_report.md` |
| **B · SQLite 直读** | Cowork artifact 嵌 sql.js 读本地 state.db | (优选) Cowork curl `https://ingest.taskon.xyz/metrics`（轻量指标）<br>(备用) artifact 通过 SMB 路径读 `\\engine\runtime\state.db` | dashboard widget · K1-K11 指标 · 4 模型归因表 |
| **C · ingestion HTTP** | `curl http://127.0.0.1:5051/...` | `curl http://engine:5051/health`（Tailscale 内网）<br>`curl https://ingest.taskon.xyz/admin/...`（公网带 Bearer） | `/health` 状态 · `/admin/run_publish` 紧急排程 · `/metrics` 快查 |
| **D · engine CLI** | `docker compose exec engine python -m jobs.xxx` | `ssh donald@engine "cd D:/engine-host/taskon/engine && docker compose exec -T engine python -m jobs.xxx"`<br>(未来) `scripts\run_xxx.ps1` 透明跳转 | 跑 voice_checker / schedule_planner / custom_slice_generator / kol_relation_tracker |

写库永远走 engine（让 state.db 单一真相），读两边都行。

### 4.1 · 4 姿势对应实战（v2 远程实例）

**姿势 A · 远程文件扔稿 + 触发 adapter**

```
1. Cowork 起完稿 → Write 工具直接写 Z:\drafts\20260602-01\xthread_final.md (或 \\engine\runtime\drafts\...)
2. Donald 说"跑下 adapter"
3. Cowork bash 工具:
   ssh donald@engine `
     "cd D:/engine-host/taskon/engine && \
      docker compose exec -T engine python -m jobs.adapter_orchestrator --piece-id 20260602-01"
4. Cowork 通过 SMB 读回 \\engine\runtime\drafts\20260602-01\linkedin_post.md 念给 Donald
```

**姿势 B · Cowork artifact 直读 SQLite（最大变动）**

v1.1 走本地路径，artifact 通过 fetch 拉 `state.db` 文件渲染指标卡。v2.0 因为 state.db 在远程，**有 3 条路**：

- 路径 1（轻量）：Cowork artifact 调 `https://ingest.taskon.xyz/metrics` 拿 Prometheus 指标（如 `taskon_leads_total 11`），覆盖 K1-K11 里能用 metrics 拿到的 70%
- 路径 2（完整）：Cowork artifact 走 `file:///` 或 fetch SMB 路径（要 CORS 配置），从 `\\engine\runtime\state.db` 拉
- 路径 3（最稳）：在 ingestion 新增 `/admin/sqlite/query` endpoint（只读、Bearer 鉴权），Cowork 发 SQL 拿 JSON。**推荐这条 · 未来 ship**

当前阶段建议：**用路径 1（公网 metrics）覆盖 80% 看板需求，剩下深度查询走 SSH 跳转**。

**姿势 C · Cowork 调 ingestion `/metrics` + `/admin/*`（最简单）**

```bash
# 主笔记本 Cowork bash · 完全不依赖 Tailscale/SSH/SMB
curl https://ingest.taskon.xyz/metrics                       # 公网快查
curl http://engine:5051/health                                # Tailscale 内网（不走公网）
curl -X POST https://ingest.taskon.xyz/admin/run_publish \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"piece_id":"20260602-01","platforms":"yt_shorts,linkedin_post","offset_minutes":10}'
```

这是 v2 下**最稳的姿势** —— 不依赖 SMB/SSH，只用 HTTP。**优先用 C，C 不能满足才上 D**。

**姿势 D · Cowork 让 Tailscale SSH 跑某个 job**

```powershell
# 主笔记本 Cowork bash
ssh donald@engine "cd D:/engine-host/taskon/engine && docker compose exec -T engine python -m jobs.weekly_reporter --week 2026W22"
```

后续可手触发的 job（同 v1.1）：`topic_ranker` / `performance_analyzer` / `kol_watch` / `update_btouch` / `cohort_analysis` / `ab_aggregator` / `channel_attribution` / `custom_slice_generator` / `kol_relation_tracker log-dm`

---

## 5 · 4 个 PowerShell 一行命令（v2 远程改造）

v1.1 假设这 4 个脚本本机跑 docker。v2 需要它们**自动判定本机/远程**：

| 阶段 | v2 命令 | 干嘛 |
|---|---|---|
| 选题 | `.\scripts\run_select.ps1 2026W22` | 跑 kol_watch + topic_ranker，**自动 SSH 跳转引擎机执行**，出 selection_2026W22.md |
| 生产 | `.\scripts\run_produce.ps1 20260602-01` | 跑 adapter + voice_checker，出 4 平台稿 |
| 发布 | `.\scripts\run_publish.ps1 20260602-01` | 跑 utm_generator + schedule_planner |
| 健康 | `.\scripts\run_health.ps1` | 看引擎机 docker ps + heartbeat + publish_failures |

**改造方向**（V1.1 §11"这次迁移没做但要补的事"已规划）：

```powershell
# scripts/run_health.ps1 (改造后伪代码)
param([switch]$Local)

if ($Local -or (Test-DockerRunning)) {
    # 本机有 docker · 直接跑
    docker compose ps
    docker compose exec -T engine python -c "from lib.db import db; ..."
} else {
    # 远程模式 · SSH 跳转
    ssh donald@engine "cd D:/engine-host/taskon/engine && docker compose ps"
    ssh donald@engine "cd D:/engine-host/taskon/engine && docker compose exec -T engine python -c '...'"
}
```

**当前临时方案**：直接在脚本顶部加一行 `Set-Variable -Name SSH_PREFIX -Value 'ssh donald@engine'` 然后所有 `docker compose ...` 改成 `& $SSH_PREFIX "docker compose ..."`。

详见 [全流程操作手册_v3.md §0.5](全流程操作手册_v3.md)。

---

## 6 · 主流程（沿用 v1.1 § 4-§ 6 · 不变）

Flow A（一日制 · 默认 · ≥ 80% piece · 3.5-5h ship 一条）和 Flow B（多日制 · ≤ 20% piece · 深耕内容）的 13 步关键路径、6 平台错峰策略、5-人 Reply 队伍、KOL Custom Slice DM 流程 —— **物理流程完全不变**。

变的只是**每一步的命令实现**：所有 `docker compose exec` 加 Tailscale SSH 前缀 / 改 HTTP 调用 / 改 SMB 文件路径。

详细远程命令在 [全流程操作手册_v3.md](全流程操作手册_v3.md) 步骤 0-13 每一步给出。

---

## 7 · 接外部 4 系统（v2 远程调整）

```
Cowork (主笔记本) ──→ engine (引擎机)  ──→ MPT       (引擎机 :8090 · 视频)
                                       ──→ Postiz    (引擎机 :4007 · 发布)
                                       ──→ Listmonk  (未部署 · Newsletter)
                                       ──→ shlink    (引擎机 :8085 · 公网 l.taskon.xyz)
```

Cowork 调 `mpt-video` skill 现在要**指向引擎机的 MPT API**（`http://engine:8090`，不再是 `http://localhost:8090`）。v1.1 假设 MPT 在本机 —— 主笔记本上 MPT skill 用户配置要更新 endpoint。

实际配置：

```
# Cowork mpt-video skill 配置 (主笔记本)
MPT_BASE_URL = http://engine:8090     # Tailscale 内网
# 或 https://mpt.taskon.xyz           # 如果将来公网暴露
```

---

## 8 · 谁做什么（角色 × 动作矩阵 · v1.1 沿用 · 仅"在哪跑"列新增）

| 动作 | engine | Cowork | Donald ★ | 兼职女生 | **在哪跑（v2 新增）** |
|---|---|---|---|---|---|
| 拉信号源 | ✅ kol_watch / metrics_collector | crypto-news-aggregator skill | — | — | **engine cron · 在引擎机** |
| 候选评分 | ✅ topic_ranker | — | — | — | **engine · 在引擎机** |
| 选 2-3 条 | 给 Top10 | 念 + 排版 | ★ 拍板 | — | Cowork 在主笔记本 |
| 起 X Thread 主稿 | ❌ 不起 | 主稿 (crypto-twitter-creator) | — | 主跑 | Cowork 主笔记本 · 文件写 SMB |
| 4 平台 fan-out | ✅ adapter_orchestrator | — | — | 触发 D 姿势 | **SSH 跳转引擎机执行** |
| 14 禁词 + voice_checker | ✅ | — | — | — | **engine 引擎机** |
| 10 维 50 分评分 | ❌ | ✅ taskon-content-critic | — | — | Cowork 主笔记本 |
| **数据关 + 可操作关** | ❌ | 念报告 | ★ 自核 | — | Donald 浏览器 / Cowork |
| UTM 短链 | ✅ utm_generator + shlink | — | — | — | **engine 引擎机 + shlink 引擎机** |
| YT 元数据 | LLM fallback | Cowork 起 | YT Studio 设 | 起草 | Cowork 主笔记本 |
| 6 平台排程 | ✅ schedule_planner | — | — | — | **engine 引擎机** |
| MPT 渲染 | ✅ async | — | — | — | **MPT 引擎机** |
| 真发 | ❌ engine 不发推 | ❌ | — | Postiz 自动 | **Postiz 引擎机** |
| ★ 5-人 Reply 队伍 | T-05 Lark | (无 skill) | ★ Donald + 4 BD | — | 主笔记本 + 手机 |
| ★ LinkedIn 回评 | T-07 Lark | (无 skill) | ★ Donald | — | 主笔记本 + 手机 |
| ★ KOL DM 手发 | T-02 草稿 | 念草稿 | ★ Donald | (Canva) | 主笔记本 + 手机 |
| KOL 关系跟踪 | log-dm + scan | D 姿势 | ★ log-dm | — | **SSH 跳转引擎机执行** |
| 数据回流 + 归因 | ✅ | dashboard 念 | 看 | — | **engine cron 引擎机** |

---

## 9 · 4 条红线（沿用 v1.1 · 永不松动）

❌ engine 不起 X 主稿 —— `crypto-twitter-creator` skill 在 Cowork 独占
❌ engine 不打分评审 —— `taskon-content-critic` plugin 在 Cowork 独占
❌ engine 不自动发推 / 自动 DM / 自动评 LinkedIn —— B1 §6 红线
❌ engine 不改业务数字 —— Fact-Check 失败必删段

**v2 新增红线**：
❌ **主笔记本永不再跑 docker** —— 旧 docker 服务必须永久 Stopped + Disabled，否则会跟引擎机抢端口/资源
❌ **主笔记本永不再跑 cloudflared** —— 同样会抢 CF Tunnel 路由（2026-05-29 已确认禁用）

---

## 10 · v2 故障排查（新增）

| 症状 | 排查 |
|---|---|
| Cowork bash `docker compose exec` 报"command not found" | 本地 docker 已经停掉，必须 SSH 跳转。改用 `ssh donald@engine "docker compose ..."` |
| `\\engine\runtime` 访问被拒 | 引擎机 SMB 共享没配 / Windows 凭据未保存。重跑 §3.1 SMB 配置 |
| `tailscale ssh` 失败 "no SSH" | Tailscale ACL 没启用 SSH。回 §3.1 配 ACL |
| 主笔记本 `ping engine` 不通 | 两边 Tailscale 没登同账号 / MagicDNS 没开 |
| `curl https://ingest.taskon.xyz/metrics` 502 | 引擎机 ingestion 容器没起。SSH 跳转 `docker compose up -d ingestion` |
| Cowork artifact 读不到 state.db | SMB 路径权限 / artifact 浏览器环境不支持 SMB。改走 metrics endpoint |
| MPT 调用超时 | endpoint 还指着 localhost:8090 而不是 engine:8090。改 skill 配置 |

---

## 11 · 一句话总结（v2）

**Cowork 在主笔记本，engine 在引擎机，物理隔离 + Tailscale 私网链接** —— Cowork 通过 ① SMB 读写文件契约、② HTTP 拿轻量指标 / 触发 admin endpoint、③ Tailscale SSH 跳转跑 docker CLI 三种手段远程操纵 engine。主笔记本随时可以盖盖子带走 / 关机 / 重装，引擎不停。

**默认走 Flow A 一日制**（沿用 v1.1）。**v2 唯一不同**是所有 engine 触达从"本机 localhost"变成"Tailscale `engine:xxxx` + CF Tunnel 公网 + SSH 跳转"。命令前缀变了，业务流程不变。

---

## 12 · 想再深入看哪段

- **v2 各步骤的具体远程命令** → [全流程操作手册_v3.md](全流程操作手册_v3.md)
- **迁移端到端验证** → [V1.1_Donald笔记本_验证清单.md](V1.1_Donald笔记本_验证清单.md)
- **引擎机部署 SOP** → `D:\TaskOn\infra\engine-laptop-setup.md`
- **主笔记本切换 SOP** → `D:\TaskOn\infra\main-laptop-migration.md`
- **18-day MIME bug 复盘** → 待 Donald 出 `D:\TaskOn\infra\landing_mime_bug_postmortem.md`
- **v1.1 历史版本（本机部署）** → [本系统 ↔ Cowork 协作模型.md](本系统 ↔ Cowork 协作模型.md)

---

## 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-21 | 首版。10 步生命周期 + 3 接触面 + 4 姿势 + 4 红线 |
| v1.1 | 2026-05-21 | 重大重构：常态默认"周节奏"→"效果最大化前提下最短可行时间"。拆 Flow A / Flow B 双流程 |
| **v2.0** | **2026-06-02** | **重大架构升级**：引擎物理迁移到独立笔记本（Tailscale 主机 `engine`）。1) 新增 §1 物理拓扑图（公网/Tailscale 私网/CF Tunnel）2) §3 三种接触面全部远程化（SMB / HTTP / SSH 跳转）3) §3.1 新增一次性配置清单（Tailscale SSH 启用 / SMB 共享 / cloudflared 主机分工）4) §4 4 姿势 v2 远程版本对照表 5) §5 PowerShell 脚本远程改造路径 6) §7 MPT 等外部系统 endpoint 改 `engine:xxxx` 7) §8 角色矩阵新增"在哪跑"列 8) §9 v2 新增红线"主笔记本永不再跑 docker / cloudflared"9) §10 v2 故障排查表。流程逻辑（Flow A/B · 角色分工 · 红线）完全沿用 v1.1，仅"姿势的物理实现"全变。配合 2026-05-29 迁移完成 + 2026-06-02 端到端 zip 修复 + leads 落库验证。 |
