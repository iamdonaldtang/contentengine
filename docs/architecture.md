# Architecture

> 一张图 + 14 表关系 + 跨模块调用图。详细 PRD 见根目录三份 .md。

---

## 1 · 端到端数据流

```
┌────────────────────────────────────────────────────────────────────────┐
│  Cowork 主驾驶舱（Donald 桌面）                                          │
│   起草 skill / 评审 critic plugin / artifact dashboard / 月报           │
└─────────┬───────────────────────────────────────────────┬──────────────┘
          │ 起草 + 评审 (人工 + Cowork)                     │ 读 dashboard
          ▼                                                ▲
┌─────────────────────────────────────────────────────────────────────┐
│ engine/  ★ 本仓                                                      │
│                                                                      │
│  jobs/topic_ranker     ──→ runtime/selection_<week>.md               │
│       ▲   ▲                                                          │
│       │   │ weight from weekly_aggregates                            │
│       │   │                                                          │
│  jobs/kol_watch        ──→ candidates (source_route=2)               │
│       │                                                              │
│  candidates pool ─→ Ranker ─→ Cowork 起草 ─→ drafts/<id>/*.md        │
│                                                  │                   │
│  jobs/adapter_orchestrator  ←──────── selection card + xthread       │
│       │                                                              │
│       └─→ linkedin/medium/carousel/shorts                            │
│       └─→ jobs/voice_checker  ──→ voice_report.md                    │
│       └─→ jobs/utm_generator  ──→ utm_links.json (via lib/shlink)    │
│                                                                      │
│  Postiz Public API (社媒发布) ←── Donald 手动 / Postiz UI            │
│  Listmonk API (邮件发布)    ←── newsletter/ 子模块                   │
│  jobs/update_btouch (周一 09:00) ──→ TaskOn 后台 6 触点              │
│                                                                      │
│  jobs/metrics_collector (日 20:00) ──→ metrics_daily /               │
│       │                                  landing_metrics /           │
│       │                                  newsletter_* / btouch_daily │
│       │                                                              │
│  jobs/attribution_engine (日 21:00) ──→ user_journey / leads         │
│       │                                                              │
│  jobs/weekly_reporter (周日 18:00) ──→ runtime/weekly_report_*.md    │
│  jobs/monthly_reporter (月末)      ──→ runtime/monthly_report_*.md   │
│                                                                      │
│  SQLite state.db ★ 唯一真相源 (15 张表)                              │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2 · 15 张表关系

```
pieces ──1:N──→ state_events           (审计日志)
   │
   │ 1:N
   ▼
publishings ──1:N──→ metrics_daily     (30m / 24h / 7d 三时点)

candidates  (路 1-6 汇合 · 被 Ranker 消费 → 升级为 pieces)
kol_watchlist (KOL Watch 写入 → 候选池路 2)

landing_metrics (GA4)
btouch_daily    (TaskOn 6 触点)
newsletter_campaigns ──1:N──→ newsletter_link_clicks  (Listmonk)

user_journey ──N:1──→ leads      (email_hash 串联)
   │
   │ aggregated weekly
   ▼
weekly_aggregates  (5 维 × N 值 / 周 · 喂 Reporter + Ranker 调权)

heartbeat        (每个 job 跑完写 1 行)
publish_failures (P0/P1/P2 告警留底)
schema_migrations (DDL 版本)
```

---

## 3 · 跨模块调用图

```
                   ┌──────────────┐
                   │ lib/db.py    │ ←─── 所有模块都过这层（禁直连 sqlite3）
                   └──────────────┘
                          ▲
        ┌─────────────────┼──────────────────┐
        │                 │                  │
   ┌────────────┐  ┌─────────────┐   ┌──────────────┐
   │ lib/llm    │  │ lib/lark    │   │ lib/retry    │
   │ MiniMaxi+  │  │ P0/P1/P2    │   │ tenacity     │
   │ Opus 4.7   │  │ webhook     │   │ wrapper      │
   └────────────┘  └─────────────┘   └──────────────┘
        ▲                                    ▲
        │            ┌─────────────┐         │
        │            │ lib/utm     │         │
        │            │ lib/shlink  │         │
        │            └─────────────┘         │
        │                  ▲                 │
        │                  │                 │
   ┌────┴─────────────────┴─────────────────┴───────┐
   │ jobs/* (10 个)                                  │
   │  voice_checker, utm_generator,                  │
   │  adapter_orchestrator, update_btouch,           │
   │  topic_ranker, metrics_collector,               │
   │  attribution_engine, kol_watch,                 │
   │  weekly_reporter, monthly_reporter              │
   └─────────────┬──────────────────────────────────┘
                 │
        ┌────────┴──────────┐
        ▼                   ▼
   ┌──────────────┐   ┌──────────────────┐
   │ sources/*    │   │ runtime/drafts/* │
   │ postiz       │   │ state.db         │
   │ twitter_x    │   │ logs/            │
   │ ga4          │   │ backups/         │
   │ listmonk     │   └──────────────────┘
   │ btouch       │
   └──────────────┘
```

---

## 4 · 状态机（pieces.state 枚举）

```
candidate ──→ selected ──→ drafted ──→ reviewed ──→ scheduled ──→ published ──→ measured
   ▲             │            │             │             │            │
   │             │            │             │             │            │
   │      Topic Ranker     Cowork       critic +     Donald         Postiz /
   │      Sun 20:00       起草 skill   voice_check  终审 Wed       Listmonk /
   │                                                  09-12        Btouch
   │
   └─── 6 路信号源（KOL/Newsletter/竞品/客户/搜索/Donald）
```

每次 state 切换都会在 `state_events` 表写 1 行（actor + 时间戳 + notes）。

---

## 5 · 调度时间表

| 时间 | Job | 写入表 |
|---|---|---|
| 每日 20:00 | metrics_collector | metrics_daily / landing_metrics / newsletter_* / btouch_daily |
| 每日 21:00 | attribution_engine | user_journey / leads |
| 每日 23:00 | backup_sqlite | (备份文件) |
| 周日 10:00 | kol_watch | candidates (route=2) / kol_topics_*.json |
| 周日 18:00 | weekly_reporter | weekly_aggregates / weekly_report_*.md |
| 周日 20:00 | topic_ranker | candidates (status=picked) / selection_*.md |
| 周一 09:00 | update_btouch | (TaskOn 平台 6 触点) |
| 月末周日 19:00 | monthly_reporter | monthly_report_*.md |

---

## 6 · 与开源框架的边界

| 谁的事 | 我们写代码？ |
|---|---|
| MoneyPrinterTurbo 视频生成 | ❌ Cowork `mpt-video` skill 调它 API |
| Postiz 发布调度 | ❌ Public API 调用而已（sources/postiz.py） |
| Listmonk Newsletter | ❌ REST API 调用（sources/listmonk.py） + newsletter/ 子模块部署 |
| Cowork 4 个内容 skill | ❌ Cowork 桌面 |
| `taskon-content-critic` plugin | ❌ Cowork plugin |
| Cowork artifact Dashboard | ❌ Cowork artifact 读 state.db |
| **15 张表 + 10 个 job + 5 个 source adapter** | ✅ **本仓** |

---

## 7 · 失败语义（critical）

| 模块 | 失败时怎么办 |
|---|---|
| lib/lark | swallow exception · log WARNING · 永不 crash 调用者 |
| lib/retry | exhaust max_attempts → P1 告警 → 抛 |
| lib/llm_client | MiniMaxi 全失败 → Anthropic fallback → 仍失败 → P1 告警 → 抛 LLMClientError |
| lib/shlink | 抛 ShlinkError，jobs/utm_generator 接住 fallback 到长链 |
| sources/* | 抛 *Error，jobs 接住，写 publish_failures + 继续下一源 |
| jobs/metrics_collector | 5 源中 1 源挂 → 跳过 + P1 → heartbeat status=warning，其他源继续 |
| jobs/attribution_engine | 5 维中某维查询挂 → 跳过 + P2 → 其他维继续 |
| jobs/weekly_reporter | LLM 失败 → P1 → 退化成"裸数据 markdown"（未实现，TODO） |

**纪律**：所有 job 入口必须 `try: ... finally: db.heartbeat.record(...)` 包裹，保证心跳一定写。

---

## 8 · 性能基线（PRD §8.3）

| 模块 | 数据规模 | 期望耗时 |
|---|---|---|
| voice_checker | 1 条 X Thread | <500ms |
| utm_generator | 1 条 5 平台 | <2s |
| adapter (X→LinkedIn) | 1 条 | 8-15s（LLM） |
| topic_ranker | 30 候选 | 60-90s（30 次 LLM） |
| metrics_collector | 5 源 1 天 | <60s |
| attribution_engine | 50 新 lead | <30s |
| weekly_reporter | 30 piece + agg | 30-60s |

超出 2× 期望耗时 → 进 troubleshooting.md §9。
