# V1 部署架构 · 本地 4 Docker + CF Tunnel 暴露

> **本文档定位**：V1 阶段（短期 1-2 周）整体部署架构 + CF Tunnel 完整配置 + 桌面 24/7 稳定性清单。
>
> **V1 决策（Donald 2026-05-15 拍板）**：engine + shlink + Postiz + MPT 4 个 docker 都在 Donald 本地桌面，通过 CF Tunnel 暴露 3 个公网域名让落地页 / 用户能访问到本地服务。**不迁服务器**。
>
> **V2 触发条件**：见本文 §6 「何时升级到 V2 · 触发条件」
>
> **关联**：
> - 上层 PRD：`D:\Taskon\marketing\00_内容营销引擎\全流程规划_v3\PRD_前后端集成需求_W1精简版.md`
> - 落地页部署：`engine/landing_pages/free-diagnostic/DEPLOY_CF_PAGES.md`

---

## 1 · V1 架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                          公网用户                                │
│   X / LinkedIn / YouTube 推文 CTA 短链 → 落地页 → 表单留资        │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                ┌───────────▼──────────────┐
                │   Cloudflare Edge        │
                │   (taskon.xyz DNS 托管)  │
                └─────┬────────┬──────────┬┘
                      │        │          │
        ┌─────────────▼───┐ ┌──▼────────┐ │
        │ taskon.xyz/     │ │l.taskon   │ │
        │ free-diagnostic │ │.xyz/<slug>│ │
        │ (CF Pages 路由) │ │           │ │
        └────────┬────────┘ └────┬──────┘ │
                 │               │        │
                 │              CF Tunnel │
                 │               │        │
                 │  ┌────────────┘        │
                 │  │                     │
                 │  │  ingest.taskon.xyz  │
                 │  │  (CF Tunnel) ←──────┘
                 │  │       │
                 │  │       │  postiz.taskon.xyz
                 │  │       │  (CF Tunnel) ★ Postiz 已存在
                 │  │       │       │
┌────────────────▼──▼───────▼───────▼──────────────────┐
│  Donald 桌面 (Windows · 必须 24/7 在线)              │
│                                                       │
│  ┌─────────────────────────────────────────────────┐ │
│  │  cloudflared (Windows service · 自启)            │ │
│  │  ├─ ingest.taskon.xyz → localhost:5051          │ │
│  │  ├─ l.taskon.xyz → localhost:8085               │ │
│  │  └─ postiz.taskon.xyz → localhost:4007          │ │
│  └──────────┬────────────┬───────────┬─────────────┘ │
│             │            │           │               │
│  ┌──────────▼─┐  ┌───────▼────┐ ┌────▼─────────────┐ │
│  │ engine     │  │ shlink     │ │ Postiz           │ │
│  │ :5051      │  │ :8085      │ │ :4007            │ │
│  │ (ingestion)│  │            │ │                  │ │
│  │            │  │            │ │                  │ │
│  │ + cron 18  │  │            │ │                  │ │
│  │ + state.db │  │            │ │                  │ │
│  └────────────┘  └────────────┘ └──────────────────┘ │
│                                                       │
│  ┌─────────────────────┐                              │
│  │ MoneyPrinterTurbo   │ ← 按需调用 · 不需 24/7        │
│  │ :8090               │                              │
│  └─────────────────────┘                              │
│                                                       │
│  ┌─────────────────────┐                              │
│  │ Cowork (Claude      │ ← 你的工作界面                │
│  │ Desktop)            │   docker compose exec 调本地  │
│  └─────────────────────┘                              │
└───────────────────────────────────────────────────────┘
```

---

## 2 · CF Tunnel 完整配置

### 2.1 · 前置 · 你已有的（Postiz 已经在用 CF Tunnel）

```powershell
# 检查 cloudflared 是否已装
cloudflared --version

# 查看现有 tunnel
cloudflared tunnel list

# 查看现有 config.yml
type C:\Users\<you>\.cloudflared\config.yml
```

如果上面都有输出且 Postiz 在工作 → 你已经会了。直接跳到 §2.3 加新规则。

### 2.2 · 全新安装（如果上面没有）

```powershell
# 下载安装 cloudflared
winget install Cloudflare.cloudflared
# 或从 https://github.com/cloudflare/cloudflared/releases 下载 .msi

# 登录 CF 账号（会打开浏览器授权）
cloudflared tunnel login

# 创建新 tunnel
cloudflared tunnel create taskon-engine

# 复制 tunnel UUID（终端会打印）
# 例: 12345678-abcd-ef00-1234-567890abcdef
```

### 2.3 · 配置 `~/.cloudflared/config.yml` · 加 2 条新规则

```yaml
# C:\Users\<you>\.cloudflared\config.yml
tunnel: <existing-tunnel-uuid>
credentials-file: C:\Users\<you>\.cloudflared\<existing-tunnel-uuid>.json

ingress:
  # ★ 现有 · Postiz
  - hostname: postiz.taskon.xyz
    service: http://localhost:4007

  # ★ V1 新加 · engine ingestion
  - hostname: ingest.taskon.xyz
    service: http://localhost:5051
    originRequest:
      noTLSVerify: true
      connectTimeout: 30s

  # ★ V1 新加 · shlink 短链
  - hostname: l.taskon.xyz
    service: http://localhost:8085
    originRequest:
      noTLSVerify: true
      connectTimeout: 10s

  # 必须的 catch-all
  - service: http_status:404
```

### 2.4 · CF DNS 添加 CNAME

在 CF 后台 → taskon.xyz → DNS → Records 添加 2 条：

| Type | Name | Target | Proxy status |
|---|---|---|---|
| CNAME | `ingest` | `<tunnel-uuid>.cfargotunnel.com` | ✅ Proxied (orange cloud) |
| CNAME | `l` | `<tunnel-uuid>.cfargotunnel.com` | ✅ Proxied (orange cloud) |

或者用 cloudflared CLI 自动加：

```powershell
cloudflared tunnel route dns <tunnel-name> ingest.taskon.xyz
cloudflared tunnel route dns <tunnel-name> l.taskon.xyz
```

### 2.5 · 重启 tunnel 让配置生效

```powershell
# 如果 cloudflared 跑在 Windows 服务（推荐）:
Restart-Service cloudflared

# 如果跑在前台:
# 先 Ctrl+C 杀掉旧的，再
cloudflared tunnel run <tunnel-name>
```

### 2.6 · 验证

```powershell
# engine 健康检查
curl https://ingest.taskon.xyz/health
# 期望: 200 + {"status":"ok"}

# shlink 健康检查
curl https://l.taskon.xyz/rest/v3/health
# 期望: 200

# 测 ingestion impression POST
curl -X POST https://ingest.taskon.xyz/api/landing-signup `
  -H "Content-Type: application/json" `
  -d '{
    "impression_only": true,
    "cookie_id": "test-uuid-001",
    "page_path": "/free-diagnostic",
    "url": "https://taskon.xyz/free-diagnostic?utm_source=test"
  }'
# 期望: 201 + {"mode":"impression","status":"ok"}
```

---

## 3 · engine ingestion 必改 · 加 form_data + CORS

### 3.1 · 当前 ingestion schema 找出

```powershell
cd D:\Taskon\marketing\engine
findstr /S /N "landing-signup" ingestion\*.py
findstr /S /N "BaseModel" ingestion\*.py
```

### 3.2 · 改 schema · 加 form_data 字段

```python
# engine/ingestion/landing_signup.py (或类似)
from pydantic import BaseModel, EmailStr
from typing import Optional, Dict, Any

class LandingSignupBody(BaseModel):
    # impression OR signup 两种模式
    impression_only: bool = False

    # signup 模式
    email: Optional[EmailStr] = None

    # 通用字段
    cookie_id: str
    page_path: str
    url: str
    referrer: Optional[str] = None
    user_agent: Optional[str] = None
    timestamp: Optional[str] = None

    # ★ V1 新加 · 接 telegram_handle + project_url
    form_data: Optional[Dict[str, Any]] = None
```

### 3.3 · 处理逻辑 · 把 form_data 写到 leads 表

选项 A · 写到 `leads.metadata` JSON 列（最简）：

```python
async def handle_signup(body: LandingSignupBody):
    # ... 现有逻辑

    if body.email and not body.impression_only:
        # 写 leads 表
        metadata = body.form_data or {}
        await db.leads.insert(
            email_hash=hash_email(body.email),
            email=body.email,
            telegram_handle=metadata.get("telegram_handle"),
            project_url=metadata.get("project_url"),
            metadata_json=json.dumps(metadata),
            # ... 现有 UTM 字段
        )

        # Lark 通知 BD
        await lark.alert(
            "P1",
            f"[New Lead] {body.email}",
            {
                "email": body.email,
                "telegram": metadata.get("telegram_handle"),
                "project": metadata.get("project_url"),
                "utm_source": utm.source,
                "utm_campaign": utm.campaign,
            }
        )
```

选项 B · 新增 leads.telegram_handle / leads.project_url 列（schema 升级）：

```sql
ALTER TABLE leads ADD COLUMN telegram_handle TEXT;
ALTER TABLE leads ADD COLUMN project_url TEXT;
```

**推荐选项 A**（避免 schema migration）。

### 3.4 · CORS middleware

```python
# engine/ingestion/main.py
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://taskon.xyz"],  # ★ 不要用 *
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Requested-With"],
    allow_credentials=False,
    max_age=3600,
)
```

### 3.5 · 重启 engine

```powershell
cd D:\Taskon\marketing\engine
docker compose restart ingestion
# 或全部:
docker compose restart engine ingestion
```

---

## 4 · 桌面 24/7 稳定性清单

V1 本地架构的最大风险：**Donald 桌面 / 网络 / 进程不稳定 = 整个内容营销引擎断**。下面 8 条降低风险：

### 4.1 · 电源管理

Windows 设置 → 电源 → 选 "高性能" 计划：
- 屏幕关闭后：从不
- 睡眠：从不
- 硬盘关闭：从不

**关键**：笔记本插电用，**不要拔电源**。如果 Donald 桌面是台式机 → 加 UPS（不间断电源 · ~¥300）防跳闸。

### 4.2 · 网络稳定

- 优先有线（千兆网线）· 不用 WiFi
- 备用 4G 热点（如果有线挂了自动切换 · 路由器或 Windows 系统能配）
- 静态 IP（如果是家庭宽带）防止 IP 变化中断 tunnel
- CF Tunnel 自动断线重连（cloudflared 默认行为）

### 4.3 · Docker Desktop 自启

```
Windows 设置 → 应用 → 启动 → 启用 "Docker Desktop"
```

启动时 docker compose stack 自动起：

```powershell
# 在 engine 根目录创建 start-engine.bat
@echo off
cd /d D:\Taskon\marketing\engine
docker compose up -d
```

Win + R → `shell:startup` → 把 start-engine.bat 拖进去。

### 4.4 · cloudflared 注册为 Windows 服务

```powershell
# 让 cloudflared 开机自启 + 后台跑
cloudflared service install
sc start cloudflared
```

验证：

```powershell
Get-Service cloudflared
# 期望: Running
```

### 4.5 · 关键容器 restart policy

`docker-compose.yml` 加 `restart: unless-stopped`：

```yaml
services:
  engine:
    restart: unless-stopped
  ingestion:
    restart: unless-stopped
  postiz:
    restart: unless-stopped
  shlink:
    restart: unless-stopped
```

挂掉自动重启。

### 4.6 · 监控告警 · Lark Webhook

engine 已实现 Lark P0/P1/P2 告警。**关键**：在你 Lark 群里**手机推送开启**，发生：

| 严重度 | 触发 |
|---|---|
| P0 | engine SQLite 不可达 / 余额耗尽 / 触达跌穿底 |
| P1 | LLM 全失败 / 某 cron 失败 / Postiz API 调用挂 |
| P2 | 单平台采集失败 / 候选数少 |

P0 / P1 必须**手机推送 + 震动**。

补充建议加一条 P0 监控：cloudflared 进程挂了。可以用一个简单 Powershell scheduled task：

```powershell
# 每 5 分钟检查 cloudflared 服务
Register-ScheduledJob -Name "CheckCloudflared" -ScriptBlock {
  if ((Get-Service cloudflared).Status -ne "Running") {
    Start-Service cloudflared
    # 发 Lark 告警
    Invoke-RestMethod -Uri $env:LARK_WEBHOOK_URL -Method Post -Body '{"msg_type":"text","content":{"text":"[P0] cloudflared was down, auto-restarted"}}'
  }
} -Trigger (New-JobTrigger -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration ([TimeSpan]::MaxValue) -At (Get-Date))
```

### 4.7 · 远程访问能力（你不在桌面前时）

如果你周末 / 出差不在桌面前，桌面挂了怎么救？

- 装 **TeamViewer / AnyDesk / RustDesk**（远程桌面）
- 装 **Tailscale**（你能 SSH 进桌面 from 任何地方）
- 桌面装 SSH server（Windows 10/11 自带 OpenSSH）

### 4.8 · 备份

engine SQLite **本地备份**：

```yaml
# engine/docker-compose.yml
volumes:
  engine_runtime:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: D:\Taskon\marketing\engine\runtime
```

`runtime/state.db` 在硬盘上，每天 23:00 自动备份 `runtime/backups/state-<ts>.db`（engine 已实现）。

**额外** · 每周一次手动备到 OneDrive / 移动硬盘：

```powershell
# scripts/backup-weekly.ps1
$today = Get-Date -Format "yyyy-MM-dd"
Copy-Item D:\Taskon\marketing\engine\runtime\state.db `
  -Destination "C:\Users\<you>\OneDrive\backups\state-$today.db"
```

---

## 5 · Cowork 调用 engine（V1 模式 · 不变）

V1 阶段 Cowork **继续直接 docker exec 本地容器**，与现在模式相同：

```powershell
docker compose exec engine python -m jobs.kol_watch --week 2026W20
docker compose exec engine python -m jobs.topic_ranker --week 2026W20
docker compose exec engine python -m jobs.metrics_collector
# ... 18 个 cron 都可手动触发
```

`scripts/run_*.ps1` 5 个脚本已写好，Cowork 一句话触发。

---

## 6 · 何时升级到 V2 · 触发条件

V1 风险可接受期 ~ 2 周。出现下列**任一情况立刻启动 V2 迁服务器**：

| 触发条件 | 描述 |
|---|---|
| 月留资 > 20 leads | V1 桌面挂掉 1 小时 = 损失估计 ~3-5 leads（值 ~$500） |
| 国家假期 / Donald 出差 ≥ 7 天 | 桌面无法保证开机 |
| 桌面挂过 1 次（>30min） | V1 验证完毕 · 该升级了 |
| 加入新团队成员调 engine | 多人调本地 docker 不现实 |
| Cowork 跨设备需求 | Donald 想从 macbook / 网页版调 |

V2 路径见**下方 §7 · V2 升级路径预览**。

---

## 7 · V2 升级路径预览

```
V2 迁移工作量: 2-3 天

[ 服务器 (VPS / 现有 taskon 服务器) ]
  ├─ engine (docker · 生产版)
  ├─ shlink (docker · 生产)
  ├─ Postiz (docker · 生产)
  ├─ 域名 DNS A 记录直接指服务器 IP（CF Tunnel 退役）
  ├─ engine 加 HTTP admin API (18 个 cron job 各 1 个 endpoint)
  └─ SSH key 给 Donald

[ 本地 Donald 桌面 ]
  ├─ MPT (本地够用 · 不上服务器)
  ├─ Cowork
  └─ git 编辑器
```

数据迁移：

```bash
# 服务器 → 本地 rsync state.db
rsync -avz admin@server:/opt/engine/runtime/state.db ./engine/runtime/state.db
# 一次性迁移历史数据，之后服务器为 master
```

Cowork 调用方式改为：

```powershell
curl -X POST https://admin.taskon.xyz/admin/trigger/kol_watch?week=2026W20 `
  -H "Authorization: Bearer $env:TASKON_ADMIN_TOKEN"
```

或直接 SSH（如果你不想加 HTTP API）：

```powershell
ssh admin@server "docker compose exec engine python -m jobs.kol_watch --week 2026W20"
```

---

## 8 · V3 升级预览（更长期）

```
V3: engine 包 MCP server，Cowork 配置 MCP endpoint
```

V3 实施工时 1-2 周（含学习曲线）。**触发条件**：
- Donald 需要跨设备一致（桌面 / 笔记本 / 出差 / 手机端 Claude.ai 网页版）
- 团队多人协作（每人本地 Cowork 都连同一个 engine MCP）
- 想给 BD 团队 / 客户做开放接口

---

## 9 · 检查清单（V1 上线必跑）

部署完成后跑下面验证 V1 整体架构：

```
基础设施:
  [ ] cloudflared Windows 服务运行中
  [ ] docker desktop 自启
  [ ] engine + ingestion + shlink + postiz 容器健康
  [ ] state.db 可读

CF Tunnel 路由:
  [ ] https://ingest.taskon.xyz/health 200 OK
  [ ] https://l.taskon.xyz/rest/v3/health 200 OK
  [ ] https://postiz.taskon.xyz 200 OK
  [ ] DNS records 在 CF 后台已配

engine 后端改造:
  [ ] ingestion schema 加 form_data 字段
  [ ] CORS Access-Control-Allow-Origin: https://taskon.xyz 配置
  [ ] 重启 engine + ingestion 容器
  [ ] curl OPTIONS preflight 测试通过

落地页:
  [ ] CF Pages 部署完成
  [ ] CF Workers Route 配 taskon.xyz/free-diagnostic*
  [ ] taskon.xyz/free-diagnostic 200 渲染正常
  [ ] DevTools impression POST 201
  [ ] 填表单提交 → leads 表写入 + Lark 通知

稳定性:
  [ ] 电源永不睡眠
  [ ] 容器 restart: unless-stopped
  [ ] cloudflared 监控脚本配置
  [ ] Lark 手机推送已开
  [ ] state.db 每周备到 OneDrive
```

---

## 10 · 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-15 | 首版 V1 部署架构 · 本地 4 docker + CF Tunnel 暴露 3 子域 · Donald 拍板 V1 跳过迁服务器 |
