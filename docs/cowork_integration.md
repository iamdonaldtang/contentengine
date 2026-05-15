# Cowork ↔ Engine 集成图谱

> **本文档定位**：Donald 在 Claude Desktop 的 Cowork 里跑全流程内容营销时，**怎么和 `engine/` 这个后台引擎对话**。
> **配套阅读**：`D:\Taskon\marketing\00_内容营销引擎\全流程规划_v3\C_工具栈职责边界.md`（17 行决策矩阵 + 6 原则）+ `B1_内容生产全流程.md`（生产细节）
> **核心原则**（来自 C §原则1）：**Cowork = 驾驶舱（你坐的位子）· engine = 发动机（罩盖下的代码）**

---

## 1 · 30 秒速览：3 种接触面

```
┌──────────────────────────────────────────────────────────────────┐
│  Donald · Claude Desktop · Cowork                                │
│  ── 选题 / 起草 / 改稿 / 审阅 / 看 Dashboard 都在这里 ──            │
└────────────┬─────────────────┬───────────────────┬───────────────┘
             │                 │                   │
       ① 文件系统          ② SQLite read     ③ engine CLI
       drafts/<id>/*.md    state.db          docker compose exec ...
       runtime/*.json
       config/*.yaml
             │                 │                   │
             ▼                 ▼                   ▼
┌──────────────────────────────────────────────────────────────────┐
│  engine/ container · supercronic + 8 jobs · 15 表 state.db        │
│  + ingestion · Flask :5051 · 落地页留资 / Listmonk / SES webhook   │
└──────────────────────────────────────────────────────────────────┘
```

3 种接触面：
1. **文件系统** — Cowork 起草 / 改稿都是写 `runtime/drafts/<piece_id>/*.md`，engine 的 voice_checker / adapter_orchestrator / utm_generator 也读写这个目录
2. **SQLite 直读** — Cowork artifact dashboard 用 sql.js 读 `state.db`（只读）；写永远走 engine
3. **engine CLI 触发** — 你在 Cowork 里说"跑下 adapter"等于 Cowork 调 `docker compose exec engine python -m jobs.adapter_orchestrator --piece-id ...`

---

## 2 · 17 行决策矩阵 vs 实际代码映射

下表把 C 文档的 17 行决策矩阵，**逐行对应到本仓代码的具体位置**，并标注每行**是谁在驾驶**（Cowork 驾驶舱 / engine 后台 / 人手动 / 外部框架）。

| # | 流程能力 | C 推荐 | engine 实现位置 | 谁触发 | 备注 |
|---|---|---|---|---|---|
| 1 | 选题生成 | Cowork 6 路 skill + Donald 选 | `jobs/topic_ranker.py` 跑 5 维评分 + 调权；`jobs/kol_watch.py` 提供路 2 | Cowork（起）+ supercronic（周日 20:00） | engine 给出 Top10，Donald 在 Cowork 里挑 2-3 条 |
| 2 | 长文（Blog） | Cowork `taskon-content-marketing` skill | `jobs/adapter_orchestrator.py` 跑 `medium_long` 配 prompt | Cowork（起草）+ engine（多平台改写） | skill 起 X Thread 终稿后，engine 自动改 Medium 版 |
| 3 | X Thread | Cowork `crypto-twitter-creator` skill | drafts/<id>/xthread_final.md 落盘后 engine 接管 | Cowork | engine 不起 X 主稿，只做改写和评审 |
| 4 | 视频生产 | MPT via Cowork `mpt-video` skill | **engine 不做** | Cowork | 走本机 `http://localhost:8090` MPT API |
| 5 | 多平台改写 | Cowork orchestrator | **`jobs/adapter_orchestrator.py` ★** | engine（手触发 or cron） | 一鸡多吃 3 策略硬约束已编码在 prompts |
| 6 | 评审打分 | Cowork `taskon-content-critic` plugin | **engine 不做** | Cowork plugin | engine 只触发 voice_checker；critic 由 plugin 做 |
| 7 | Voice Checker（机检） | 自写 Python | **`jobs/voice_checker.py`** | engine（被 adapter 串调 or 手触发） | 14 禁词 + 长度 + CTA |
| 8 | Fact-Check | Cowork `brand-review` + grep | **engine 不做** | Cowork + 人 | 兼职女生补 `[DATA-NEEDED]` |
| 9 | UTM 注入 | 自写脚本 | **`jobs/utm_generator.py` + `lib/utm.py` + `lib/shlink.py`** | engine（手触发 or 排程前） | shlink 自托管，长链 fallback |
| 10 | 定时发布调度 | Postiz | **engine 不做** | Postiz | engine 只读 Postiz analytics（sources/postiz.py） |
| 11 | 跨平台分发 | Postiz | 同上 | 同上 | 同上 |
| 12 | KOL 监测 | Cowork 双 skill + X Premium | **`jobs/kol_watch.py` + `lib/twikit_pool.py`** | engine（周日 10:00 cron） | X API 主路径 + Twikit 多账号池 fallback |
| 13 | 数据采集（5 源） | 本地 Python 脚本 | **`jobs/metrics_collector.py` + `sources/*.py`** | engine（日 20:00 cron） | Postiz / X / GA4 / Listmonk / btouch |
| 14 | 归因分析 | 本地 Python 脚本 | **`jobs/attribution_engine.py` + `jobs/channel_attribution.py`** | engine（日 21:00 cron + 月 Markov） | MVP last-touch + V1 first-touch + V2 linear + V3 Markov |
| 15 | 看板呈现 | Cowork artifact | **engine 提供 SQLite 表 + ingestion `/metrics` 端点** | Cowork artifact | sql.js / prom 双通道 |
| 16 | 周报月报 | Cowork `performance-report` skill | **`jobs/weekly_reporter.py` + `monthly_reporter.py` + `performance_analyzer.py`** | engine（周日 18:00/18:30 + 月末） | LLM 走 MiniMaxi M2.7 |
| 17 | Newsletter | Cowork 起草 + Listmonk + SES | **`sources/listmonk.py` + `ingestion/app.py /api/listmonk-webhook`** | Cowork（起草）+ Listmonk 外部 + engine ingestion 接 webhook | E:\AILife\listmonk 还未本机部署 |

**记忆口诀**：
- **Cowork 干"想"的事**（选题决策 / 起草 / 评审 / 看板）
- **engine 干"算"的事**（评分 / 改写 / UTM / 数据 / 归因 / 报告）
- **Postiz 干"发"的事**（定时发布）
- **MPT 干"剪"的事**（视频生产）
- **Listmonk + SES 干"寄"的事**（Newsletter）

---

## 3 · Cowork 怎么"开" engine — 4 种姿势

### 姿势 A · Cowork 在文件系统里"扔稿子" → engine 自动处理

最常用模式。**没有 API 调用**，纯文件系统握手。

```
Cowork (Donald 在 Claude Desktop)
   ↓ 用 taskon-content-marketing skill 起草 X Thread
   ↓ Cowork 把终稿存到:
   D:\Taskon\marketing\engine\runtime\drafts\2026W19-thread01\xthread_final.md
   ↓ Cowork 同时把选题卡 YAML 存到:
   .../2026W19-thread01\selection_card.yaml

   [此刻 Donald 等下一步...]

Donald 在 Cowork 里说："跑下 adapter 把这条改成 4 平台版本"
   ↓ Cowork 触发 bash 工具:
docker compose exec engine python -m jobs.adapter_orchestrator --piece-id 2026W19-thread01

engine 跑完 → 写回:
  drafts/2026W19-thread01/linkedin_post.md
  drafts/2026W19-thread01/medium_long.md
  drafts/2026W19-thread01/carousel_10pages.md
  drafts/2026W19-thread01/shorts_60s.md
  drafts/2026W19-thread01/voice_report.md   ← 自动跑了 voice_checker

Cowork 直接读这些文件给 Donald 看
```

### 姿势 B · Cowork artifact 直读 SQLite（dashboard）

```
Cowork → create_artifact("dashboard.html")
   ↓ artifact 内嵌 sql.js:
   <script src="//cdn.jsdelivr.net/npm/sql.js/dist/sql-wasm.js"></script>
   const SQL = await initSqlJs();
   const db = new SQL.Database(await fetch('state.db').then(r => r.arrayBuffer()));
   const top10 = db.exec("SELECT * FROM weekly_aggregates WHERE week='2026W19'");

artifact 渲染 K1-K11 指标卡片 + 5 维归因表
```

**read-only 纪律**：artifact **绝不写 state.db**。要改数据必须走 engine job 或 ingestion API。

### 姿势 C · Cowork 调 ingestion `/metrics` 取实时数

```python
# Cowork 用 fetch tool 拿 Prometheus 格式监控数据
GET http://127.0.0.1:5051/metrics
→
# TYPE taskon_leads_total gauge
taskon_leads_total 142
# TYPE taskon_heartbeat_last_seconds gauge
taskon_heartbeat_last_seconds{job="metrics_collector"} 1234
```

适合"我今天上午发的 X Thread 跑了多少 lead 进来了？"这种实时小问。

### 姿势 D · Cowork 让 Donald 手动跑某个 job

```
Donald 在 Cowork 里说："跑下本周 weekly reporter 看看"

Cowork 触发:
docker compose exec engine python -m jobs.weekly_reporter --week 2026W19

输出:
runtime/weekly_report_2026W19.md

Cowork 读这个文件 + 展示给 Donald
```

类似的可手触发模块：`topic_ranker`, `performance_analyzer`, `kol_watch`, `update_btouch`, `cohort_analysis`, `ab_aggregator`, `channel_attribution`。

---

## 4 · Cowork prompt 模板（直接复制粘贴用）

### 模板 4.1 · 触发完整起草链

```
我刚把 X Thread 终稿存到 D:\Taskon\marketing\engine\runtime\drafts\2026W19-thread01\xthread_final.md

帮我:
1. 跑 adapter_orchestrator 改成 LinkedIn Post / Medium 长文 / Carousel / YT Shorts 四个版本
2. 跑完后把 voice_report.md 给我看下
3. 如果某个版本不过 voice check，告诉我哪条违规
4. 全过的话，跑下 utm_generator 给我 8 平台 × donald_en 的 UTM 短链 JSON

bash 命令：
docker compose exec engine python -m jobs.adapter_orchestrator --piece-id 2026W19-thread01
docker compose exec engine python -m jobs.utm_generator --piece-id 2026W19-thread01 --target-url https://taskon.xyz/benchmark-report --platforms twitter,linkedin,medium,youtube --accounts donald_en,taskon_official --hook-type 47pct_bot
```

### 模板 4.2 · 周日选题会

```
今天周日，跑下选题流水线，给我下周选 2-3 条:

1. 先跑 kol_watch 看 30 个 KOL 上周聊什么
2. 跑 topic_ranker 把候选池打分
3. 把 runtime/selection_2026W19.md 的 Top10 念给我听
4. 我从里面挑 2-3 条，你帮我新建 piece 占位（state=selected）

bash:
docker compose exec engine python -m jobs.kol_watch
docker compose exec engine python -m jobs.topic_ranker --week 2026W19
type D:\Taskon\marketing\engine\runtime\selection_2026W19.md
```

### 模板 4.3 · 周复盘

```
周一上午做上周复盘:

1. 跑 weekly_reporter
2. 跑 performance_analyzer 找 Top1 / Bottom1
3. 把两份 .md 都念给我，重点讲 5 维归因和 Top1 赢的原因

bash:
docker compose exec engine python -m jobs.weekly_reporter --week 2026W19
docker compose exec engine python -m jobs.performance_analyzer --week 2026W19
type D:\Taskon\marketing\engine\runtime\weekly_report_2026W19.md
type D:\Taskon\marketing\engine\runtime\performance_analysis_2026W19.md
```

### 模板 4.4 · 月度多触点归因

```
月底跑深度归因看哪些 channel 真带量:

1. cohort_analysis 看过去 8 周 D7/D30 转化漏斗
2. ab_aggregator 看 utm_term 实验
3. channel_attribution 跑 Markov 多触点

bash:
docker compose exec engine python -m jobs.cohort_analysis
docker compose exec engine python -m jobs.ab_aggregator --month 2026-05
docker compose exec engine python -m jobs.channel_attribution --month 2026-05
```

### 模板 4.5 · 单条压测

```
我在外面 IDE 刚改了 jobs/voice_checker.py 的禁词清单，跑下测试:

bash:
docker compose exec engine python -m pytest tests/test_voice_checker.py -v
```

---

## 5 · engine 不该做的 4 件事（红线）

按 C 文档 §红线：

❌ **engine 不起 X 主稿** — 走 Cowork `crypto-twitter-creator` skill，engine 只做改写
❌ **engine 不做评审打分** — `taskon-content-critic` plugin 在 Cowork 独占
❌ **engine 不发邮件** — Listmonk + SES 干这事，engine 只接 webhook
❌ **engine 不改数据** — Fact-Check 失败必删段，绝不让 LLM 编数字

---

## 6 · 调试 / 排查入口（Donald 自助）

| 想知道 | 命令 |
|---|---|
| 容器健不健康 | `docker compose ps` |
| 哪个 cron 最近跑过 | `docker compose exec engine python -c "from lib.db import db; [print(dict(r)) for r in db.fetchall('SELECT job_name,last_run_at,status FROM heartbeat ORDER BY last_run_at DESC LIMIT 20')]"` |
| 哪里告警了 | `docker compose exec engine python -c "from lib.db import db; [print(dict(r)) for r in db.fetchall('SELECT * FROM publish_failures ORDER BY occurred_at DESC LIMIT 10')]"` |
| LLM 通不通 | `docker compose exec engine python -c "from lib.llm_client import llm; print(llm.complete('test','say hi'))"` |
| 落地页留资能不能写进来 | `curl -X POST http://127.0.0.1:5051/api/landing-signup -H "Content-Type: application/json" -d '{"email":"t@x.com","page_path":"/free-diagnostic","url":"https://taskon.xyz/?utm_source=manual"}'` |
| 全量看板数据 | `curl http://127.0.0.1:5051/metrics` |
| 容器日志（cron + jobs） | `docker compose logs -f engine` |
| ingestion 日志（HTTP） | `docker compose logs -f ingestion` |

---

## 7 · 与 4 个外部系统的接线图

```
┌─────────────────────────────────────────────────────────┐
│                  Cowork (Claude Desktop)                 │
└──┬──────────────────────────────────────────────────────┘
   │
   │ docker compose exec / 文件 I/O / sql.js
   ▼
┌──────────────────────────┐
│  engine + ingestion       │ ← 本仓 ★
│  (本地 Docker)             │
└──┬─────────┬────────┬─────┘
   │         │        │
   │ HTTP    │ HTTP   │ SMTP+SNS
   ▼         ▼        ▼
┌──────┐ ┌──────┐ ┌──────────────┐
│ MPT  │ │Postiz│ │ Listmonk     │  ★ E:\AILife\listmonk 未部署
│:8090 │ │:5000 │ │ + AWS SES    │     需要先 docker compose up
└──────┘ └──────┘ └──────────────┘
   ↑         ↑          ↑
   │         │          │
 Cowork    Donald     Cowork
 mpt-video 在 Postiz   email-sequence
 skill 调   UI 看排程   skill 起草
```

**4 个外部接线**：
- **MPT**（视频）—— 已部署在 `localhost:8090`，Cowork `mpt-video` skill 直调
- **Postiz**（发布调度）—— 已部署，engine 通过 `sources/postiz.py` 拉 analytics
- **Listmonk + SES**（Newsletter）—— **未部署**（`E:\AILife\listmonk`），engine 通过 `sources/listmonk.py` 拉 campaign 数据 + `ingestion/app.py /api/listmonk-webhook` 接事件
- **shlink**（短链）—— **未部署**，engine 通过 `lib/shlink.py` 调；未部署时 fallback 长链

---

## 8 · 接下来去看这两份

- [todo.md](../todo.md) — 还没填的 .env key + 未部署的外部服务 + B1 manual touchpoints
- [B1_audit_prompt.md](B1_audit_prompt.md) — 你拿这个 prompt 单独开会话，让 Claude 替你跑一次"B1 全流程能不能自动化"自检
