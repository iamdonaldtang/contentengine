# 本系统 ↔ Cowork 协作模型

> **一句话**：**Cowork = 驾驶舱（想 / 评 / 看），engine = 发动机（算 / 改 / 数），中间靠 3 种接触面握手 —— 没有 API，只有文件 + SQLite + docker exec。**
>
> **常态默认**：在**效果最大化**的前提下走**最短可行时间**节奏。**默认 = 一日制流程**（3.5-5h ship 一条）；只有当内容必须深耕（赛道深度长文 / 客户联创 / Benchmark 旗舰）才切多日制。
>
> 配套文档：
> - [cowork_integration.md](cowork_integration.md) —— 17 行决策矩阵对应代码位置 + Cowork prompt 模板
> - [architecture.md](architecture.md) —— 端到端数据流大图 + 17 张表 + 16 cron
> - [Donald_每日操作手册_v1.md](Donald_每日操作手册_v1.md) —— Donald 每日 / 每周时间分配
> - [B3_engine_落地路线_v1.md](B3_engine_落地路线_v1.md) —— W22-W24 任务 T-02..T-08 落地
> - `D:\Taskon\marketing\00_内容营销引擎\全流程规划_v3\C_工具栈职责边界.md` —— 上游规划 17 行决策矩阵 + 6 原则

---

## 1 · 职责分工

### 1.1 · 5 个动词记 5 个系统

```
┌─────────────────────────────────────────────────────────────────┐
│  Cowork（Claude Desktop） · "想" 的事                            │
│    选题决策 · 起草 · 评审 · Dashboard · Newsletter 起草          │
├─────────────────────────────────────────────────────────────────┤
│  engine（本仓 docker）  · "算" 的事                              │
│    评分 · 改写 · UTM · 数据 · 归因 · 报告 · 算法借力提醒          │
├─────────────────────────────────────────────────────────────────┤
│  Postiz · "发" 的事（已部署 :4007）                              │
│  MPT · "剪" 的事（已部署 :8090）                                 │
│  Listmonk + SES · "寄" 的事（未部署）                            │
└─────────────────────────────────────────────────────────────────┘
```

记忆口诀：
- **Cowork 干"想"** —— 选题决策 / 起草 / 评审 / 看板
- **engine 干"算"** —— 评分 / 改写 / UTM / 数据 / 归因 / 报告
- **Postiz 干"发"** —— 定时发布
- **MPT 干"剪"** —— 视频生产
- **Listmonk + SES 干"寄"** —— Newsletter

### 1.2 · 3 个人 × 1 个发动机（执行视角）

```
┌─────────────────────────────────────────────────────────────────┐
│  Cowork (Claude Desktop)  ←→  Donald (人) ←→ 兼职女生 (人)        │
│  ───────────────────────                                         │
│   驾驶舱 · 起草 · 念读 · 排版                                     │
│                          ↕   4 种姿势 (A/B/C/D)                  │
└──────────────────────────┃─────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  engine (Docker container)                                       │
│  ───────────────────                                             │
│   16 cron jobs + 11 HTTP endpoints + 4 source adapters           │
│   永远 headless · 永远不发推 · 永远不 DM · 永远不评 LinkedIn        │
│                                                                  │
│   握手区(双向):                                                   │
│     state.db                ← 唯一真相源(17 张表) → Cowork sql.js │
│     runtime/drafts/<piece>/ ← Cowork 写, engine 读, 兼职女生改    │
└─────────────────────────────────────────────────────────────────┘
```

**关键纪律**：Donald 100% 时间在 Cowork 里对话。engine **没有 UI**，永远是后台。

---

## 2 · 3 种接触面

```
Cowork (Claude Desktop)
    │
    │ ① 文件系统    ──→ runtime/drafts/<piece_id>/*.md
    │                   runtime/*.json
    │                   config/*.yaml
    │
    │ ② SQLite 只读 ──→ runtime/state.db（sql.js in artifact）
    │
    │ ③ docker exec ──→ docker compose exec engine python -m jobs.<name> ...
    │                   (或 ingestion /admin/run_publish 远程触发)
    ▼
engine container（supercronic + 16 cron + 17 张表）
```

**纪律**：Cowork 只 read state.db、never write —— 所有写库走 engine job 或 ingestion `/api/...` POST。

---

## 3 · Cowork 里的 skill / plugin 清单（驾驶舱侧）

| Cowork 件 | 类型 | 干嘛 |
|---|---|---|
| `taskon-content-marketing` | skill | 起 Medium / blog 长文 |
| `crypto-twitter-creator` | skill | 起 X Thread 主稿（**engine 不起 X 主稿**） |
| `youtube-crypto-growth` | skill | 起 YT 短视频脚本 + 弹药选取 |
| `taskon-content-critic` | **plugin** | 内容评审打分（50 分制 · 10 维）· engine 不做 |
| `mpt-video` | skill | 调本机 MPT API 出视频（实验场景 · 生产走 engine async） |
| `email-sequence` | skill | 起 Newsletter 草稿 |
| `crypto-news-aggregator` | skill | 路 5 信号源（搜索 + 新闻） |
| `twitter-search-scraper` | skill | KOL 推文搜索 |
| `performance-report` | skill | 看 weekly_report.md 念给 Donald |
| `brand-review` | skill | Fact-check 找 `[DATA-NEEDED]` + 品牌口径扫 |
| `council-of-minds` | skill | 月度方向校准 · 反向输出叙事锚点候选 |

---

## 4 · 主流程 A · 一日制（默认 · 推荐）

> **定位**：默认走这条 —— 在保持效果最大化的前提下，**3.5-5 小时 ship 一条 piece**。
>
> **适用 ≥ 80% piece**：行业热点回应 / 数据洞察 thread / 项目体检 / 方法论(简版) / 闪电观点。

### 4.1 · 依赖 DAG（无时间约束的纯逻辑序）

```
        ┌────────────────────────────────────────────────────────┐
        │ Stage 1 · 选题 + 起草                                  │
        │                                                        │
        │  signals(parallel)     ─┐                              │
        │    ├ kol_watch          │                              │
        │    ├ news aggregator    │                              │
        │    └ Donald 立场        │                              │
        │                         ▼                              │
        │  topic_ranker (5 维 LLM) ──→ selection_*.md            │
        │                         ▼                              │
        │  ★ Donald 拍板 + selection_card.yaml                    │
        │                         ▼                              │
        │  Cowork crypto-twitter-creator → xthread_final.md      │
        └──────────────────┬─────────────────────────────────────┘
                           ▼
        ┌────────────────────────────────────────────────────────┐
        │ Stage 2 · 机检 + 评审                                  │
        │                                                        │
        │  adapter_orchestrator → 4 平台 + voice_check + Algo Rule│
        │                         ▼                              │
        │  taskon-content-critic (10 维 50 分 · ≥35 放行)         │
        │                         ▼                              │
        │  fact-check + brand-review                              │
        │                         ▼                              │
        │  ★ Donald 终审 (数据关 + 可操作关)                       │
        └──────────────────┬─────────────────────────────────────┘
                           ▼ state=reviewed
        ┌────────────────────────────────────────────────────────┐
        │ Stage 3 · 资产生产 (并行 · 最长一支决定)                │
        │                                                        │
        │  ├ MPT 渲染 ★ HARD FLOOR 5-40 min (异步)                │
        │  ├ Canva 配图 (兼职女生 并行 15-30 min)                  │
        │  ├ yt_metadata.yaml (并行 · 含 title_variants[3])       │
        │  └ utm_generator + shlink (并行 · 2s)                   │
        │                                                        │
        │  ★ Custom Slice DM 草稿 (T-02 · 可选 · 30s LLM × N KOL) │
        └──────────────────┬─────────────────────────────────────┘
                           ▼  (等 mp4 + 所有资产就位)
        ┌────────────────────────────────────────────────────────┐
        │ Stage 4 · 发布                                          │
        │                                                        │
        │  schedule_planner --dry-run (1s)                       │
        │                         ▼                              │
        │  schedule_planner 真排程 OR /admin/run_publish 紧急通道 │
        │                         ▼                              │
        │  Postiz 按 scheduled_at 真发(每平台几秒后)               │
        └──────────────────┬─────────────────────────────────────┘
                           ▼ ★ HARD FLOOR 30 min wait
        ┌────────────────────────────────────────────────────────┐
        │ Stage 5 · 算法借力 (每平台独立 30 min 计时)              │
        │                                                        │
        │  T-05/T-07 cron */10min 自动 Lark P2 提醒                │
        │                         ▼                              │
        │  ★ Donald + 4 BD 5-人 Reply 队伍 (X)                    │
        │  ★ Donald 回 ≥5 LinkedIn 评论                           │
        │  ★ Donald 手发 KOL Custom Slice DM (T-02 草稿用)         │
        │  → kol_relation_tracker log-dm (每次实发记一笔)         │
        └──────────────────┬─────────────────────────────────────┘
                           ▼ ★ HARD FLOOR 24h / 7d 物理墙钟
        ┌────────────────────────────────────────────────────────┐
        │ Stage 6 · 数据回流 (异步 · 永远是 rolling)                │
        │                                                        │
        │  metrics_collector 每日 20:00(30m / 24h / 7d 三时点)    │
        │  attribution_engine 每日 21:00(4 模型)                  │
        │  weekly_reporter / monthly_reporter / cohort / Markov  │
        └────────────────────────────────────────────────────────┘
```

### 4.2 · 3 个不可消除的硬墙钟下限

| 阶段 | 最短墙钟 | 为什么 |
|---|---|---|
| **MPT 视频渲染**（仅当有 yt_shorts/tiktok） | **5-40 min** | TTS + Whisper 字幕 + ffmpeg 编码 · CPU-bound |
| **T-05/T-07 30min 提醒** | **30 min**（每平台独立计时） | 30min 是 X/LinkedIn 算法借力的固定窗口 · B3 §2.1/§4.1 已定 |
| **24h / 7d 指标快照** | **24h / 7d** | metrics_collector 三时点采集是物理墙钟,不能假快 |

### 4.3 · 最小关键路径（13 步）

| 顺序 | 阶段 | 谁 | 耗时 |
|---|---|---|---|
| 1 | 信号 + topic_ranker | engine 自动 + Cowork crypto-news-aggregator | 5-10 min |
| 2 | **★ Donald 选 + 写 selection_card** | Donald | 5-10 min（1 条）|
| 3 | Cowork 起 xthread_final | Cowork crypto-twitter-creator | 15 min |
| 4 | adapter_orchestrator 4 平台 + voice_check（含 T-04 Algorithm Rules） | engine D 姿势 | 1 min |
| 5 | critic + fact-check + brand-review | Cowork + 兼职女生 | 15-20 min |
| 6 | **★ Donald 终审**（数据关 + 可操作关）| Donald | 30-45 min |
| 7 | mpt_runner submit + MPT 渲染 | engine async | 1s submit + 5-40 min 异步 |
| 8 | Canva 配图 + yt_metadata + utm_generator | 兼职女生 + engine | 15-30 min（并行）|
| 9 | T-02 custom_slice_generator | engine D 姿势 | 1-2 min |
| 10 | schedule_planner --dry-run + 真排程 | engine | 2 min |
| 11 | Postiz 按 `scheduled_at` 真发 | Postiz | 几秒 |
| 12 | **★ Donald + 4 BD 5-人 Reply / LinkedIn 回评** | Donald + BD | 1h（30min wait + 30min 上场）|
| 13 | Donald 手发 KOL DM + log-dm | Donald | 20 min × 1-3 KOL |

### 4.4 · 一日时间线（具体小时数）

```
T+0:00    Donald 拍板 selection_card                  ← Stage 1 起点
T+0:15    Cowork 起完 xthread_final
T+0:16    跑 adapter + voice_check（T-04 含 Algorithm Rules）
T+0:35    Cowork critic + brand-review + 兼职女生 fact-check
T+1:15    ★ Donald 终审通过 → state=reviewed
T+1:16    并行起飞:
            ├ mpt_runner submit (1s) ──→ MPT 渲染中(5-40 min)
            ├ utm_generator (2s)
            ├ yt_metadata 写
            ├ Canva 配图 (兼职女生 15-30 min)
            └ T-02 custom_slice_generator
T+1:46    Canva 出图 + yt_metadata 完成
T+~2:00   MPT mp4 落盘(取决于视频长度)
T+2:01    schedule_planner --dry-run 检查
T+2:02    schedule_planner 真排程(scheduled_at=now+5min)
          OR /admin/run_publish 紧急通道（offset_minutes=10）
T+2:07    Postiz LinkedIn / Carousel / YT 发出
T+2:37    ★ T-05/T-07 Lark 提醒 fire
T+2:37    ★ Donald + 4 BD 5-人 Reply / LinkedIn 回评(30 min 内)
T+3:07    ★ Donald 手发 1-3 条 KOL Custom Slice DM(发完 log-dm)
T+3:30    一天闭环结束 · 接下来是 24h/7d 数据物理等待

T+24h     第一次完整 metrics 快照
T+7d      第二次完整 metrics 快照 + 当周 weekly_reporter
```

### 4.5 · 一日制保留的"效果最大化"元素（不可省）

| 保留 | 为什么不能省 |
|---|---|
| **T-04 X 主推无外链规则** | 第 1 推带 https 算法降权 30-50% · voice_checker 自动校验,0 成本保留 |
| **T-05 / T-07 30min Lark 提醒** | X / LinkedIn 算法 30min 窗口是物理事实,不省 |
| **Donald 终审 数据关 + 可操作关** | 数据关失败 = 这条死了,绝不允许 AI 改数字。压缩到 30-45min/条 但不省 |
| **critic plugin + fact-check + brand-review** | 3 道独立机检独立机制 · 都已自动,压缩到 15-20min 但不省 |
| **MPT 渲染质量** | 不抢快出片牺牲字幕 / TTS 音质 · 等就等 |
| **Postiz 平台错峰 ≥ 30-60min** | 6 平台同一秒发会被 X 算法识别"协调机构发布"降权 · 默认压缩从 24h+ 到 ≥ 30-60min,但 ≠ 0 错峰 |
| **Custom Slice + Donald 手发 KOL DM** | KOL 主动 Quote 是 90 天目标的核心,1 条 piece 漏掉就漏掉了 |

### 4.6 · 执行 SOP（Cowork bash 全程触发）

```bash
# Stage 1 · 选题 (10 min · 即时跑 · 不等周日 cron)
docker compose exec engine python -m jobs.kol_watch --week <CURRENT_W> --fallback-only
# (并行)Cowork crypto-news-aggregator skill 抓 7 天热点
docker compose exec engine python -m jobs.topic_ranker --week <CURRENT_W>
# Donald 在 Cowork: "选 X 题,写 selection_card.yaml"

# Stage 2-3 · 起草 + 机检 (30 min)
# Cowork crypto-twitter-creator skill 起 xthread_final.md
docker compose exec engine python -m jobs.adapter_orchestrator --piece-id <id>
# voice_check 已 inline · 看 voice_report_x_thread.md 末尾的 Algorithm Rules 段

# Stage 4 · 评审 (30 min · 压缩单条)
# Cowork: taskon-content-critic plugin → critic_report.md
# Cowork: marketing:brand-review skill
# Donald 终审数据关 + 可操作关
docker compose exec engine python -c "from lib.db import db; db.execute('UPDATE pieces SET state=\"reviewed\" WHERE id=?', ('<id>',))"

# Stage 5 · 资产并行起飞 (max 40 min · MPT 决定)
docker compose exec engine python -m jobs.mpt_runner --piece-id <id>            # 1s 返回
docker compose exec engine python -m jobs.utm_generator --piece-id <id> \
  --target-url https://taskon.xyz/benchmark-report \
  --platforms twitter,linkedin,medium,youtube \
  --accounts donald_en \
  --hook-type <hook>                                                            # 2s
docker compose exec engine python -m jobs.custom_slice_generator --piece-id <id>  # 30s
# 兼职女生 Canva 配图(并行 15-30 min)
# MPT 渲染中 5-40 min(后台 HMAC callback)

# Stage 6 · 发布(MPT mp4 出来后)
docker compose exec engine python -m jobs.schedule_planner --piece-id <id> --dry-run
# 默认 schedule_planner 锚到下周一 · 一日制要走 admin/run_publish 立即排程:
curl -X POST https://ingest.taskon.xyz/admin/run_publish \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"piece_id":"<id>","platforms":"yt_shorts,linkedin_post,linkedin_carousel","offset_minutes":10}'
# 立即排程(now + 10 min) · /admin/run_publish 已 ship(2026-05-16)

# Stage 7 · 30 min 后 Lark 提醒 + Donald 上场(★ 不可压)
# T-05/T-07 cron 自动 fire · Donald 看 Lark 群

# Stage 8 · KOL DM(发完每条都 log-dm)
docker compose exec engine python -m jobs.kol_relation_tracker log-dm \
  --kol @xxx --kind reply --tweet-url <url> --piece-id <id> --notes "..."
```

**关键技术钩子**：[`/admin/run_publish` endpoint](功能清单与外部访问.md#L450) 已经为这个场景而生（2026-05-16 ship · `offset_minutes=N` 直接覆盖 schedule_planner 的周一锚）。这是 Cowork Mobile 凌晨紧急发布的同一接口，也是一日制流程的标准入口。

---

## 5 · 主流程 B · 多日制（备选 · 深度内容专用）

> **定位**：当内容必须深耕、不能压缩时启用。**保留原 7-9 天周节奏**。
>
> **适用 ≤ 20% piece**：
> - 赛道深度长文（数据采集本身要 2-3h · 多来源交叉验证）
> - 客户联创（多方协调 / 联名 Co-Authored Take）
> - 季度 Benchmark Report 旗舰（如未来 Donald 重启 T-01 Pre-Read 8 DM）
> - KOL Pre-Read 季度 DM（季度节奏天然多日）
> - 需要 24h cooling-off 过滤情绪化选题的政治敏感题

### 5.1 · 7-day 完整生命周期

```
周日 20:00 │ ① engine cron · topic_ranker（自动）
          │   读 candidates_pool_<week>.json → 5 维评分 → 写
          │   runtime/selection_<week>.md (Top10)
          │
周日 21:00 │ ② Cowork 念 selection.md（★ Donald 30min 拍板挑 2-3 条）
          │
周日-周一  │ ③ 兼职女生周一全天起草（taskon-content-marketing /
          │    crypto-twitter-creator / youtube-crypto-growth）
          │   写出 runtime/drafts/<piece>/xthread_final.md
          │   ★ 草稿里 CTA 必须用 {{CTA_URL}} 占位符（B1 §4.5 SOP）
          │
周一/周二  │ ④ Cowork 起 taskon-content-critic plugin 评审
          │   50 分制打分 · ≥35 才放行 · 不过则改稿（第 2 轮）
          │
周二      │ ⑤ Donald 在 Cowork 里说"跑下 adapter"，Cowork 触发:
          │     docker compose exec engine python -m jobs.adapter_orchestrator \
          │        --piece-id <piece_id>
          │   产 4 平台稿 + voice_report*.md（含 T-04 Algorithm Rules 段）
          │
周一 11:00│ ⑥ engine cron · mpt_runner（自动 · A-design 异步）
          │   < 1s 提交 MPT，MPT 渲染 5-40min 后 HMAC 回调 engine
          │   产 shorts_60s.mp4 落到 drafts/<piece_id>/
          │
周二/周三  │ ⑦ Cowork 触发 utm_generator + custom_slice_generator
          │   → utm_links.json + custom_slice_*.md/canva.json
          │
★ 周三     │ ⑧ Donald 终审 3h（批 2-3 条 · 数据关 + 可操作关 + 跨 piece 比对）
09-12     │   通过 → state=reviewed
          │
周日 22:00 │ ⑨ engine cron · schedule_planner（自动）
          │   inject_cta() + sign_media_url() + Postiz Public API
          │   → 排到下周 Mon-Thu 错峰 24h+ 槽位
          │
下周 Mon-Thu │ ⑩ Postiz 自动按排程发布
          │   X Mon 09:00 ET → LinkedIn Tue 09:00 → Carousel Tue 10:00
          │   → Medium Wed 09:00 → YT Shorts Thu 09:00 → TikTok Thu 10:00
          │   ★ 每平台发后 30min · T-05/T-07 Lark 提醒 · Donald + BD 上场
          │
下周-下下周 │ ⑪ engine cron · metrics_collector (日 20:00) + attribution_engine
          │   + weekly_reporter (周日 18:00) + performance_analyzer
          │   下周一 topic_ranker 5 维权重调权（"赢的多发,输的少发"）
```

### 5.2 · 周节奏保留的不可替代价值

| 价值 | 一日制做不到 | 多日制做到 |
|---|---|---|
| **6 平台错峰 24h+** | 一日制 ≥ 30-60min 错峰是妥协 · 算法效果打折 | 完整 24h+ 错峰 · X→LinkedIn→Medium→YT 完整漏斗 |
| **批 2-3 条终审 cross-piece 比对** | 单条 piece 失去"哪条 hook 更强"的判断 | 周三 3h 处理 2-3 条 · Donald 比较中决策 |
| **24h cooling-off** | 选题热度过滤靠 Donald 当场直觉 | 周日选 → 周一起草 · 中间一夜过滤情绪化选题 |
| **兼职女生周一全天专注起草** | 1-2h 起草 1 条 vs 8h 起草 1 条 · 深度差距大 | 周一全天 1 条赛道深度 + Flourish 出图 |
| **5-人 Reply 队伍 angle 预分配** | 临场 push 5 人 · 不一定都有时间 | 周一兼职女生提前在 TG 群发 5 角度 · 周三时已知 |

---

## 6 · 两流程选择判断表

发选题时先答这两个问题，决定走哪条流程：

| 问题 | 答 → 走 Flow A 一日制 | 答 → 走 Flow B 多日制 |
|---|---|---|
| **数据来源**：是否需要 2+ 第三方数据集交叉验证？ | 1 个来源 / 沿用既有 ammo 弹药 | 多源交叉 / 新数据集首次引用 |
| **时效性**：明天还热吗？ | 24h 内必须出（行业突发事件 / 别家 KOL 在聊）| 不依赖时效（Playbook / 方法论 / 长尾 SEO）|
| **内容深度**：文字量 + 图表数？ | X Thread 5-7 条 + 1 图 / 数据洞察短贴 | Medium 长文 + 多图表 / 10 页 Carousel + 数据图 |
| **协调成本**：是否要等外部人？ | 0 等待（Donald 自闭环） | 等客户 / KOL Co-Author / BD 协调 |
| **品牌风险**：表达敏感度？ | low（沿用既有立场 / 行业共识） | high（首次表态新立场 / 政治敏感题）|

**经验法则**：每周如果出 3-5 条 piece，预期分布 ≈ 3-4 条 Flow A（一日制）+ 1 条 Flow B（多日制深耕）。Flow B 那条做"周旗舰"。

---

## 7 · 文件系统握手（接触面 ① 详图）

Cowork **永远不直接调 engine API**，靠文件契约：

```
runtime/drafts/<piece_id>/
├── selection_card.yaml          ← Cowork 写（选题卡 + critic_score + ammo + risk）
├── xthread_final.md             ← Cowork 写（X 主稿终稿，{{CTA_URL}} 占位）
├── linkedin_post.md             ← engine 写（adapter 改写）
├── medium_long.md               ← engine 写
├── carousel_10pages.md          ← engine 写
├── shorts_60s.md                ← engine 写
├── shorts_60s.mp4               ← MPT 异步回调写
├── yt_metadata.yaml             ← Cowork 写 (Tier 1) · 或 engine LLM 写 (Tier 2/3)
│                                  含 title_variants[3] + thumbnail_specs[3] (T-06)
├── voice_report_<platform>.md   ← engine 写（4 平台 · 含 T-04 Algorithm Rules 段）
├── utm_links.json               ← engine 写（utm_generator）
└── custom_slice_<handle>.md     ← engine 写（B3 §1.3 模型 4 · T-02 W22 ship）
   custom_slice_<handle>.canva.json  ← engine 写（兼职女生 Canva 改图参数）
```

任何一方都不 lock 文件 —— **Cowork 写在前，engine 写在后**。如果 engine 跑完 Cowork 再改 xthread_final.md，下次 schedule_planner 还是读最新的（最后写赢）。

---

## 8 · 4 种 Cowork "开" engine 的姿势

| 姿势 | 用法 | 典型场景 |
|---|---|---|
| **A · 文件系统** | Cowork 直接 read/write `runtime/drafts/<piece>/*.md` | 起草 xthread_final.md / 念 voice_report.md 给 Donald |
| **B · SQLite 直读** | Cowork artifact 嵌 sql.js · **read-only** | dashboard widget 念心跳 / 看 publishings 时间表 / 4 模型归因表 |
| **C · ingestion HTTP** | Cowork `curl http://127.0.0.1:5051/...` | `/health` 全栈状态 · `/admin/run_publish` 紧急排程 · `/admin/health/all` Bearer |
| **D · engine CLI** | Cowork bash → `docker compose exec engine python -m jobs.xxx` | 跑 voice_checker / schedule_planner / custom_slice_generator / kol_relation_tracker |

写永远走 engine（让 state.db 单一真相），读两边都行。

### 8.1 · 4 姿势对应实战

**姿势 A · 文件扔稿 + 触发 adapter（最常用）**
Cowork 起完稿存 xthread_final.md → Donald 说"跑下 adapter" → Cowork bash 工具触发 `docker compose exec engine python -m jobs.adapter_orchestrator --piece-id ...` → Cowork 读回 4 平台 + voice_report 给 Donald 看。

**姿势 B · Cowork artifact 直读 SQLite**
Cowork 起 dashboard.html artifact → 嵌 sql.js → `await fetch('state.db')` → 渲染 K1-K11 指标卡 + 5 维归因表。**Read-only**。

**姿势 C · Cowork 调 ingestion `/metrics` + `/admin/*`**
`GET http://127.0.0.1:5051/metrics` 拿 Prometheus 文本（"今天 lead 跑多少了？"实时小问）。`POST /admin/run_publish` 一日制紧急排程入口（Bearer 鉴权）。

**姿势 D · Donald 让 Cowork 手动跑某 job**
"跑下本周 weekly reporter" → Cowork 触发 → 读回 `runtime/weekly_report_2026W21.md` 给 Donald 念。

类似可手触发：`topic_ranker` / `performance_analyzer` / `kol_watch` / `update_btouch` / `cohort_analysis` / `ab_aggregator` / `channel_attribution` / **`custom_slice_generator`** / **`kol_relation_tracker log-dm`**（W22-W24 ship）。

---

## 9 · 4 个 PowerShell 一行命令（Donald 自助的"齿轮按钮"）

| 阶段 | 一行命令 | 干嘛 |
|---|---|---|
| 选题 | `.\scripts\run_select.ps1 2026W21` | 跑 kol_watch + topic_ranker，出 selection_2026W21.md |
| 生产 | `.\scripts\run_produce.ps1 2026W21-thread01` | 跑 adapter + voice_checker，出 4 平台稿（含 T-04 Algorithm Rules）|
| 发布 | `.\scripts\run_publish.ps1 2026W21-thread01` | 跑 utm_generator + schedule_planner，进 Postiz 排程 |
| 健康 | `.\scripts\run_health.ps1` | 看 docker ps + heartbeat + publish_failures（含 4 新 job 心跳）|

这 4 个脚本就是 Cowork 在 Claude Desktop 里调 bash 工具时的标准入口 —— 不用每次拼 `docker compose exec ...` 长串。

---

## 10 · 谁做什么（角色 × 动作矩阵）· "★" = 不可让渡

| 动作 | engine | Cowork | Donald ★ | 兼职女生 |
|---|---|---|---|---|
| 拉信号源 | ✅ kol_watch / metrics_collector | (crypto-news-aggregator skill) | — | — |
| 候选评分 | ✅ topic_ranker (5 维 LLM) | — | — | — |
| **选 2-3 条** | 给 Top10 | 念 + 排版 | ★ 拍板 | — |
| 起 X Thread 主稿 | ❌ 不起 | 主稿 (crypto-twitter-creator) | — | 主跑 |
| 4 平台 fan-out | ✅ adapter_orchestrator | — | — | 调 D 姿势 |
| 14 禁词 + X 主推无外链(T-04) | ✅ voice_checker | — | — | 改稿 |
| 10 维 50 分评分 | ❌ | ✅ taskon-content-critic | — | 调评审 |
| **数据关 + 可操作关** | ❌ | 念报告 | ★ 自己核 Dune/DefiLlama | — |
| UTM 短链 | ✅ utm_generator + shlink | — | — | 调 |
| YT 元数据 + 3 套 A/B(T-06) | LLM 派生 fallback | (cowork 优先写) | YT Studio 设 Test&Compare | 起草 |
| 6 平台排程 | ✅ schedule_planner（+role/account · T-08 plumbing）| — | — | 真排程 |
| MPT 渲染 | ✅ 1s submit + async callback | — | — | — |
| 真发 | ❌ engine 不发推 | ❌ 不发 | — | (Postiz 自动) |
| **★ 5-人 Reply 队伍** | T-05 Lark 提醒 | (无 skill) | ★ Donald + 4 BD 手发 | — |
| **★ LinkedIn 回评** | T-07 Lark 提醒 | (无 skill) | ★ Donald 手回 | — |
| **★ KOL DM 手发** | T-02 出草稿 | 念草稿 | ★ Donald 手发 X DM | (Canva 改图) |
| KOL 关系跟踪 | T-03 log-dm + scan | 调 D 姿势 | ★ 实发后 log-dm | — |
| 数据回流 + 归因 | ✅ metrics + attribution + cohort + Markov | dashboard 念 | 看 | — |
| **月度方向校准** | ✅ monthly_reporter | council-of-minds | ★ 1h 拍板 | — |
| KOL Pre-Read ⏸ | — | — | ★ ⏸（Benchmark 暂不出 · Donald 5/18 拍板）| — |
| Twitter Space | — | 起开场白 | ★ 真人开麦 | — |
| Messari 投稿 | 起初稿 | 排版 | ★ Email 沟通 | — |

---

## 11 · 4 条红线（engine 永远不做）

❌ **engine 不起 X 主稿** —— `crypto-twitter-creator` skill 在 Cowork 独占
❌ **engine 不打分评审** —— `taskon-content-critic` plugin 在 Cowork 独占
❌ **engine 不自动发推 / 自动 DM / 自动评 LinkedIn** —— B1 §6 红线
   - T-05 / T-07 只产 Lark P2 提醒,Donald 上场手发
   - T-02 只产 markdown 草稿,Donald 手发 KOL DM
   - T-03 scan 只读 X API,不写 X
❌ **engine 不改业务数字** —— Fact-Check 失败必删段,LLM 不许编

补充（W22-W24 ship 后强化）：
❌ **X Quote chain 永不启用** —— `config.yaml :: postiz.routing.x_thread / x_short` 标 ★ DO NOT POPULATE（B3 §2 杠杆 1 · 27-人协调 Quote 算法降权）

---

## 12 · 接外部 4 系统（engine 当中转）

```
Cowork ──→ engine ──→ MPT       (localhost:8090 · 视频)
                 ──→ Postiz    (localhost:4007 · 发布)
                 ──→ Listmonk  (未部署 · Newsletter)
                 ──→ shlink    (未部署 · 短链 · fallback 长链)
```

Cowork 调 `mpt-video` skill 是**直连** MPT API 起一次性视频（实验场景）；engine cron 调 MPT 是**走异步管线**（生产 piece 自动出片）。两条路径并行。

---

## 13 · 一句话总结

**Cowork 和 engine 没有 RPC，只靠 `runtime/drafts/<piece_id>/` 这个共享文件夹握手** —— Cowork 写选题卡 + X 主稿，engine 改 4 平台 + 跑数据 + 排程，Postiz 真发，metrics_collector 回流数据进 SQLite，weekly_reporter 反哺下周选题权重。

**默认走 Flow A 一日制**（3.5-5h 一条 piece · 效果最大化的硬约束全保留 · 适用 ≥ 80% piece）；**只在内容必须深耕时切 Flow B 多日制**（7-9 天 · 适用 ≤ 20% piece · 通常是周旗舰）。Donald 视 piece 复杂度逐条选流程，不绑死周节奏。

---

## 14 · 想再深入看哪段

- **B1 全链路细节** → `D:\Taskon\marketing\00_内容营销引擎\全流程规划_v3\B1_内容生产全流程.md`
- **B3 算法借力 + KOL 触达落地路线** → [B3_engine_落地路线_v1.md](B3_engine_落地路线_v1.md)
- **17 行决策矩阵** → `D:\Taskon\marketing\00_内容营销引擎\全流程规划_v3\C_工具栈职责边界.md`
- **本仓代码地图** → [architecture.md](architecture.md)（17 表 · 16 cron · A-design async · §12 B3 算法借力闭环）
- **Cowork prompt 模板（直接复制粘贴用）** → [cowork_integration.md](cowork_integration.md) §4
- **Donald 每日操作** → [Donald_每日操作手册_v1.md](Donald_每日操作手册_v1.md)
- **engine 能力 4 姿势查表** → [功能清单与外部访问.md](功能清单与外部访问.md)
- **首次跑通 B1** → [B1流程跑通_首次执行指南.md](B1流程跑通_首次执行指南.md)

---

## 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-21 | 首版。10 步生命周期 + 3 接触面 + 4 姿势 + 4 红线 |
| **v1.1** | **2026-05-21** | **重大重构**：常态默认从"周节奏"改为"效果最大化前提下最短可行时间"。拆 § 4 主流程为 **Flow A 一日制（默认 · 3.5-5h）** + **Flow B 多日制（备选 · 深耕内容）**。新增 § 4.1 依赖 DAG / § 4.2 硬墙钟下限 / § 4.3 最小关键路径 / § 4.4 一日时间线 / § 4.5 不可省的效果元素清单 / § 4.6 一日 SOP bash 脚本 / § 5.2 周节奏不可替代价值 / § 6 两流程判断表 / § 10 角色×动作矩阵。整合本会话 W22-W24 ship 后的 T-02..T-08（voice_checker Algorithm Rules / T-05/T-07 Lark 提醒 / Custom Slice / KOL 关系状态机 / YT A/B variants / 矩阵号路由）。`/admin/run_publish` 紧急通道成为一日制标准入口。一句话总结改写,反映新默认。 |
