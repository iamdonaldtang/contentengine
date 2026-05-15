# Newsletter 系统决策固化 · 2026-05-09

> **本文档定位**：记录 Donald 与 Claude 在 2026-05-09 关于 Newsletter 系统的所有讨论与拍板结论，供后续会话备查。
> **状态**：v1.0 已锁定，未来调整必须在此文件追加变更段并附理由。

---

## 1 · 核心问题

TaskOn 内容营销自动化引擎需要给 2K+ B 端 Lead 发月度 Newsletter + 留资后 7 天 3 EDM Nurture 序列。

讨论的 3 个关键问题：

1. **为什么不能纯本地 VPS 自建 Postfix 发邮件？**
2. **为什么需要外部 SMTP relay（AWS SES / SendGrid 等）？**
3. **Newsletter 应用层（订阅管理 / Open/Click / Nurture）用 SaaS（Mailchimp）还是自托管开源（Listmonk）还是完全自写代码？**

---

## 2 · 已锁定的物理事实（非选择性）

### 2.1 SMTP relay 是物理必需

**不是 SaaS 锁定，是 IP 信誉物理约束**。

- Gmail / Outlook / Yahoo 等收件方反垃圾邮件系统的**第一信号是发件 IP 信誉**
- 新 VPS 上自建 Postfix 直接发 = IP 信誉为零 = 50-90% 邮件进 Spam 文件夹
- IP 信誉建设需 3-6 个月持续低量低 bounce 发送（期间触达打折）
- 一旦进 Spamhaus / Barracuda 黑名单，申诉数周到数月
- 1.5 人现实不可承担

**结论**：必须借一个"有维护好的 IP 池"的 SMTP relay 服务。
- AWS SES、SendGrid、Mailgun、Postmark、Resend 都是同类
- 唯一关心的是：deliverability + 价格

### 2.2 应用层（订阅 / 追踪 / Nurture / A/B）是工具选择，不是物理必需

完全自写代码可实现，~1160 行 Python ~40-60h 工时（Claude Code 加速到 1-2 周）。

但同样功能 Listmonk 开源项目（14k stars）已经稳定生产验证，部署 ~10h + 月度运维 4-8h。

**工时维度看 Listmonk 比自写省 30-50h**。

---

## 3 · Donald 拍板的决策

### 决策 1 · 应用层 = Listmonk 本地部署 + 配置层修改

**选择**：基于官方 [knadh/listmonk](https://github.com/knadh/listmonk) 项目，本地修改 + 本地部署。

**子决策**：
- **不 fork Go 源码** —— 用官方 Docker image + 挂载自定义 config / templates / webhook
- **本地部署在 VPS**（Vultr / Linode $10/月，区域美西就近 SMTP relay）
- **不部署在 Donald 桌面** —— Newsletter 涉及 SMTP 长连接 + webhook 接收 24/7

**理由**：
- 配置层修改可覆盖 95% 自定义需求（HTML 模板 / 退订页 / footer / Nurture 流程）
- 不动 Go 源码 → Listmonk 上游升级时合并冲突几乎为零
- 1.5 人现实下不养 Go 编译环境
- 数据完全自己掌控（自部署 Postgres）

**排除路径**：
- ❌ Mailchimp SaaS（数据在云端 + 月费阶梯）
- ❌ 完全自写 Python 代码（工时 40-60h 远超 Listmonk 部署 10h）
- ❌ Fork Listmonk Go 源码自 build（升级维护负担）

### 决策 2 · SMTP relay = SendGrid Free 起步 → M4 切 AWS SES

**阶段路径**：

| 阶段 | SMTP relay | 月成本 | 月发送量 | 触发切换条件 |
|---|---|---|---|---|
| **W1-M3** | SendGrid Free（100/天） | **$0** | ≤2.6K（87/天） | — |
| **M4-长期** | AWS SES（按量） | **~$0.5/月** | 5K-50K+ | M4 末发送量 >100/天 OR Open Rate <22% |

**为什么不一上来就 AWS SES**：
- SES Production Access 申请审核 1-3 天可能被拒（营销邮件较严）
- SendGrid Free 无审核期立即可用，W1 不延期

**为什么 M4 要切 SES**：
- SendGrid Free 用共享 IP 池，B2B 营销邮件 Open Rate 实测比 SES Production 低 30-40%
- M4 量级超 100/天 SendGrid Free 不够用
- SES 按量 $0.5/月 比 Mailgun $35/月便宜 70 倍

**W1 并行操作**：
- 注册 SendGrid Free（主路径）
- 同时提交 AWS SES Production Access Request（备 M4 切换）
- 不申请 Mailgun（试用 30 天 + $35/月 不划算）

### 决策 3 · 实际邮件量精算

| 阶段 | Lead 池 | 月度 Newsletter | Nurture EDM | 月总量 | 每日均量 |
|---|---|---|---|---|---|
| W1（warm-up 第 1 期） | 200 活跃 | 200 | ~0 | 200 | 7/天 |
| W3 | 500 | 500 | 30 | 530 | 18/天 |
| M2 | 1500 | 1500 | 150 | 1650 | 55/天 |
| M3 | 2000 全量 | 2000 | 600 | 2600 | **87/天** ★ 卡 SendGrid Free 上限 |
| M4 | 2500 | 2500 | 750 | 3250 | 108/天 → 必须切 SES |
| M6 | 4000 | 4000 | 900 | 4900 | 163/天 |

**结论**：M3 末（约 7 月下旬）切 SES。

### 决策 4 · Listmonk 部署架构（2026-05-09 v1.2 最终版）

**开发 vs 生产分离**——这是专业开源项目的标准做法。

| 阶段 | 位置 | 用途 |
|---|---|---|
| **本地开发** | `E:\AILife\listmonk\` | Donald 本地的 Listmonk 项目 git clone / 自定义修改 / Docker build / 测试 |
| **生产运行** | **VPS（Vultr / Linode $10/月）** | 24/7 跑 Listmonk + webhook server / 接 webhook 转发到 Donald 桌面 SQLite |
| **数据真相源** | `D:\Taskon\marketing\00_内容营销引擎\runtime\state.db` | Donald 桌面 SQLite（接 webhook 转发）|

**关键架构**：

```
[E 盘开发]                  [git remote]              [VPS 生产]
E:\AILife\listmonk\         GitHub / GitLab           /opt/taskon-newsletter
├── (fork of knadh/listmonk)   ↑  push                ├── docker-compose.yml
├── docker-compose.yml         ↓  pull                ├── config/
├── config/                                            ├── templates/
├── templates/                                         └── webhook_server/
└── webhook_server/                                          ↓ HTTP 转发
                                                       https://taskon-ingestion.xxx
                                                              ↓
                                                       [Donald 桌面 D 盘]
                                                       Flask ingestion endpoint
                                                              ↓
                                                       SQLite state.db
```

### 决策 4-bis · `E:\AILife\listmonk\` 角色

**E 盘是开发工作目录**——不跑生产服务。

| 用途 | 说明 |
|---|---|
| **git clone Listmonk fork** | `git clone git@github.com:taskon-org/listmonk.git E:\AILife\listmonk\` |
| **本地修改 Go 源码** | 如未来真要改 Listmonk 内核（极少情况） |
| **本地修改模板 / 配置** | 在 `templates/` 和 `config/` 改 |
| **本地 Docker build** | 测试自定义 image：`docker build -t taskon-listmonk:dev .` |
| **本地 docker-compose 试跑** | 验证修改不破坏，再 push 上服务器 |
| **CI/CD 起点** | git push 到 GitHub 后触发部署到 VPS |

**E 盘不做**：
- ❌ 不跑生产 Listmonk（VPS 才跑）
- ❌ 不接收真实邮件 webhook
- ❌ 不导入 2K+ 真实 B 端邮箱

**前置依赖**：
- Docker Desktop for Windows（本地开发用）
- git（推荐 GitHub Desktop / VSCode 集成）

### 决策 4-ter · VPS 生产部署细节

| 维度 | 方案 |
|---|---|
| 部署位置 | VPS（Vultr / Linode / DigitalOcean / Hetzner 自选） |
| 配置 | 1 CPU / 1 GB RAM / 25 GB SSD = $5-10/月 |
| 区域 | us-east-1（美西 / 就近 SES + Gmail 数据中心） |
| OS | Ubuntu 22.04 LTS |
| 部署方式 | docker-compose（4 容器：listmonk-db / listmonk / webhook_server / nginx） |
| 域名 | `newsletter.taskon.xyz`（Listmonk admin） + `newsletter-wh.taskon.xyz`（webhook） |
| TLS | Let's Encrypt（nginx-certbot 自动续）|
| SQLite ingestion | **VPS webhook_server HTTP 转发到 Donald 桌面 ingestion endpoint → 写 SQLite** |
| Donald 桌面暴露方式 | Cloudflare Tunnel（把本机 5051 → 公网 URL） |

### 决策 4-quater · CI/CD 路径

```
1. Donald 本地 E:\AILife\listmonk\ 改代码 / 模板 / 配置
2. git commit + push 到 GitHub
3. SSH 登 VPS / 或 GitHub Actions 自动部署:
   cd /opt/taskon-newsletter
   git pull
   docker compose pull          # 拉新 listmonk image（如改了 Dockerfile 用 build）
   docker compose up -d         # 重启服务（自动 migration）
4. 验证: curl https://newsletter.taskon.xyz/api/health
```

W1 阶段简化：手动 SSH + git pull + docker compose（不上 GitHub Actions）。M3 后量大可加 CI/CD。

### 决策 5 · 数据流向（v1.2 最终版 · 开发 vs 生产分离）

```
[本地开发 · E 盘]               [git remote]              [VPS 生产]
E:\AILife\listmonk\              GitHub repo               /opt/taskon-newsletter
├── (fork of knadh/listmonk)      ↑ git push               ├── Listmonk
├── docker-compose.yml              git pull ↓             ├── webhook_server
├── config/                                                ├── nginx + Let's Encrypt
├── templates/                                             └── Postgres
└── webhook_server/

Donald 改代码/模板/配置                                    Listmonk @ VPS 跑生产
        ↓                                                        ↓ 发送
                                                          SendGrid (W1-M3) / SES (M4+)
                                                                 ↓
                                                          2K+ B 端 Lead 邮箱
                                                                 ↓ Open/Click/Bounce
                                                          Listmonk webhook
                                                                 ↓
                                                          VPS webhook_server
                                                                 ↓ HTTPS + HMAC
                                                          https://taskon-ingestion.xxx
                                                                 ↓
                                                          Cloudflare Tunnel
                                                                 ↓
                                                          Donald 桌面 Flask
                                                          (ingestion endpoint :5051)
                                                                 ↓ 写
                                                          SQLite state.db (D 盘)
                                                                 ↓
                                                          Cowork artifact Dashboard
```

**关键架构**：
1. **E 盘 = 开发**（不跑生产服务 / 仅做修改 + Docker build + 测试）
2. **VPS = 生产**（Listmonk + webhook_server + Postgres + nginx 24/7）
3. **Donald 桌面 D 盘 = 数据真相源**（SQLite state.db 由 Cowork 和其他 engine/ 模块共享）
4. **VPS webhook → 桌面 ingestion 走 HTTPS + HMAC 签名 + 重试 3 次**
5. **失败兜底**：转发不成功的事件写 VPS 本地 JSONL，兼职女生周一手动补抓

**为什么不直接让 VPS 写 SQLite**：
- Cowork artifact 在 Donald 桌面读 SQLite，远程读 VPS SQLite 复杂
- v3 多个 engine/ 模块都读 D 盘 state.db，统一更简单
- SQLite 在 D 盘是 v3 锁定的"单一真相源"决策

**Cloudflare Tunnel 用途**（W1 D5 配）：
- Donald 桌面 ingestion endpoint :5051 → 公网 URL `https://taskon-ingestion.xxx`
- 仅 VPS webhook_server 调用（Cloudflare WAF 限源 IP 或 HMAC 签名校验）
- VPS 自己用 nginx + Let's Encrypt（不用 Tunnel）

### 决策 5-bis · git workflow（开发→生产）

```
Donald 本地 E:\AILife\listmonk\
  ↓ git checkout -b feature/xxx
  ↓ 改 config/ templates/ docker-compose.yml
  ↓ docker compose up -d  (本地试跑测试)
  ↓ git commit + push
GitHub (taskon-org/listmonk)
  ↓ Donald SSH 登 VPS
  ↓ cd /opt/taskon-newsletter
  ↓ git pull
  ↓ docker compose pull        # 如改了 Dockerfile 用 build
  ↓ docker compose up -d        # 重启服务（自动 migration）
VPS 生产环境
```

**关键纪律**：
- ❌ 不允许直接 SSH 登 VPS 改文件（容易丢失 / 难追溯）
- ✅ 所有改动必须在 E 盘改 + git push + VPS git pull
- ✅ .env 不入 git（生产 VPS 上 .env 单独维护）

---

## 4 · 与本规划其他文档的衔接

| 文档 | 涉及部分 | 是否已同步 v3.3 |
|---|---|---|
| `00_README.md` | 三大杠杆点 / 关键决策 | ✅ |
| `F_实施路线.md` | W1 D1-D5 时间表 | ✅ |
| `B2_内容发布.md` | §5 Newsletter 月度发送 | ✅ |
| `B4_曝光与数据分析.md` | §1.1 数据源 4 | ✅ |
| `G_风险与卡点.md` | G4 卡点 / R9 风险 | ✅ |
| `D_SOP.md` | D1.4 月末周三发送日 | ✅ |
| `C_工具栈职责边界.md` | 17 项 Newsletter | ✅ |
| `Metrics_Collector_归因引擎_需求文档.md` | §2.4 数据源 4 | ✅ |
| `Newsletter方案对比与建议.md` | §4 落地方案 | ✅ |

---

## 5 · 不可让渡的红线（沿用 v2 + 本决策新增）

❌ **不发 C 端邮件给 2200 万注册用户**（错位 + IP 信誉风险） — R3
❌ **不发产品广告给 B 端 Lead 池**（退订率飙升 → IP 进黑名单） — R9
❌ **不跳过 warm-up 阶梯**（IP 信誉一次性爆掉） — 新增
❌ **不让 Listmonk 直接连 Gmail SMTP / Outlook SMTP 发**（账号封禁风险） — 新增
❌ **不允许邮件不带 unsubscribe 链接**（CAN-SPAM 违法）— 新增
❌ **不允许邮件不带 footer 公司地址**（CAN-SPAM 违法）— 新增

---

## 6 · 待 Donald 后续决策的小问题（W1 D1 前明确即可）

1. **VPS 厂商**：Vultr / Linode / DigitalOcean / Hetzner？默认建议 Vultr（区域多 + UI 简单）
2. **VPS 区域**：us-east-1（美西就近 SES + Gmail）/ ap-east-1（亚洲就近华语圈）？默认美西
3. **域名**：`newsletter.taskon.xyz` 还是 `news.taskon.xyz`？默认 `newsletter.taskon.xyz`
4. **Listmonk 管理员邮箱**：Donald 个人邮箱 / `admin@taskon.xyz`？默认后者（多人可登入）
5. **SendGrid Free 注册用什么邮箱**：Donald 个人 / `marketing@taskon.xyz`？默认后者

---

## 7 · 文件目录结构（v2 修订）

**关键拆分**：
- `D:\Taskon\marketing\engine\newsletter\` = **规划 + 模板 + 脚本 + 文档**（OneDrive 同步保留）
- `E:\AILife\listmonk\` = **Listmonk 运行时**（Docker volume / 数据 / 不入 OneDrive 同步）

### 7.1 D 盘规划目录（OneDrive 同步）

```
D:\Taskon\marketing\engine\newsletter\
├── 决策固化_Newsletter_2026-05-09.md    ← 本文件
├── README.md                              ← 全流程总览
├── docker-compose.yml                     ← Listmonk + Postgres 一键（路径指向 E:\AILife\listmonk\data\）
├── .env.example                           ← 环境变量模板
├── .gitignore
├── config\
│   ├── config.toml                        ← Listmonk 主配置（gitignore，含密码）
│   └── config.toml.example                ← 配置模板
├── templates\
│   ├── newsletter_monthly.html
│   ├── nurture_edm_1.html
│   ├── nurture_edm_2.html
│   ├── nurture_edm_3.html
│   └── footer_partial.html
├── webhook_server\
│   ├── app.py                             ← Flask 接 Listmonk webhook（直接写 SQLite）
│   ├── requirements.txt
│   └── Dockerfile
├── scripts\
│   ├── import_subscribers.py
│   ├── send_test_email.py
│   └── warmup_schedule.py
└── docs\
    ├── 部署手册_W1.md                     ← Docker Desktop for Windows 步骤
    ├── 运维runbook.md
    └── 切换SES手册_M4.md
```

### 7.2 E 盘运行时目录（不入 OneDrive）

```
E:\AILife\listmonk\                       ← Listmonk 本地 runtime root
├── data\                                  ← Docker volume mount 目标
│   ├── postgres\                          ← Listmonk Postgres 数据（自动创建）
│   └── uploads\                           ← Listmonk uploads（图片 / 附件）
├── logs\                                  ← Docker container 日志
└── backups\                               ← Postgres 备份（每日 cron）
    └── listmonk_YYYY-MM-DD.sql.gz
```

**OneDrive 必须排除 `E:\AILife\listmonk\data\` 整目录**（高频写 / 文件锁冲突 / 体积大）

### 7.3 SQLite 真相源（D 盘，与 v3 其他模块共享）

```
D:\Taskon\marketing\00_内容营销引擎\runtime\state.db
                                       ↑
                            webhook_server Docker mount 读写
                            其他 engine/ 模块 也读写
```

### 7.4 Cloudflare Tunnel 配置（W1 D5）

```
cloudflared.exe 安装在: C:\Program Files\cloudflared\
配置文件: C:\Users\<user>\.cloudflared\config.yml
开机自启: 注册为 Windows Service

公网→本地 映射:
  newsletter.taskon.xyz       → localhost:9000  (Listmonk admin)
  newsletter-wh.taskon.xyz    → localhost:5050  (webhook server, M4 接 SES SNS 用)
```

---

## 8 · 关键 URL 速查

- **Listmonk 项目**：https://github.com/knadh/listmonk
- **Listmonk 官方文档**：https://listmonk.app/docs/
- **Listmonk Docker 部署**：https://listmonk.app/docs/installation/#docker
- **SendGrid Free 注册**：https://signup.sendgrid.com/
- **SendGrid SMTP 文档**：https://docs.sendgrid.com/for-developers/sending-email/integrating-with-the-smtp-api
- **AWS SES 控制台**：https://console.aws.amazon.com/ses/
- **AWS SES Production Access**：https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html

---

## 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-09 早 | 首版。固化 Donald 5/9 5 个核心决策（应用层 Listmonk / SMTP relay 阶段路径 / 邮件量精算 / VPS 部署 / 数据流向） |
| v1.1 | 2026-05-09 深夜 | **架构修订**：决策 4 VPS → 本地 `E:\AILife\listmonk`；新增决策 4-bis 桌面常态在线对冲；决策 5 数据流简化（webhook server 同机器直写 SQLite，去 ingestion endpoint + Cloudflare Tunnel for ingestion）；§7 目录拆分 D 盘规划 + E 盘运行时 |
