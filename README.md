# TaskOn Marketing Engine

> 内容营销自动化引擎 · 10 个模块 + 15 张 SQLite 表 + 6 个定时任务
> **代码根**：`D:\Taskon\marketing\engine\` （本地开发） → 之后整体移植到 VPS
> **唯一真相源**：`runtime/state.db`

---

## 0 · 30 秒速览

```
信号源 (KOL / 候选池)
   ↓
Topic Ranker         ──→ runtime/selection_<week>.md
   ↓
Cowork 起草 → drafts/<id>/xthread_final.md
   ↓
Adapter Orchestrator ──→ linkedin / medium / carousel / shorts 4 平台版
   ↓
Voice Checker        ──→ drafts/<id>/voice_report.md  (14 禁词 + 长度 + CTA)
   ↓
UTM Generator        ──→ drafts/<id>/utm_links.json   (shlink 自托管)
   ↓
Postiz / Listmonk / Update Btouch                     ←── 发布
   ↓
Metrics Collector  (daily 20:00)  ──→ metrics_daily / landing_metrics / ...
   ↓
Attribution Engine (daily 21:00)  ──→ user_journey + leads
   ↓
Weekly Reporter    (Sun 18:00)    ──→ runtime/weekly_report_<week>.md
```

---

## 1 · 目录结构

```
engine/
├── lib/                    Python 共享层（db / lark / retry / llm_client / utm / shlink）
├── sources/                5 个外部 API adapter（postiz / twitter_x / ga4 / listmonk / btouch）
├── jobs/                   10 个 job 模块 + 1 个 __init__
├── config/                 YAML / Markdown 配置
│   ├── kol_watchlist.yaml
│   ├── voice_disabled_words.yaml
│   ├── multiplatform_rules.yaml
│   ├── topic_ranker_rubric.md
│   └── prompts/            6 个 LLM prompt 模板
├── scripts/                init_db / backup_sqlite / seed_test_data / setup_scheduler
├── tests/                  47 个 pytest 用例（lib + jobs + sources）
├── runtime/                ★ git-ignored
│   ├── state.db                ← SQLite 单一真相源
│   ├── drafts/<piece_id>/      ← 每条内容的草稿 + 报告
│   ├── logs/                   ← per-job 日志
│   ├── backups/                ← 每日 SQLite dump
│   ├── ga4_credentials.json    ← GA4 service account
│   ├── twikit_cookies.json     ← KOL Watch fallback session
│   └── *.json / *.md           ← 选题池 / 周报 / 月报
├── newsletter/             ✅ Listmonk + webhook server（已就位）
├── skills/                 ✅ Cowork skill 源码（content-critic + mpt-video）
├── plugin-src/             ✅ Cowork plugin 源码（taskon-content-critic）
├── dify/                   ✅ Dify 工作流配置文档（备用编排）
├── docs/                   ← 部署 / 排障 / 架构 文档
├── .env.example
├── config.yaml
├── pyproject.toml
└── requirements.txt
```

---

## 2 · 本地开发安装（Windows · 单次）

```powershell
cd D:\Taskon\marketing\engine

# 2.1 创建虚拟环境（Python 3.12+）
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2.2 安装依赖
pip install --upgrade pip
pip install -r requirements.txt

# 2.3 拷贝 .env 并填入真实 key
copy .env.example .env
notepad .env

# 2.4 初始化 SQLite（创建 runtime/state.db + 15 张表）
python -m scripts.init_db

# 2.5 跑全量测试（应该 47 passed）
pytest -q

# 2.6 灌种子数据（可选 · 给 Cowork artifact dashboard 预览）
python -m scripts.seed_test_data
```

> **如果 `python` 命令不存在**：装 Python 3.12+ 并把 `python.exe` 加到 PATH，或用 `py -3.12` 替代。

---

## 2.5 · 本地 Docker 部署（推荐 · 单容器跑全部 8 个定时任务）

容器内置 [supercronic](https://github.com/aptible/supercronic) 跑所有 cron，**state.db 通过 host 卷映射持久化**，编辑 `./config/*.yaml` 不用重建镜像，重启容器即可生效。

```powershell
cd D:\Taskon\marketing\engine

# 2.5.1 构建镜像（首次 ~3 分钟，含 supercronic 下载 + pip install）
docker compose build

# 2.5.2 启动（detached）
docker compose up -d

# 2.5.3 查看 unified 日志（cron + 各 job 都在这一条流里）
docker compose logs -f

# 2.5.4 状态 / 健康检查
docker compose ps

# 2.5.5 手动触发某个 job（绕过 cron）
docker compose exec engine python -m jobs.metrics_collector --dry-run
docker compose exec engine python -m jobs.weekly_reporter --week 2026W19

# 2.5.6 容器内跑 pytest
docker compose exec engine python -m pytest -q

# 2.5.7 停止 + 删除容器（数据保留在 ./runtime/）
docker compose down
```

**关键点**：
- 镜像 ~646MB（Python 3.12-slim + jieba + anthropic + 其他依赖 + supercronic）
- 容器以非 root user `taskon` (UID 1000) 跑
- `runtime/` 是 host bind mount，state.db 在 `./runtime/state.db` 直接可查
- `config/` 只读挂载，IDE 编辑 → 下次 cron 触发自动读新值
- 健康检查每 60 秒跑一次 `init_db --verify-only`
- 日志：JSON file driver，10MB × 5 文件滚动
- 资源上限：1G 内存 + 1 CPU（修改 `docker-compose.yml` 调整）

**关键 env：**.env 里的 D:/Taskon/... 绝对路径是给 host 端 Cowork 工具的；docker-compose.yml 内部覆写所有路径到 `/app/runtime/` 容器内路径，host vs 容器路径自动隔离。

---

## 3 · 服务器部署（Linux VPS · M2 升级时）

```bash
# 3.1 准备依赖
sudo apt update && sudo apt install -y python3.12 python3.12-venv git
git clone <内部 git 仓> /opt/taskon-marketing
cd /opt/taskon-marketing/engine

# 3.2 虚拟环境 + 依赖
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3.3 配置
cp .env.example .env
$EDITOR .env
# 服务器关键差异：
#   ENGINE_ROOT=/opt/taskon-marketing/engine
#   SQLITE_PATH=/var/lib/taskon/state.db
#   GA4_CREDENTIALS_PATH=/etc/taskon/ga4_credentials.json
#   LOG_DIR=/var/log/taskon

sudo mkdir -p /var/lib/taskon /var/log/taskon /etc/taskon
sudo chown -R $USER /var/lib/taskon /var/log/taskon

# 3.4 DB 初始化
python -m scripts.init_db

# 3.5 注册 systemd timer 套件
sudo bash scripts/setup_systemd.sh

# 3.6 Listmonk + webhook server 在 newsletter/ 子目录单独部署，见 newsletter/README.md
```

---

## 4 · 6 个定时任务（cron 表）

| Job | 调度 | 入口 | 输出 |
|---|---|---|---|
| `metrics_collector` | 每日 20:00 | `python -m jobs.metrics_collector` | `metrics_daily / landing_metrics / btouch_daily / newsletter_*` |
| `attribution_engine` | 每日 21:00 | `python -m jobs.attribution_engine` | `user_journey / leads` |
| `weekly_reporter`  | 周日 18:00 | `python -m jobs.weekly_reporter --week 2026W19` | `runtime/weekly_report_<week>.md` |
| `monthly_reporter` | 月末周日 19:00 | `python -m jobs.monthly_reporter` | `runtime/monthly_report_<YYYY-MM>.md` |
| `kol_watch` | 周日 10:00 | `python -m jobs.kol_watch` | `runtime/kol_topics_<week>.json` + candidates 表 |
| `update_btouch` | 周一 09:00 | `python -m jobs.update_btouch` | TaskOn 后台 6 触点 push |
| `topic_ranker` | 周日 20:00 | `python -m jobs.topic_ranker --week 2026W19` | `runtime/selection_<week>.md` |
| `backup_sqlite` | 每日 23:00 | `python -m scripts.backup_sqlite` | `runtime/backups/state-<ts>.db` |

**Windows 一键注册**：

```powershell
pwsh scripts/setup_scheduler.ps1
```

**Linux 一键注册**：

```bash
sudo bash scripts/setup_systemd.sh
```

---

## 5 · 手动跑 / 调试

每个 job 都支持 `--help` 列参数：

```powershell
python -m jobs.voice_checker --help
python -m jobs.adapter_orchestrator --piece-id 2026W19-thread01
python -m jobs.metrics_collector --date 2026-05-12
python -m jobs.attribution_engine --compute-weekly-aggregates 2026W19
python -m jobs.weekly_reporter --week 2026W19
python -m jobs.kol_watch --fallback-only
```

**Dry run**：`--dry-run` flag 在 `update_btouch` / `metrics_collector` 支持，不写 DB / 不调外部 API。

---

## 6 · 配置文件说明

| 文件 | 谁动 | 用途 |
|---|---|---|
| `.env` | Donald | 所有 API key / 路径 |
| `config.yaml` | Donald + 兼职女生 | LLM/超时/btouch 触点/调度时间 |
| `config/voice_disabled_words.yaml` | Donald | 14 禁词 + B2B/YT 扩展 + AI 味套话 + CTA 模式 |
| `config/multiplatform_rules.yaml` | Donald | 4 平台改写规则 |
| `config/prompts/*.txt` | Donald | LLM system prompt 模板 |
| `config/topic_ranker_rubric.md` | Donald | 5 维评分 rubric |
| `config/kol_watchlist.yaml` | Donald + 兼职女生 | 30 KOL handle + tier |

修改后**立即生效**（无需重启），但运行中的 job 不会热加载——下次 cron 触发时读新值。

---

## 7 · LLM 路由（MiniMaxi primary + Anthropic fallback）

3 把 MiniMaxi key 轮询，失败超 N 次（`LLM_MAX_RETRIES_PER_KEY` × 3 keys）→ 切 Anthropic Opus 4.7。

```bash
# .env
MINIMAXI_BASE_URL=https://api.minimaxi.chat/v1
MINIMAXI_API_KEY_1=...
MINIMAXI_API_KEY_2=...
MINIMAXI_API_KEY_3=...
MINIMAXI_DEFAULT_MODEL=MiniMax-M1
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_FALLBACK_MODEL=claude-opus-4-7
ENABLE_ANTHROPIC_FALLBACK=true
```

**强制走 Anthropic**：把所有 MiniMaxi key 留空即可（系统跳过 primary 路径）。
**禁用 fallback**：`ENABLE_ANTHROPIC_FALLBACK=false` —— MiniMaxi 全失败时抛 `LLMClientError`。

---

## 8 · 验证 checklist（部署完跑一遍）

```
□ python -m scripts.init_db --verify-only         (退出码 0)
□ pytest -q                                       (47 passed)
□ python -m scripts.seed_test_data                (3 pieces / 5 metrics / 3 leads 写入)
□ python -m jobs.weekly_reporter --week 2026W19   (生成 .md)
□ python -m jobs.metrics_collector --dry-run      (5 源至少 3 个可达)
□ python -c "from lib.lark import alert; alert('P2','test alert')"   (Lark 收到)
□ Windows: schtasks /Query /TN "TaskOn-*"         (6 任务存在)
□ Linux:   systemctl list-timers 'taskon-*'       (6 timer active)
```

---

## 9 · 进一步阅读

- **架构原理**：[docs/architecture.md](docs/architecture.md)
- **排障 runbook**：[docs/troubleshooting.md](docs/troubleshooting.md)
- **PRD 真相源**：[Engine_Components_PRD.md](Engine_Components_PRD.md)
- **Metrics + 归因详细需求**：[Metrics_Collector_归因引擎_需求文档.md](Metrics_Collector_归因引擎_需求文档.md)
- **AI 编程提示词**：[Prompt_AI系统化编程_v1.md](Prompt_AI系统化编程_v1.md)
- **Newsletter 子模块**：[newsletter/README.md](newsletter/README.md)

---

## 10 · 安全 / 数据主权

- 所有 secret 在 `.env`，**不入 git**（`.gitignore` 已锁）
- 所有业务数据落本地 SQLite，**不进任何云 SaaS DB**
- LLM 调用只走出站 HTTPS；不上传训练数据（MiniMaxi / Anthropic 各自数据策略请 Donald 自行评估）
- KOL 抓取走 X Premium API（合规） + Twikit fallback（账号自担风险，cookie pool 加 3-5 个备用号）

---

## 11 · 变更记录

| 日期 | 变更 |
|---|---|
| 2026-05-13 | v0.1 首版 · 10 模块 + 47 测试 + Windows/Linux 调度脚本 |
| 2026-05-13 | v0.1.1 · 加 Dockerfile + docker-compose + supercronic 单容器部署（646MB）|
