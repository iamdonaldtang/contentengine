# Architecture

> 一张图 + 17 张表关系 + 跨模块调用图 + 异步架构 + B3 算法借力闭环.
>
> Last updated: 2026-05-19 (W22-W24 ship: T-02..T-08 · 4 new jobs · 3 migrations · 17 tables · 16 cron). 详细 PRD 见根目录三份 .md.

---

## 1 · 端到端数据流

```
┌────────────────────────────────────────────────────────────────────────┐
│  Cowork 主驾驶舱（Donald 桌面）                                          │
│   起草 skill / 评审 critic plugin / artifact dashboard / 月报           │
└─────────┬───────────────────────────────────────────────┬──────────────┘
          │ 起草 + 评审 (人工 + Cowork)                     │ 读 dashboard
          │ Cowork 也可 POST /admin/run_publish 远程触发    │
          ▼                                                ▲
┌─────────────────────────────────────────────────────────────────────┐
│ engine/  ★ 本仓                                                      │
│                                                                      │
│  jobs/topic_ranker     ──→ runtime/selection_<week>.md               │
│       ▲   ▲                                                          │
│       │   │ weight from weekly_aggregates                            │
│  jobs/kol_watch        ──→ candidates (source_route=2)               │
│                                                                      │
│  candidates pool ─→ Ranker ─→ Cowork 起草 ─→ drafts/<id>/*.md        │
│         (Cowork 写 markdown 含 {{CTA_URL}} 占位符 ★ 2026-05-16)       │
│                                                  │                   │
│  jobs/adapter_orchestrator  ←──────── selection card + xthread       │
│       └─→ linkedin/medium/carousel/shorts                            │
│       └─→ jobs/voice_checker  ──→ voice_report.md                    │
│       └─→ jobs/utm_generator  ──→ utm_links.json (含 short + long)   │
│                                                                      │
│  ── 异步视频管线（A-design webhook）─────────────────                 │
│  jobs/mpt_runner (Mon 11:00) ─→ MPT API submit + return < 1s         │
│         │              ↓                                             │
│         │       mpt_tasks 表 (pending_submit→submitted)              │
│         │                                                            │
│         │   MPT 渲染 (5-40 min) ─→ POST /api/mpt-callback (HMAC)     │
│         │                                       │                    │
│         │                                       ▼                    │
│         │   ingestion/mpt_callback ─→ jobs/mpt_post_callback         │
│         │                            (daemon thread 下载 mp4)        │
│         │                                                            │
│         └── jobs/mpt_reconciler (every 5 min) 兜底丢失 callback      │
│                                                                      │
│  jobs/schedule_planner (Sun 22:00) ─→ inject_cta(content, long_url)  │
│        │            └─→ sign_media_url(piece, file) → Postiz fetch   │
│        ▼                                                             │
│  Postiz Public API → YouTube / LinkedIn / X / Medium / Newsletter    │
│        │                                                             │
│        └─→ Postiz 拉 GET /api/media/<piece>/<file>?expires&sig       │
│                  (cloudflared tunnel · 签 URL · 1h TTL)              │
│                                                                      │
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
│  ── B3 algorithm-borrow nudges (W22-W24 ship 2026-05-19) ───────     │
│  jobs/reply_density_alert (every */10 min) ──→ Lark P2 + sets        │
│         publishings.reply_alert_sent                                 │
│  jobs/linkedin_engagement_alert (every */10 min) ──→ Lark P2 + sets  │
│         publishings.engagement_alert_sent                            │
│  jobs/custom_slice_generator (manual / post-adapter) ──→ MiniMaxi    │
│         LLM → runtime/drafts/<p>/custom_slice_<handle>.{md,canva.json}│
│  jobs/kol_relation_tracker (daily 09:01 · opt-in cron)               │
│         log-dm CLI: writes kol_dm_log                                │
│         scan: GET X API → marks kol_replied_at, tier auto-promote    │
│                                                                      │
│  SQLite state.db ★ 唯一真相源 (17 张表 / 12 migrations)              │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2 · 17 张表关系

```
pieces ──1:N──→ state_events           (审计日志)
   │
   │ 1:N
   ▼
publishings ──1:N──→ metrics_daily     (30m / 24h / 7d 三时点)
   │  ★ migration 011 (2026-05-19): scheduled_at column + composite index
   │     (platform, scheduled_at) — T-05/T-07 30min alert anchor
   │  ★ migration 010 (2026-05-19): reply_alert_sent + engagement_alert_sent
   │     sentinel columns — fire-once-per-row idempotency

mpt_tasks ★ async render state machine (2026-05-16 加入)
   pending_submit → submitted → completed | failed | stale
   (FK → pieces.id; reconciler 自愈丢失 callback)

candidates  (路 1-6 汇合 · 被 Ranker 消费 → 升级为 pieces)
kol_watchlist (KOL Watch 写入 → 候选池路 2)
   ▲
   │ soft FK on handle · log-dm auto-upsert · scan tier-promote
   │
kol_dm_log ★ migration 012 (2026-05-19): KOL 触达日志
   kind: reply | dm | quote | custom_slice
   sent_at → kol_replied_at (scan polls X API) → kol_quote_count → tier B→A

landing_metrics (GA4)
btouch_daily    (TaskOn 6 触点)
newsletter_campaigns ──1:N──→ newsletter_link_clicks  (Listmonk)

user_journey ──N:1──→ leads      (email_hash + cookie_id stitching)
   │
   │ aggregated weekly
   ▼
weekly_aggregates  (5 维 × N 值 / 周 · 喂 Reporter + Ranker 调权)

heartbeat        (每个 job 跑完写 1 行)
publish_failures (P0/P1/P2 告警留底)
schema_migrations (DDL 版本 · 当前 12 个)
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
        │      ┌───────────────────────┐    │
        │      │ lib/utm  · lib/shlink │    │
        │      │ lib/media_url ★ HMAC  │    │ ★ 2026-05-16
        │      │ lib/content_inject ★  │    │
        │      │ lib/yt_metadata       │    │
        │      └───────────────────────┘    │
        │                  ▲                │
   ┌────┴─────────────────┴────────────────┴───────┐
   │ jobs/* (16 个 cron + 2 manual / opt-in)        │
   │  voice_checker (T-04: +X 主推无外链规则),       │
   │  utm_generator,                                │
   │  adapter_orchestrator, update_btouch,          │
   │  topic_ranker, metrics_collector,              │
   │  attribution_engine, kol_watch,                │
   │  weekly_reporter, monthly_reporter,            │
   │  mpt_runner ★ async submit-and-exit,           │
   │  mpt_reconciler ★ every-5min self-heal         │
   │  reply_density_alert ★★ every */10 min (T-05)  │
   │  linkedin_engagement_alert ★★ */10 min (T-07)  │
   │  custom_slice_generator ★★ manual (T-02)       │
   │  kol_relation_tracker ★★ daily opt-in (T-03)   │
   │  + jobs/mpt_post_callback (shared helper)      │
   └─────────────┬──────────────────────────────────┘
                 │
        ┌────────┴──────────┐
        ▼                   ▼
   ┌──────────────┐   ┌──────────────────┐
   │ sources/*    │   │ runtime/         │
   │ postiz       │   │ state.db         │
   │ twitter_x    │   │ drafts/<piece>/  │
   │ ga4          │   │   *.md (Cowork)  │
   │ listmonk     │   │   *.mp4 (MPT)    │
   │ btouch       │   │   *.yaml         │
   │ mpt ★ async  │   │   *.json (utm)   │
   └──────────────┘   │ logs/ · backups/ │
                      └──────────────────┘

   ┌─────────────────────────────────────────────────────────┐
   │ ingestion/ Flask app (gunicorn :5051)                    │
   │   /api/landing-signup     (POST · HMAC opt · CORS)       │
   │   /api/listmonk-webhook   (POST · campaign click track)  │
   │   /api/ses-bounce         (POST · SNS bounce/complaint)  │
   │   /api/mpt-callback ★     (POST · HMAC · MPT webhook)    │
   │   /api/media/<p>/<f>  ★   (GET  · signed URL · stream)   │
   │   /admin/run_publish ★    (POST · Bearer · async task)   │
   │   /admin/run_metrics ★    (POST · Bearer · async task)   │
   │   /admin/tasks/<id> ★     (GET  · Bearer · poll status)  │
   │   /admin/health/all ★     (GET  · Bearer · subsystems)   │
   │   /admin/restart_signal ★ (POST · Bearer · sentinel)     │
   │   /health · /metrics                                     │
   └─────────────────────────────────────────────────────────┘
```

---

## 4 · 状态机

### 4.1 · pieces.state（内容生命周期）

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

### 4.2 · mpt_tasks.status（异步渲染生命周期）★ 2026-05-16

```
pending_submit ─► submitted ─► completed   (callback OR reconciler 抢一次)
              │              ├► failed
              │              └► stale       (>6h, reconciler-only)
              └► failed                      (submit POST 出错, 未拿到 task_id)
```

`UPDATE ... WHERE status='submitted'` 原子转移 + 幂等。`terminal_source` 列记 `'callback'` / `'reconciler'`。`callback_received_at` 仅 callback-won 时设。

---

## 5 · 调度时间表

| 时间 | Job | 写入表 |
|---|---|---|
| 每 5 分钟 | **mpt_reconciler ★** | mpt_tasks (自愈丢失 callback) |
| 每 5 分钟 | container_heartbeat | heartbeat (容器存活心跳) |
| **每 10 分钟** | **reply_density_alert ★★** | publishings.reply_alert_sent · Lark P2 nudge (B3 §2 杠杆 1) |
| **每 10 分钟** | **linkedin_engagement_alert ★★** | publishings.engagement_alert_sent · Lark P2 nudge (B3 §4 杠杆 1) |
| 每日 08:30 | kol_daily_replier | runtime/kol_reply_candidates_*.md (旁支 · cron 暂注释) |
| **每日 09:01 (opt-in)** | **kol_relation_tracker scan ★★** | kol_dm_log.kol_replied_at + kol_watchlist.tier (旁支 · cron 暂注释 · uncomment 后启用) |
| 每日 20:00 | metrics_collector | metrics_daily / landing_metrics / newsletter_* / btouch_daily |
| 每日 21:00 | attribution_engine | user_journey / leads (4 模型 last/first/linear/markov) |
| 每日 23:00 | backup_sqlite | (备份文件) |
| 周一 09:00 | update_btouch | (TaskOn 平台 6 触点) |
| **周一 11:00** | **mpt_runner ★** | mpt_tasks (submit-and-exit, 1s) |
| 周日 10:00 | kol_watch | candidates (route=2) / kol_topics_*.json |
| 周日 18:00 | weekly_reporter | weekly_aggregates / weekly_report_*.md |
| 周日 20:00 | topic_ranker | candidates (status=picked) / selection_*.md |
| 周日 22:00 | **schedule_planner ★** | publishings (inject_cta + signed media URL + scheduled_at + role/account) |
| 月 25 日 09:00 | newsletter_assembler | runtime/newsletter_draft_*.md (dry-run · 旁支 · cron 暂注释) |
| 月末周日 19:00 | monthly_reporter | monthly_report_*.md |
| 月末周日 19:30+ | cohort_analysis / ab_aggregator / channel_attribution | aggregated tables |
| **manual / post-adapter** | **custom_slice_generator ★★** | runtime/drafts/<piece>/custom_slice_*.{md,canva.json} · KOL DM 草稿 (B3 §1.3 模型 4) |

★ = A-design async webhook 改造涉及的 cron（2026-05-16）
★★ = W22-W24 ship (2026-05-19) · B3 算法借力 + KOL 触达 + 矩阵号路由 (T-02..T-08)

---

## 6 · 与开源框架的边界

| 谁的事 | 我们写代码？ |
|---|---|
| **MoneyPrinterTurbo 视频渲染** | ⚠️ 改成 **engine 直接调** `sources/mpt.py` async client。Cowork 的 `mpt-video` skill 已废弃。MPT 端 ship 了 webhook callback（HMAC-SHA256），engine 端 ship 了 ingestion 接收 + reconciler 兜底。详见 §9。|
| Postiz 发布调度 | ⚠️ Public API 调用（`sources/postiz.py`）。需要本地 schema 适配：tags `[{value, label}]` + image `[{id, path}]`。媒体由 engine 公开签名 URL 提供，详见 §10。|
| Listmonk Newsletter | ❌ REST API 调用（`sources/listmonk.py`）+ newsletter/ 子模块部署（待部署） |
| Cowork 4 个内容 skill | ❌ Cowork 桌面（写 markdown 含 `{{CTA_URL}}` 占位符） |
| `taskon-content-critic` plugin | ❌ Cowork plugin (评审 10 维独占) |
| Cowork artifact Dashboard | ❌ Cowork artifact 读 state.db |
| **16 表 + 12 cron + 5 admin endpoint + 1 callback + 1 media + 6 source adapter + 9 lib** | ✅ **本仓** |

---

## 7 · 失败语义（critical）

| 模块 | 失败时怎么办 |
|---|---|
| lib/lark | swallow exception · log WARNING · 永不 crash 调用者 |
| lib/retry | exhaust max_attempts → P1 告警 → 抛 |
| lib/llm_client | MiniMaxi 全失败 → Anthropic fallback → 仍失败 → P1 告警 → 抛 LLMClientError |
| lib/shlink | 抛 ShlinkError，jobs/utm_generator 接住 fallback 到长链 |
| lib/media_url | sign 时 MEDIA_URL_SECRET 空 → raise MediaUrlConfigError；verify 时返 `(False, reason)` |
| lib/content_inject | strict=False 默认 → 找不到占位符 fallback append + WARN；strict=True → raise MissingPlaceholderError |
| sources/* | 抛 *Error，jobs 接住，写 publish_failures + 继续下一源 |
| sources/mpt (async) | submit 出错 → mpt_tasks.mark_submit_failed + P1；callback 验证失败 → 401 + audit log；reconciler 5min 后兜底 |
| jobs/metrics_collector | 5 源中 1 源挂 → 跳过 + P1 → heartbeat status=warning，其他源继续 |
| jobs/attribution_engine | 5 维中某维查询挂 → 跳过 + P2 → 其他维继续 |
| jobs/schedule_planner | 视频平台缺 mp4 → 该平台 fail（不发出去给 Postiz 看 Invalid URL）；缺 CTA URL → P2 + content 仍发布（无归因） |
| jobs/mpt_runner (async) | 写 mpt_tasks pending_submit 行 → MPT POST 失败 → mark_submit_failed + P1。**不再阻塞 5-10 min**。|
| jobs/mpt_reconciler | 单行 handler 崩 → 继续下一行（不抢 batch）；6h 不到 terminal → mark stale + P1 |
| jobs/weekly_reporter | LLM 失败 → P1 → 退化成"裸数据 markdown" (`render_bare_report`) |
| **jobs/reply_density_alert** (T-05) | 单行 Lark 失败 → counts['errors']++ + 继续下一行；DB UPDATE 失败 → 同；空结果 → status='ok' rows=0 |
| **jobs/linkedin_engagement_alert** (T-07) | 同上 · Lark + DB 失败不传染下一行；nudge-only,永不调 LinkedIn API |
| **jobs/custom_slice_generator** (T-02) | LLM 失败 → 单 KOL skip + P2;全失败(0 generated)→ P1 + status='failed';URL 漏出 → server 端 regex scrub 成 `[URL-removed-by-engine]` |
| **jobs/kol_relation_tracker** (T-03) | X API 失败 → P2 + 单行 last_checked_at 续 stamping(防 tight loop);全失败 → status='warning';tier promote 失败 → P2 不阻塞其他行 |

**纪律**：所有 job 入口必须 `try: ... finally: db.heartbeat.record(...)` 包裹，保证心跳一定写。

---

## 8 · 性能基线（PRD §8.3 + A-design 后基线）

| 模块 | 数据规模 | 期望耗时 |
|---|---|---|
| voice_checker | 1 条 X Thread | <500ms |
| utm_generator | 1 条 5 平台 | <2s |
| adapter (X→LinkedIn) | 1 条 | 8-15s（LLM） |
| topic_ranker | 30 候选 | 60-90s（30 次 LLM） |
| metrics_collector | 5 源 1 天 | <60s |
| attribution_engine | 50 新 lead | <30s |
| weekly_reporter | 30 piece + agg | 30-60s |
| **mpt_runner submit** ★ | 1 piece | **<1s** (async; 旧版 5-10 min 阻塞已废弃) |
| **mpt_reconciler** ★ | ≤50 行扫描 + GET MPT | <10s 一轮（多数 tick 无事） |
| **/api/mpt-callback** ★ | HMAC verify + transition + spawn | <100ms 同步 (下载在 daemon thread) |
| **/api/media/<p>/<f>** ★ | 30MB mp4 流式 | bandwidth-bound (Range + 304 支持) |
| **schedule_planner** | 1 piece 6 平台 | 20-40s (含 yt_metadata LLM derive) |
| **reply_density_alert** ★★ | 0-N rows in 2-min window | <1s 一轮（多数 tick 0 行）|
| **linkedin_engagement_alert** ★★ | 同上 | <1s 一轮 |
| **custom_slice_generator** ★★ | 1 piece × 3 KOL | 12-30s (3 次 LLM JSON) |
| **kol_relation_tracker scan** ★★ | ≤50 rows × X API | <30s 一轮（少数 tick 真有 reply）|

超出 2× 期望耗时 → 进 [troubleshooting.md §9](troubleshooting.md)。

---

## 9 · 异步 webhook 架构（A-design 2026-05-16）★ 新增

### 9.1 · 为什么改

旧 sync poll-block：`mpt_runner` 调 MPT submit → 自己 poll 每 5 秒一次直到 terminal → 下载 → 写表。整个 cron 阻塞 5-10 分钟，吃住 engine 主线程。MPT 超时被传染成 engine 端 P1 故障。

A-design async：engine submit + 退出 < 1 秒。MPT 完成后主动 POST 回 engine。完整 contract：

### 9.2 · 时序图

```
[mpt_runner CLI]              [engine ingestion]              [MPT container]
       │                              │                              │
       │ INSERT mpt_tasks             │                              │
       │ (status=pending_submit)      │                              │
       │────────────────────────────►│                              │
       │                              │                              │
       │ POST /api/v1/videos          │                              │
       │   + callback_url + secret    │                              │
       │─────────────────────────────────────────────────────────►│
       │                              │                              │
       │  ◄── {task_id: 4c48b7ae...} ◄──────────────────────────────│
       │                              │                              │
       │ UPDATE mpt_tasks             │                              │
       │ (task_id=..., submitted)     │                              │
       │ ✓ EXIT (< 1s 总耗时)         │                              │
       X                              │                              │
                                      │       (MPT 渲染 5-40 min)    │
                                      │                              │
                                      │  ◄── POST /api/mpt-callback  │
                                      │       HMAC-SHA256 signed     │
                                      │       (status,mp4_url,error) │
                                      │                              │
                                      │ [verify HMAC + ts < 5min]    │
                                      │ atomic mark_completed         │
                                      │ spawn download daemon thread  │
                                      │ ─── 202 OK ──────────────►  │
                                      │                              │
                                      │ daemon: GET mp4_url stream   │
                                      │      → runtime/drafts/.../   │
                                      │        shorts_60s.mp4         │
                                      │      → set_media_path()       │
```

### 9.3 · HMAC 契约（engine ↔ MPT 端跨 repo）

* **算法**：HMAC-SHA256
* **签名字符串**：`f"{X-MPT-Timestamp}.{raw_body}"`（timestamp 在前；body 用 `json.dumps(payload, sort_keys=True, ensure_ascii=False)`）
* **Header**：`X-MPT-Signature: sha256=<hex>` + `X-MPT-Timestamp: <unix>`
* **Timestamp 容忍**：±300s（防回放 + 防时钟漂移）
* **Secret 来源**：engine 每次 submit 时把 `callback_secret` 字段塞进 body 传给 MPT（MPT 不持久化、不日志）
* **Body 验证**：engine 端用 `request.get_data()` 拿 raw bytes，不 round-trip JSON（Flask 的 JSON 解析会改 whitespace 打散签名）

### 9.4 · 可靠性矩阵

| 失败场景 | 兜底 | 修复延迟 |
|---|---|---|
| MPT worker 死锁 | reconciler 5min GET MPT → state stuck → 6h 后标 stale + P1 | ≤ 6h |
| MPT 网络挂 POST callback | MPT 端 3 次重试 1+2+4s → 都失败写 DLQ；engine reconciler 5min 后 GET MPT → simulate_callback | ≤ 5min |
| Engine 重启时 callback 抵达 | callback HTTP 5xx → MPT DLQ；engine 启动后 reconciler 5min 内 simulate_callback | ≤ 5min |
| Callback 抵达但 engine 处理中 crash | `status` 卡 'submitted' → reconciler GET MPT → simulate（幂等） | ≤ 5min |
| HMAC 签名失败 | engine 返 401 + audit log + P1 | 立即 |
| Callback 重复（MPT 重试） | engine status='completed' 已 terminal → 返 200 already_processed（幂等） | 立即 |
| MPT 发了 2 次 callback | atomic UPDATE WHERE status='submitted' → 第二次 rowcount=0 → already_processed | 立即 |
| Engine 长期下线 | MPT DLQ 文件堆积；engine 上线后 reconciler 全部 self-heal | 上线 + 5min |

**结论**：A 设计的"丢失场景"被 reconciler 自愈 + 幂等 atomic UPDATE 全兜住。**reconciler 不是可选项；删了 A 设计就垮**。

### 9.5 · 关键文件

* migration: `lib/migrations/009_mpt_tasks.sql`
* state machine: `lib/db._MptTasksAdapter`
* MPT client (async): `sources/mpt.py` (callback_url + callback_secret kwargs)
* engine callback receiver: `ingestion/mpt_callback.py` Blueprint
* download daemon: `jobs/mpt_post_callback.py::spawn_download`
* reconciler: `jobs/mpt_reconciler.py` (cron `*/5 * * * *`)
* submit-and-exit cron: `jobs/mpt_runner.py` (cron `0 11 * * 1`)

---

## 10 · CTA 占位符注入 + 公开媒体端点（2026-05-16）★ 新增

### 10.1 · 为什么需要

两个独立但相邻的问题：
1. **归因闭环**：`utm_generator` 写 utm_links.json，但 schedule_planner 没把 URL 塞进 post content → YT description / LinkedIn post 都没带 UTM → GA4 看不到流量来源
2. **媒体上传**：Postiz YouTube provider 需要 HTTPS URL 拉 mp4 → engine mp4 在容器本地 disk + Postiz SSRF 拦截私有 IP → 必须公网签名 URL

### 10.2 · CTA `{{CTA_URL}}` 占位符流程

```
Cowork 写 markdown / yt_metadata LLM 派生 description
       │
       │ 必须含 {{CTA_URL}} 占位符（而不是真实 URL）
       │ marketing/CLAUDE.md §4.5 内容创作 SOP
       ▼
schedule_planner 调 inject_cta(content, utm_links[network+'_'+account][long_url])
       │
       │ inject_cta 替换所有 {{CTA_URL}} 为真实 URL
       │ strict=False 时找不到占位符 fallback append + WARN（兼容旧 markdown）
       ▼
postiz.create_post(content=injected_content, extra_settings={description: injected_desc}, ...)
```

**配置**（`config.yaml :: postiz`）：
* `accounts` — 每平台默认账号映射（当前全 donald_en）
* `cta_url_kind` — `long_url` 或 `short_url`（H.1 当前用 long_url；l.taskon.xyz 上 tunnel 后切 short）

**关键文件**：
* `lib/content_inject.py` (PLACEHOLDER, inject_cta, has_placeholder, count_placeholder)
* `config/prompts/yt_metadata.txt` (强制 LLM 输出 `{{CTA_URL}}` 占位符)

### 10.3 · 公开签名媒体端点（B 路径）

```
schedule_planner ──→ lib.media_url.sign_media_url(piece_id, filename)
       │                ↓ HMAC-SHA256
       │           https://ingest.taskon.xyz/api/media/<piece>/<file>
       │                ?expires=<unix>&sig=<hex>
       ▼
postiz.create_post(media_urls=[{id, path: signed_url}], ...)
       │
       │ Postiz 排程到时间
       ▼
Postiz container ──→ GET signed_url (cloudflared tunnel)
       │
       │ engine ingestion/media_routes verify HMAC + expires + path traversal
       │ → send_file 流式 mp4 (Range + 304 支持)
       ▼
Postiz uploads to YouTube via OAuth token
```

**HMAC 签名串**：`f"{piece_id}/{filename}.{expires}"` （signing payload）  
**TTL**：默认 1h（Postiz publish 时 fetch 一次足够）  
**SSRF 注意**：Postiz `/public/stream?url=` 拒绝私有 IP，必须公网 URL（cloudflared tunnel）

**关键文件**：
* `lib/media_url.py` (sign_media_url, verify_media_url)
* `ingestion/media_routes.py` Blueprint
* `sources/postiz.create_post` (media_urls → `[{id, path}]` MediaDto shape)

### 10.4 · 完整 piece 发布 10 环链路

```
1. mpt_runner submit-and-exit (1s)                ★ §9
2. MPT 渲染 mp4 (5-40 min)
3. MPT POST callback HMAC-signed                  ★ §9
4. engine /api/mpt-callback 验签 + 异步下载       ★ §9
5. publish_immediate / schedule_planner cron
6. schedule_planner inject_cta + sign_media_url   ★ §10
7. Postiz 拉 mp4 via cloudflared                  ★ §10
8. engine /api/media verify HMAC + stream mp4     ★ §10
9. Postiz upload to YouTube via OAuth
10. YouTube publish → video_id
```

**任何一环断 = 不上 YT**。完整链路于 2026-05-16 跑通：[YT video es7XQWghoSM](https://www.youtube.com/watch?v=es7XQWghoSM)（piece 02 首次真上线）。

---

## 11 · 配置层（env + config.yaml）速查

### .env（运行时密钥）

```
# 核心
SQLITE_PATH=/app/runtime/state.db
MINIMAX_API_KEY=...                # LLM 主链
ANTHROPIC_API_KEY=...               # LLM fallback

# A-design async
MPT_API_BASE=http://moneyprinterturbo-api:8090
MPT_CALLBACK_URL=http://taskon-ingestion:5051/api/mpt-callback
MPT_CALLBACK_SECRET=<openssl rand -hex 32>   # ★ HMAC 共享 secret

# 公开签名媒体
MEDIA_URL_BASE=https://ingest.taskon.xyz
MEDIA_URL_SECRET=<openssl rand -hex 32>      # ★ HMAC 共享 secret

# Cowork 远程触发
ADMIN_API_TOKEN=<bearer token>               # ★ 空 = endpoint 禁用

# 外部
POSTIZ_BASE_URL=http://postiz:3000
POSTIZ_API_KEY=...
SHLINK_BASE_URL=http://shlink:8085
SHLINK_API_KEY=...
LARK_WEBHOOK_URL=...
GA4_PROPERTY_ID=...
LISTMONK_BASE_URL=...
LISTMONK_USERNAME=...
LISTMONK_PASSWORD=...
TASKON_ADMIN_TOKEN=...               # update_btouch
X_BEARER_TOKEN=...                   # kol_watch 主路径
```

### config.yaml（业务配置）

```yaml
postiz:
  integrations:                # platform → Postiz UUID
    linkedin_post: cmp6765x6...
    yt_shorts: cmp6azvuh...
    ...
  accounts:                    # platform → utm_links.json account 键
    yt_shorts: donald_en       # ★ 2026-05-16 新增
    linkedin_post: donald_en
    ...
  cta_url_kind: long_url       # ★ H.1 决策：long_url（待 l.taskon.xyz 上 tunnel 后切 short_url）

  # ★ 2026-05-19 新增 (T-08) · 矩阵号 cross-post 路由 (B3 §5.2)
  routing:                     # platform → cross_post[{account, integration_id, offset_minutes}]
    yt_shorts:
      cross_post: []           # 默认空 · 等 Donald 连 2nd YT (taskon_official) 后 populate
    linkedin_post:
      cross_post: []
    x_thread:
      cross_post: []           # ★ DO NOT POPULATE (B3 §2 杠杆 1 27-人 Quote 算法降权)
    ...

schedule_planner:
  slots: {...}                 # 跨平台错峰窗口
  draft_filenames: {...}       # platform → markdown 文件名
```

---

## 12 · B3 算法借力 + KOL 触达 + 矩阵号路由（2026-05-19）★ 新增

### 12.1 · 为什么需要

B3 模块（内容分发与放大）有 7 段：KOL SOP / X 算法 / YT 算法 / LinkedIn 算法 / 矩阵协同 / 客户联创 / 投稿 Space。其中 6 段需要 engine 支持（客户联创 + 投稿是纯 BD 人工动作）。W22-W24 三周 ship 了 T-02..T-08 共 7 个工程任务。

完整 roadmap → [B3_engine_落地路线_v1.md](B3_engine_落地路线_v1.md)。

### 12.2 · 数据流（4 路新增 + 1 路加强）

```
[T-04 · voice_checker 加 X 主推无外链规则]
  drafts/<piece>/xthread_final.md
       ↓ jobs.voice_checker
  voice_report_x_thread.md (新增 "Algorithm Rules" 段)
       ↓ Donald 评审
  通过 → state=reviewed；FAIL → 兼职女生改稿

[T-05 · X Reply 密度提醒 / T-07 · LinkedIn 回评提醒]
  publishings (scheduled_at + role + account)
       ↓ schedule_planner 周日 22:00 写入 (★ migration 011)
  scheduled_at = Postiz-promised 发布时刻 (UTC)
       ↓ Postiz 真发
  实际发布时刻 ≈ scheduled_at
       ↓ */10 min cron tick
  reply_density_alert / linkedin_engagement_alert 扫
       ↓ if scheduled_at in [now-31min, now-29min]
       ↓    AND <col>_alert_sent IS NULL
  Lark P2 nudge + stamp publishings.<col>_alert_sent (idempotent)

[T-02 · Custom Slice]
  drafts/<piece>/selection_card.yaml + config/kol_watchlist.yaml
       ↓ jobs.custom_slice_generator (manual / post-adapter)
       ↓ 1. token-overlap match → top-N KOLs (tier A 优先)
       ↓ 2. for each KOL: llm.complete_json(custom_slice prompt)
       ↓ 3. URL scrub + write outputs
  drafts/<piece>/custom_slice_<handle>.md  (DM 草稿 · Donald 手发)
  drafts/<piece>/custom_slice_<handle>.canva.json  (兼职女生 Canva 改图参数)

[T-03 · KOL 关系状态机]
  Donald 手发 Reply/DM → CLI: kol_relation_tracker log-dm ...
       ↓ INSERT kol_dm_log (kol_handle, kind, donald_tweet_id, sent_at)
       ↓ UPSERT kol_watchlist.last_dm_date
  daily 09:01 cron (opt-in) → kol_relation_tracker scan
       ↓ for each kol_dm_log WHERE kol_replied_at IS NULL AND sent_at > now-7d
       ↓   GET X API replies(donald_tweet_id) → find reply by KOL author_id
       ↓ if found: stamp kol_replied_at, kol_reply_tweet_id
       ↓ check 90d quote count → if ≥ 3: UPSERT kol_watchlist.tier = 'A' + P2 Lark

[T-08 · Matrix routing]
  config.yaml :: postiz.routing.<platform>.cross_post
       ↓ schedule_planner.build_schedule()
       ↓ for each platform: 1 primary plan + N cross_post plans (offset_minutes 延后)
  publishings (1 piece × M platforms × (1 primary + K cross_post) rows)
       ↓ Postiz 按各自 scheduled_at + integration_id 发到不同账号
```

### 12.3 · 关键文件

* T-04: `jobs/voice_checker.py` (`_check_x_first_tweet_no_https` + `algo_rule_violations` field)
* T-05: `jobs/reply_density_alert.py` · cron `*/10 * * * *`
* T-07: `jobs/linkedin_engagement_alert.py` · cron `*/10 * * * *`
* T-02: `jobs/custom_slice_generator.py` + `config/prompts/custom_slice.txt`
* T-03: `jobs/kol_relation_tracker.py` (log-dm + scan) + `lib/migrations/012_kol_dm_log.sql`
* T-06: `lib/yt_metadata.py` (title_variants + thumbnail_specs fields) + `config/prompts/yt_metadata.txt`
* T-08: `config.yaml :: postiz.routing` + `jobs/schedule_planner.py` (build_schedule emits primary+cross_post)
* migrations: 010 (publishings.{reply,engagement}_alert_sent) · 011 (publishings.scheduled_at + index) · 012 (kol_dm_log table)

### 12.4 · 红线（不可让渡）

* **engine 永不发推 / 永不 DM / 永不评 LinkedIn** · T-05/T-07 只产 Lark P2 提醒;T-02 只产 markdown 草稿;T-03 scan 只读 X API 反应,不写 X
* **X Quote chain 永不启用** (B3 §2 杠杆 1) · config.yaml routing.x_thread / x_short 标 ★ DO NOT POPULATE
* **LLM 永远只用 MiniMaxi 包月** · 不加 Anthropic per-token fallback (user_llm_cost_constraint)
* **KOL 旁支永不传染主链** · T-02/T-03 失败 → P2 + skip · 不阻塞主链 publish

---

**变更记录**：

- 2026-05-13 · 初版（10 jobs + 15 表）
- 2026-05-15 · 加 mpt_runner / utm_generator / 落地页 dist
- **2026-05-16 · A-design async webhook + signed media + CTA placeholder 三大改造完成，piece 02 首次真上 YouTube**
- **2026-05-19 · W22-W24 ship · T-02..T-08 (B3 算法借力 + KOL 触达 + 矩阵号路由) · 4 new jobs · 3 migrations · 17 表 · 16 cron · 加 §12 节 · 249/249 pytest 全过(py3.12.13) · supercronic 2026-05-19 09:01 reload 完成**
