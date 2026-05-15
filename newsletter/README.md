# TaskOn Newsletter Engine

> 基于开源 [knadh/listmonk](https://github.com/knadh/listmonk) 的 Newsletter 系统
> **架构**：开发 vs 生产分离 / **生产部署在 VPS**
> SMTP relay：W1-M3 SendGrid Free → M4 切 AWS SES
> 数据归属：完全自己掌控

---

## 架构（关键 · 必读）

```
[Donald 本地 E 盘 · 开发]              [git remote]              [VPS · 生产]
E:\AILife\listmonk\                    GitHub repo                /opt/taskon-newsletter
├── (fork of knadh/listmonk)            ↑ git push                ├── Listmonk @ Docker
├── docker-compose.yml                    git pull ↓              ├── webhook_server
├── config/                                                       ├── nginx + Let's Encrypt
├── templates/                                                    ├── Postgres
└── webhook_server/                                               └── 数据卷 ./data/

  本地试改测试 → git push → SSH 登 VPS → git pull → docker compose up -d
```

```
[VPS 生产]                                    [Donald 本地 D 盘]
Listmonk 发邮件                              SQLite state.db
   ↓ 收 Open/Click/Bounce 事件               D:\Taskon\marketing\00_内容营销引擎\runtime\state.db
   ↓
webhook_server (VPS Flask)                    ↑ Cowork artifact Dashboard
   ↓ HTTPS + HMAC                             ↑ 其他 engine/ 模块
https://taskon-ingestion.xxx                  ↑
   ↓ Cloudflare Tunnel                       ingestion endpoint (Donald 桌面 :5051)
                                              ↑
```

---

## 关键路径速查

| 类别 | 路径 | OneDrive 同步 |
|---|---|---|
| **本地开发工作区** | `E:\AILife\listmonk\` | ❌ 不同步（git 管理） |
| **D 盘规划文档** | `D:\Taskon\marketing\engine\newsletter\` | ✅ 同步 |
| **VPS 生产代码** | `/opt/taskon-newsletter/` | — |
| **VPS 运行时数据** | `/opt/taskon-newsletter/data/` | — |
| **VPS Postgres 备份** | `/opt/taskon-newsletter/backups/` | rsync 到云存储 |
| **SQLite 共享真相源** | `D:\Taskon\marketing\00_内容营销引擎\runtime\state.db` | ⚠️ 频繁写慎同步 |
| **Donald 桌面 ingestion** | 监听 :5051 / Cloudflare Tunnel 暴露公网 | — |

---

## 两个目录的关系

**D 盘 `D:\Taskon\marketing\engine\newsletter\`** = 规划 + 文档 + 模板（OneDrive 同步）
- 决策固化、部署手册、运维 runbook、SES 切换 SOP 等 md
- HTML 模板（newsletter_monthly / nurture_edm_1/2/3 / footer_partial）
- Python 脚本（import_subscribers / send_test_email / warmup_schedule）
- ingestion_endpoint_example.py（Donald 桌面跑的）
- .env.example、config.toml.example

**E 盘 `E:\AILife\listmonk\`** = Listmonk 项目代码 fork（git clone）
- 整个 [knadh/listmonk](https://github.com/knadh/listmonk) 仓库
- 加 docker-compose.yml、webhook_server/、config/、templates/（从 D 盘复制或符号链接）
- 本地试改 + Docker build + 测试
- git push 到 taskon-org/listmonk fork

**VPS `/opt/taskon-newsletter/`** = 生产部署
- git clone taskon-org/listmonk
- docker compose up -d
- 24/7 运行

---

## W1-W4 前置 Checklist

- [ ] **GitHub fork**：把 `knadh/listmonk` fork 到 `taskon-org/listmonk`
- [ ] **本地 clone**：`git clone <taskon-fork> E:\AILife\listmonk\`
- [ ] **VPS 申请**（Vultr / Linode / DigitalOcean $5-10/月）
- [ ] **域名 DNS**：`newsletter.taskon.xyz` + `newsletter-wh.taskon.xyz` 解析到 VPS IP
- [ ] **DKIM / SPF / DMARC 配置**（见部署手册 W1 D2）
- [ ] **SendGrid Free 注册** + API key
- [ ] **AWS SES Production Access 申请**（备 M4）
- [ ] **Cloudflare Tunnel** 装在 Donald 桌面（暴露 ingestion endpoint :5051）
- [ ] **Docker Desktop for Windows**（本地开发用，可选）

---

## 开发→生产的工作流

### 本地开发（E 盘）

```powershell
# 1. 在 E:\AILife\listmonk\ 改东西
cd E:\AILife\listmonk
# 改 config\config.toml / templates\xxx.html / docker-compose.yml

# 2. 本地试跑（可选）
docker compose up -d
# 访问 http://localhost:9000 验证

# 3. git commit + push
git add .
git commit -m "feat: change newsletter template"
git push origin main
```

### 部署到 VPS

```bash
# SSH 登 VPS
ssh user@vps-ip

cd /opt/taskon-newsletter

# 拉新代码
git pull

# 拉新镜像（如改了 Dockerfile 用 build）
docker compose pull

# 重启服务（自动 Listmonk migration）
docker compose --profile prod up -d

# 验证
curl https://newsletter.taskon.xyz/api/health
```

### Donald 桌面（ingestion endpoint）

```powershell
# 一次性配置 W1 D5
cd D:\Taskon\marketing\engine\newsletter\webhook_server
$env:WEBHOOK_SHARED_SECRET = "<生成 32+ 随机字符串>"
$env:SQLITE_PATH = "D:\Taskon\marketing\00_内容营销引擎\runtime\state.db"

# 启动
python ingestion_endpoint_example.py
# 或注册为 Windows Service（nssm）

# 另起 PowerShell 启动 Cloudflare Tunnel
cloudflared tunnel run taskon-ingestion
# 或注册为 Windows Service（cloudflared service install）
```

---

## SMTP relay 阶段路径

### W1-M3 · SendGrid Free（主路径）

- 注册：https://signup.sendgrid.com/
- 免费额度：100 邮件/天
- Listmonk Admin UI → Settings → SMTP 填：
  ```
  Host:     smtp.sendgrid.net
  Port:     587
  Username: apikey       （字面，不是邮箱）
  Password: <SendGrid API key>
  TLS:      STARTTLS
  ```

### M4+ · AWS SES（升级路径）

详见 `docs/切换SES手册_M4.md`

---

## 邮件量预期

| 阶段 | 月总量 | 日均 |
|---|---|---|
| W1（warm-up） | 200 | 7/天 |
| W3 | 530 | 18/天 |
| M2 | 1650 | 55/天 |
| M3 | 2600 | **87/天**（SendGrid Free 上限 100/天） |
| M4 | 3250 | **108/天** → 必须切 SES |
| M6 | 4900 | 163/天 |

---

## warm-up 节奏（不可跳过）

| 期 | 时间 | 量 | 触发条件 |
|---|---|---|---|
| 第 1 期 | W3 周三 5/27 | **≤200 封** | 仅近 7 天有互动邮箱 |
| 第 2 期 | W4 周三 6/3 | 500 封 | 第 1 期 Open ≥25% |
| 第 3 期 | M2 周三 6/24 | 1500 封 | 第 2 期 Open ≥25% |
| 第 4 期 | M2 末周三 7/15 | 2000+ 全量 | 第 3 期 Open ≥25% |

---

## 常用命令

### VPS 端（生产）

```bash
cd /opt/taskon-newsletter

# 启动所有
docker compose --profile prod up -d

# 停止
docker compose down

# 重启 listmonk
docker compose restart listmonk

# 日志
docker compose logs -f listmonk
docker compose logs -f webhook_server

# Postgres 备份
docker compose exec listmonk-db pg_dump -U listmonk listmonk | \
    gzip > backups/listmonk_$(date +%F).sql.gz

# 升级 Listmonk
docker compose pull listmonk && docker compose up -d listmonk
```

### Donald 桌面端

```powershell
# 启动 ingestion endpoint（如未注册为 service）
cd D:\Taskon\marketing\engine\newsletter\webhook_server
python ingestion_endpoint_example.py

# 看 SQLite 数据
cd D:\Taskon\marketing\00_内容营销引擎\runtime
sqlite3 state.db "SELECT count(*) FROM user_journey;"
sqlite3 state.db "SELECT * FROM newsletter_campaigns ORDER BY send_time DESC LIMIT 5;"

# 看 Cloudflare Tunnel 状态
Get-Service cloudflared
```

### E 盘本地开发

```powershell
cd E:\AILife\listmonk
git status
git pull upstream main      # 拉 knadh/listmonk 上游更新
docker compose up -d        # 本地试跑
docker compose down         # 停
```

---

## 异常处理

详见 `docs/运维runbook.md`。最常见：

| 现象 | 紧急程度 | 处理 |
|---|---|---|
| VPS 整体宕机 | P0 | VPS 控制台检查 / 联系厂商 / 必要时重建 |
| Donald 桌面 ingestion 失联 | P0 | 检查 Cloudflare Tunnel / Flask 进程 |
| webhook 转发 24h 失败率 >10% | P1 | 看 `data/webhook/failed_events.jsonl` 兼职女生补抓 |
| 邮件全进 Spam | P0 | SPF/DKIM/DMARC 检查 + 缩量 warm-up |
| SendGrid 100/天用尽 | P1 | 次日发 / 长期升级或切 SES |
| SES Production Access 被拒 | P0 | 切 Postmark $10/月 兜底 |

---

## 与 v3 全流程规划的衔接

- **本目录** = engine/newsletter 实施目录（含决策固化 + 模板 + 脚本 + 文档）
- **上游决策** = `D:\Taskon\marketing\00_内容营销引擎\全流程规划_v3\Newsletter方案对比与建议.md`
- **工程 PRD** = `D:\Taskon\marketing\00_内容营销引擎\全流程规划_v3\Engine_Components_PRD.md`（其他独立模块）
- **SQLite schema** = `D:\Taskon\marketing\00_内容营销引擎\全流程规划_v3\Metrics_Collector_归因引擎_需求文档.md §3`

---

## 相关文档

- `决策固化_Newsletter_2026-05-09.md` ★ — Donald 拍板的所有决策（v1.2 含开发vs生产分离）
- `docs/部署手册_W1.md` — W1 5 天详细步骤（含 git fork + VPS 部署 + ingestion endpoint）
- `docs/运维runbook.md` — 日常运维 + 异常处理
- `docs/切换SES手册_M4.md` — SendGrid → AWS SES 切换 SOP
