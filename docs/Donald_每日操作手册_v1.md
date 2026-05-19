# Donald 每日操作手册 v1.1（2026-05-19 起生效）

> **本手册定位**：把内容选题 → 生产 → 评审 → 发布 → 数据分析 → 迭代优化的全流程做成「Cowork 一个对话框就能完成」的可重复参考。
>
> **核心原则**：你只动 Cowork · engine 是 Docker 里跑的后台发动机 · 永远不直连 engine UI（它没有 UI）。
>
> **v1.1 (5/19) 新增**：W22-W24 ship · 7 个 B3 任务 (T-02..T-08)
> - **T-04** voice_checker 报告新增 `Algorithm Rules` 段(自动校验 X 主推无外链 · §2.5)
> - **T-05/T-07** Lark 30min 自动提醒(发后立即上场 5-人 Reply / 回 LinkedIn 评论 · §2.8)
> - **T-02** Custom Slice 一键出 3 条 KOL DM 草稿(adapter 之后 · §2.3.1)
> - **T-03** KOL 触达 log-dm CLI + 关系状态机(手发后记一笔 · §2.9)
> - **T-06** YT 上传新增 3 套 title + thumbnail variants(YT Studio Test & Compare · §2.6)
> - **T-08** 矩阵号 cross-post plumbing 就绪(待 Donald 连 2nd 账号)
>
> **5/16 起生效前提**：
> - engine HEAD `b39b450` · 249/249 pytest 全绿(W22-W24 后仍 249/249)
> - A-design async + signed media + CTA `{{CTA_URL}}` 占位符 三大改造 ship
> - piece 02 首次真上 YouTube [`es7XQWghoSM`](https://www.youtube.com/watch?v=es7XQWghoSM) 验证完整 10 环链路

---

## 0 · 一句话原则

| 谁 | 做什么 |
|---|---|
| **你** | 拍板（周日选题 / 周三终审 / 月末校准）+ 念读（Cowork 念给你听）+ 改稿（手发 X / KOL Reply） |
| **engine** | 候选池 / 评分 / fan-out / 排程 / 渲染 / 发布 / 采集 / 归因 / 周月报（12 cron 自动跑） |
| **Cowork** | 你的唯一驾驶舱 · 用 4 种姿势调 engine（A 读文件 / B 读 SQLite / C HTTP / D 跑 bash） |
| **兼职女生** | 周一起草 + 配图 / 周二跑 adapter + 评审关 1-3 / 周三下午 UTM + 排程 |
| **state.db + drafts/<piece>/** | 两边的握手数据库 + 工件目录 |

---

## 1 · 周节奏全景图

下面这张表把整周的 engine 自动 cron + 你的人工动作放在一起。**你只管"★"标的 4 个时刻**，其他全自动。

| 时间 | engine 自动 | 你的动作 ★ | 兼职女生 | 你耗时 |
|---|---|---|---|---|
| **周日 09:00** | — | Cowork："跑下本周 crypto-news 7 天热点" | — | 5min |
| 周日 10:00 | `kol_watch` 抓 30 KOL | — | — | 0 |
| 周日 18:00 | `weekly_reporter` 写周报 | — | — | 0 |
| 周日 18:30 | `performance_analyzer` Top1/Bottom1 | — | — | 0 |
| **周日 19:00-19:30** | — | Cowork："念本周周报 + Top1/Bottom1" | — | 30min |
| 周日 20:00 | `topic_ranker` 5 维 LLM Top10 | — | — | 0 |
| **★ 周日 21:00-21:30** | — | Cowork："念本周 Top10" → 拍板 2-3 条 → 建 selection_card.yaml | — | 30min |
| 周日 22:00 | `schedule_planner` dry-run | — | — | 0 |
| **周一全天** | — | — | 起草 X Thread + Canva 配图（用 `{{CTA_URL}}` 占位符）| 8h |
| 周一 09:00 | `update_btouch`（阻塞 admin API） | — | — | 0 |
| **★ 周一-四 09:00-09:30** | 08:30 `kol_daily_replier` 备 5-8 候选 | Cowork："念今天 KOL Reply 候选" → 挑 1-3 条手发 | — | 30min/天 |
| 周一 11:00 | `mpt_runner` submit-and-exit（短视频）| — | — | 0 |
| **周二 / 周四** | — | — | 跑 adapter + 评审关 1-3 + 配图 + 排程 | 8h |
| **★ 周三 09:00-12:00** | — | Cowork：终审 3h（数据关 + 可操作关 + YT 元数据关） | — | 3h |
| 每日 20:00 | `metrics_collector` 5 源回流 | — | — | 0 |
| 每日 21:00 | `attribution_engine` 4 模型归因 | — | — | 0 |
| 每日 23:00 | `backup_sqlite`（保 14 天） | — | — | 0 |
| 每 5 min | `mpt_reconciler` + `container_heartbeat` | — | — | 0 |
| **★ 月末周日 1h** | 月末 19:00 `monthly_reporter` | Cowork：月度方向校准 | — | 1h |
| 月末 19:30+ | `cohort_analysis` + `ab_aggregator` + `channel_attribution`（Markov） | — | — | 0 |

**你周时间**：6h/周（KOL Reply 2h + 终审 3h + 选题 30min + 周报 30min）+ 月度峰值 +3h。

---

## 2 · 7 个环节 · Cowork 对话模板（复制粘贴即可）

### 2.1 · 周日选题（30min · 你 + Cowork）

**Step 1 · 拉本周热点 + 念周报 + 念 Top10**

```
跑下本周 crypto-news-aggregator 7 天热点，主题筛 DeFi growth / Quest /
Anti-Sybil / Perps DEX / White Label，输出 ≤30 条，写到
D:\Taskon\marketing\engine\runtime\hot_topics_<本周WXX>.json。

然后念本周选题 Top10 + 周报 + Top1/Bottom1 复盘给我，分别从：
- runtime/selection_<本周WXX>.md
- runtime/weekly_report_<上周WXX>.md
- runtime/performance_analysis_<上周WXX>.md
```

Cowork 会用 4 种姿势：
- A · 读 `runtime/selection_*.md`（engine `topic_ranker` 周日 20:00 已写好）
- A · 读 `runtime/weekly_report_*.md`（engine `weekly_reporter` 周日 18:00 已写好 · LLM 全挂时读 `_bare` 兜底版）
- A · 读 `runtime/performance_analysis_*.md`
- 用对话视角组织 Top10 念给你

**Step 2 · 拍板 2-3 条**

```
我选「47% Quest 预算被 Bot 吃」。建一个 piece 占位，piece_id=<本周>-thread01，
落 selection_card.yaml 到 runtime/drafts/<本周>-thread01/，字段：

id: <本周>-thread01
title_hypothesis: 47% Quest 预算被 Bot 吃 — Q1 全平台数据交叉
hook_type: 47pct_bot
narrative_anchor: trust_collapse
target_persona: crypto_cmo
risk_level: low
data_sources:
  - https://dune.com/...
  - https://defillama.com/...
expected_format: X Thread / 5 推 / ≤1400字
created_at: <today ISO>
```

**Step 3 · 重复 2-3 次**（每条 piece 一个 selection_card.yaml）

### 2.2 · 起草 X Thread（周一 30-90min · 兼职女生主跑 / 你可代跑）

如果兼职女生休假或你想自己起：

```
用 crypto-twitter-creator skill 起草 X Thread 终稿。
选题卡见 runtime/drafts/<piece>/selection_card.yaml

【硬要求】
- 5-7 条推文
- 第一条：数字钩子 + 反共识结论（前 30 字含数字）
- 倒数第二条：埋钩子说"完整数据在评论区"
- 最末条：CTA + 用 {{CTA_URL}} 占位符（★ 不写真实 URL！）
- 数据 100% 真实，每个数字标 [SOURCE: xxx]
- 14 禁词清单：全方位/革命性/颠覆/赋能/闭环/抓手/价值赋能/显著/dive into/let's explore/综上所述/无缝/全栈/一站式
- 涉及 TaskOn 业务主动披露利益

落到 runtime/drafts/<piece>/xthread_final.md
```

**红线**：必须用 `{{CTA_URL}}` 占位符（不写真实 URL），否则归因断链（风险 R17）。详见 [`marketing/CLAUDE.md §4.5`](../../CLAUDE.md)。

### 2.3 · 4 平台 fan-out + voice check（5min · 全自动）

```
跑 engine adapter_orchestrator 把 piece <piece-id> 的 X Thread 改成
LinkedIn / Medium / Carousel / Shorts 四版，并自动串调 voice_checker。

bash:
docker compose exec engine python -m jobs.adapter_orchestrator \
  --piece-id <piece-id>

然后念 voice_report.md 给我看有没有禁词或长度超限。
```

如果 `state=needs_revision`：

```
voice_report 显示第 3 推有"显著"，piece <piece-id>。
兼职女生改完后帮我重跑 voice_checker。

bash:
docker compose exec engine python -m jobs.voice_checker --piece-id <piece-id>
```

★ **W22 新增** · voice_report.md 末尾现在多一段 `## Algorithm Rules: PASS | FAIL`(T-04):

```
## Algorithm Rules: PASS (0 violations)
_none_
```

如果 FAIL → X Thread 推 1 含 `https://` 外链(B3 §2 杠杆 2 算法降权 30-50%)。修复:把 URL 移到 thread 第 2 条自我 Reply。

### 2.3.1 · Custom Slice KOL DM 草稿（5min · 周三下午 / Adapter 之后）★ T-02 W23 新增

```
跑完 adapter 后,顺手为 piece <piece-id> 出 3 条 KOL Custom Slice DM 草稿:

bash:
docker compose exec engine python -m jobs.custom_slice_generator --piece-id <piece-id>

然后念 drafts/<piece-id>/custom_slice_*.md 给我,我看完后挑 1-3 条
周四发布后 1 小时内手发 DM。Canva 改图参数在 *.canva.json 兼职女生用。
```

engine 自动:
1. 读 `selection_card.yaml` 提取 narrative_anchor / hook_type / key_data_points
2. 跟 `config/kol_watchlist.yaml` 30 KOL 的 focus + angle 做 token-overlap 匹配
3. 同分按 tier A→B→C 排序 · 取 top-3
4. MiniMaxi LLM 出每位 KOL 个性化 DM(≤280 字)+ Canva 图表参数 JSON
5. URL 漏出会被 server 端 regex scrub(防红线)

**红线**:engine 永不自动 DM · 你手发(B1 §6 + B3 §1.3 模型 4 纪律)。

### 2.4 · 短视频渲染（周一 11:00 自动 · 或手动）

**自动模式**：周一 11:00 cron 自动对所有 `state=reviewed` 的 piece 提交 MPT 渲染。

**手动模式**：

```
帮我提交 piece <piece-id> 的短视频渲染。

bash:
docker compose exec engine python -m jobs.mpt_runner \
  --piece-id <piece-id> --voice zh-CN-YunxiNeural-Male
```

**< 1 秒返回**（async submit-and-exit）。5-40 min 后 MPT 渲染完，HMAC webhook 自动回 engine，engine daemon 异步下载 mp4 → `runtime/drafts/<piece>/shorts_60s.mp4`。

**监控**：

```
看一眼 piece <piece-id> 的视频渲染状态。

bash:
docker compose exec engine python -c "from lib.db import db; row=db.mpt_tasks.get_in_flight_for_piece('<piece-id>'); print(dict(row) if row else 'done — check completed_at')"
```

**超 6h 还卡在 submitted**：reconciler 自动 mark stale + P1 告警。手动重提：

```
piece <piece-id> 渲染卡死了，帮我重新提交。

bash:
docker compose exec engine python -m jobs.mpt_runner \
  --piece-id <piece-id> --force
```

### 2.5 · 评审 4 关（周二夜兼职女生跑前 3 关 · ★ 你周三 09-12 跑关 4）

**关 1**（voice_checker · 已在 2.3 跑过）
**关 2**（critic plugin · 兼职女生跑）：

```
跑 taskon-content-critic plugin 对 piece <piece-id> 的 X Thread 评分。
读 runtime/drafts/<piece>/xthread_final.md，输出 10 维 50 分 + 3 处改写建议 +
标题候选 3 条 + CTA 重写 + 一句话死穴。
落到 runtime/drafts/<piece>/critic_report.md
```

阈值：≥45 进关 3 / 40-44 改 1-2 项重跑 / <40 通知你砍。

**关 3**（fact-check · 兼职女生）：

```
扫 piece <piece-id> 的所有 [DATA-NEEDED]，每个补 ≥2 个来源（DefiLlama / Dune /
Chainalysis / 项目方公开数据）。然后跑 marketing:brand-review skill 扫品牌口径。

bash:
findstr /S "DATA-NEEDED" D:\Taskon\marketing\engine\runtime\drafts\<piece>\*.md
```

**★ 关 4**（你周三 09-12 3h · 不可让渡）：

```
帮我把 piece <piece-id> 的所有评审产物念给我（处理 2-3 条 piece）：

- selection_card.yaml（看立场）
- xthread_final.md（终稿）
- critic_report.md（10 维评分）
- voice_report.md（14 禁词）
- fact_check_report.md（数据来源）
- yt_metadata.yaml（如有 yt_shorts，看 description 含 {{CTA_URL}}）

我边读边核数据。
```

**通过**：

```
piece <piece-id> 终审通过，标 state=reviewed。

bash:
docker compose exec engine python -c "from lib.db import db; db.execute('UPDATE pieces SET state=\"reviewed\" WHERE id=?', ('<piece-id>',)); db.commit()"
```

**数据关失败**（这条死了）：

```
piece <piece-id> 数据关失败（第 3 推 47% 数字与 DefiLlama 不一致），
标 state=killed 不发。

bash:
docker compose exec engine python -c "from lib.db import db; db.execute('UPDATE pieces SET state=\"killed\" WHERE id=?', ('<piece-id>',)); db.commit()"
```

**红线**：数据关失败 = 砍。**绝不允许让 AI 改数字**。

### 2.6 · UTM + 排程 dry-run（周三 15:00 · 1 行命令）

```
为 piece <piece-id> 生成 UTM 短链 + dry-run 看 6 平台排程时间表。

bash:
docker compose exec engine python -m jobs.utm_generator \
  --piece-id <piece-id> \
  --target-url https://taskon.xyz/benchmark-report \
  --platforms twitter,linkedin,medium,youtube \
  --accounts donald_en,taskon_official \
  --hook-type <hook_type from selection_card>

docker compose exec engine python -m jobs.schedule_planner \
  --piece-id <piece-id> --dry-run

然后念 utm_links.json + dry-run 输出给我，检查 6 平台时间表合不合错峰。
```

期望输出：

```
DRY-RUN · linkedin_post     scheduled_at=下周二 09:00 ET ...
DRY-RUN · linkedin_carousel scheduled_at=下周二 10:00 ET ...
DRY-RUN · yt_shorts         scheduled_at=下周四 09:00 ET ... yt_meta.source=cowork
WARNING · skip medium / tiktok / x_thread / newsletter（未配 / ⏸）
schedule_planner done: planned=6 scheduled=3 skipped=3 failures=0 status=warning
```

3 平台 scheduled、3 skip = 健康（Newsletter ⏸ / Medium / TikTok / X 还没 OAuth 接 Postiz）。

### 2.7 · 真排程（周三 15:30 · 慎按）

```
真排程 piece <piece-id> 到 Postiz。

bash:
docker compose exec engine python -m jobs.schedule_planner \
  --piece-id <piece-id>
```

engine 自动：
1. `inject_cta()` 把所有 `{{CTA_URL}}` 占位符替换成 `utm_links.json` 里 `(platform, account)` 对应长链
2. `sign_media_url()` 给 mp4 签 HMAC URL（TTL 1h）
3. 调 Postiz `create_post()` 建 scheduled post

然后 Postiz 自动按时间发到 LinkedIn / YT / ...

**验证**：

```
piece <piece-id> 已真排程，给我看 Postiz 那边的 scheduled posts 全景。

bash:
docker compose exec engine python -c "from lib.db import db; [print(dict(r)) for r in db.fetchall('SELECT platform,postiz_post_id,scheduled_at,utm_campaign FROM publishings WHERE piece_id=\"<piece-id>\" ORDER BY scheduled_at')]"
```

**真发后跟进**：周一/二/四 09:00 ET Postiz 自动发，**你 09:00-09:30 ★ 跟 5 人深度 Reply**。

### 2.8 · 30min 算法借力 Lark 自动提醒 ★ T-05 / T-07 W22 新增

发布后 30 分钟,engine 自动 push 2 条 Lark P2 提醒(无需你动手):

| 触发 | 提醒内容 | 你的动作 |
|---|---|---|
| X Thread 发后 30min | "X 主推发了 30min · 5-人 Reply 队伍 请上场 (B3 §2 杠杆 1)" | 看一眼推下回复数,< 5 → 自己 + BD 上 1 条带新观点的 Reply(≥30 字,不允许 +1) |
| LinkedIn Post / Carousel 发后 30min | "LinkedIn 发了 30min · 请回 ≥5 条评论 (B3 §4 杠杆 1)" | 打开 LinkedIn 给评论区前 5 条回复 1-2 句 |

机制:`publishings.scheduled_at` (周日 22:00 schedule_planner 写入 UTC) → cron `*/10 * * * *` 扫 `now-31min ~ now-29min` 窗口 → Lark P2 + stamp `<col>_alert_sent`(idempotent · 永不重 fire)。

⚠️ **MVP 限制**:T-05 当前**总是 nudge**,不查 reply 数 — 因为 X tweet_id 在 30min 还未回填。未来要做"reply<5 才 nudge"需要 Postiz tweet_id 解析路径。

**红线**:engine 永远不替你发 Reply / 回 LinkedIn 评论(B1 §6 + B3 §2/§4 纪律)。

### 2.9 · KOL 触达记录(每发完 1 条 KOL Reply / DM 写 1 笔)★ T-03 W24 新增

```
我刚发了 KOL Reply 给 @hildobby,帮我记一下:

bash:
docker compose exec engine python -m jobs.kol_relation_tracker log-dm \
  --kol @hildobby --kind reply \
  --tweet-url https://x.com/DonaldTang/status/<tweet-id> \
  --piece-id <piece-id> \
  --notes "..."
```

`--kind` 可选:`reply` / `dm` / `quote` / `custom_slice`(对应 §2.3.1 出的草稿)。

engine 自动:
1. INSERT `kol_dm_log` 行(`donald_tweet_id` 从 URL parse)
2. UPSERT `kol_watchlist` 的 `last_dm_date`

**后续(可选)**:连续 1 周用 log-dm 记录后,uncomment `docker/crontab` 的 `1 9 * * * kol_relation_tracker scan` 行 + 重启 engine → 每日 09:01 自动扫 X API 看 KOL 是否回应 + 3 quote/90天 自动升 tier B→A + Lark P2 提醒。

**红线**:engine 永远不替你 DM · 这个 CLI 只是记账(B1 §6 + B3 §1.4 纪律)。

---

## 3 · 数据分析 + 迭代闭环（自动跑 · 你只看不动）

### 3.1 · 物理回路（每周自动跑 · 你 0 动作）

```
每日 20:00 → metrics_collector 5 源拉数据 → metrics_daily 表
每日 21:00 → attribution_engine 4 模型归因（last/first/linear/Markov）→ user_journey + leads
每周日 18:00 → weekly_reporter 写周报 → weekly_aggregates 表（5 维 × N 值）
每周日 20:00 → topic_ranker 读上周 weekly_aggregates 调权 → 下周 Top10
```

**本周表现好的钩子下周自动 +20% 权重，差的 -20%，单类钩子 50% 上限**（强制叙事多样性）。

### 3.2 · 周末看仪表盘（30s）

```
打开 TaskOn 内容引擎 dashboard。
```

Cowork 用 `mcp__cowork__create_artifact` 拉一个 HTML widget，sql.js 直读 `state.db`：
- 顶部：当月 SQL 计数 + 月度目标进度条 + 距月底天数
- 6 张卡片：K1 / K2 / K5 / K9 / K10（⏸）/ K11
- Row 3：本周 TOP3 内容 + KOL Quote
- Row 4：漏斗可视化（曝光 → 互动 → 点击 → 访问 → 留资 → MQL → SQL）
- 底部：心跳监控 + 最近 7 天 P0/P1 异常告警

**artifact 一旦建好就持久化**，下次"打开 dashboard"直接弹出最新数据（30s 自动刷新）。

### 3.3 · 健康全景（5s 一句话查）

```
看一眼 engine 全栈健康。

bash:
curl -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  http://127.0.0.1:5051/admin/health/all
```

返回：engine + postiz + shlink + mpt 各组件状态 + 最近 12 cron 心跳 + `publish_failures` 是否有 P0。

### 3.4 · BD K4 当面归因覆盖（周一 BD 30min）

BD 当面问到某个 SQL 真实归因后：

```
BD Alex 当面确认 lead_id=42 来自 piece 2026W20-thread01，
帮我覆盖归因。

bash:
docker compose exec engine python -m jobs.attribution_engine \
  --bd-override lead_id=42,content_id=2026W20-thread01,by=alex
```

**这是 K4 的唯一真相源**，CRM 自动归因和 BD 对不上时以 BD 为准。

### 3.5 · 月度方向校准（★ 月末周日 1h）

```
念本月 monthly_report + cohort 留存 + Markov 归因 + Top1/Bottom1 月度归因给我：

bash:
type D:\Taskon\marketing\engine\runtime\monthly_report_<YYYY-MM>.md
type D:\Taskon\marketing\engine\runtime\cohort_<YYYY-MM>.md
type D:\Taskon\marketing\engine\runtime\markov_<YYYY-MM>.md

然后我们用 council-of-minds skill 跑：
"基于本月数据 + Q2 锚点 + Crypto_Industry_Reality 弹药库，反向输出
5 个候选新叙事锚点 + 3 个候选 Hook 类型迭代"

输出落到 runtime/anchor_change_proposal_<YYYY-MM>.md
```

你拍板后下月生效（改 `config/voice_disabled_words.yaml` 或 hook_library）。

---

## 4 · 紧急路径（你在路上 / 桌面关机）

**Cowork Mobile（iOS/Android Claude App）+ engine admin endpoint Bearer Token** 远程触发。

### 4.1 · 在路上紧急发一条

```
现在立即发 piece 2026W21-thread01 到 YouTube + LinkedIn，10 分钟后。

bash:
curl -X POST https://ingest.taskon.xyz/admin/run_publish \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"piece_id":"2026W21-thread01","platforms":"yt_shorts,linkedin_post","offset_minutes":10}'

立即返 202 + task_id；然后帮我轮询 /admin/tasks/<task_id> 拿状态。
```

### 4.2 · 在路上看引擎健康

```
看一眼 engine 健康。

bash:
curl -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  https://ingest.taskon.xyz/admin/health/all
```

### 4.3 · 远程触发数据采集（凌晨发现昨天数据没回流）

```
重跑昨天的数据采集 + 归因。

bash:
curl -X POST https://ingest.taskon.xyz/admin/run_metrics \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"date":"2026-05-15"}'
```

---

## 5 · 红线 · 不可让渡的 7 个时刻

| # | 时刻 | 频率 | 时长 | 为什么不让 |
|---|---|---|---|---|
| 1 | 周日 21:00 选题拍板 | 周 | 30min | 品牌方向决策 |
| 2 | 周一-四 09:00 KOL Reply | 每天 | 30min | engine 永不自动发推 |
| 3 | 周三 09-12 终审 | 周 | 3h | 数据关 + 可操作关 |
| 4 | KOL Pre-Read 8 人 DM ⏸ | 季度 | 40min | 关系资产唯一接口 · ⏸ Donald 2026-05-18 拍板 Benchmark Report 暂不出 → T-01 Pre-Read 8 待 Benchmark 决策重启 |
| 5 | Twitter Space 真人开麦 | 双周 | 1.5h | 反算法识别 / 关系层 |
| 6 | Messari/CoinDesk 投稿 | 月 | 2-3h | 行业话语权 |
| 7 | 月末方向校准 | 月 | 1h | 季度战略决策 |

**红线规则**：
- engine 帮你备料、Cowork 帮你念读 / 排版 / 起草，但最后那一下必须你按
- 数据关失败 → 这条死了不发（绝不允许 AI 改数字）
- 所有平台 markdown + yt_metadata description **必须含 `{{CTA_URL}}` 占位符**（红线 R17）

---

## 6 · 常见出错 cheatsheet

| 现象 | 一句话查 |
|---|---|
| 哪个 cron 挂了 | Cowork: "看 engine heartbeat 最近 20 行" |
| 哪里 P 告警 | Cowork: "看 publish_failures 最近 10 行" |
| Postiz 通不通 | bash: `docker compose exec engine python -c "from sources.postiz import postiz; print(len(postiz.list_integrations()))"` |
| LLM 通不通 | bash: `docker compose exec engine python -c "from lib.llm_client import llm; print(llm.complete('test','say hi'))"` |
| 容器健康 | bash: `docker compose ps` |
| 容器日志 | bash: `docker compose logs -f engine` |
| 删一条没用的 piece | 删 `runtime/drafts/<piece>/` 目录 + state.db pieces 表那行 |
| MPT 渲染卡死 | bash: `docker compose exec engine python -m jobs.mpt_runner --piece-id <id> --force` |
| Postiz OAuth 过期 | Donald 去 Postiz UI Integrations 页 reconnect 即可，UUID 不变 |
| cloudflared 中断 | 检查 `cloudflared` 容器 + `ingest.taskon.xyz` DNS · 切 `publish_immediate.py` 紧急路径 |

---

## 7 · 你不需要记住的（engine 自动跑的）

下面这些 engine 永远在后台跑，你 0 动作：

```
✅ 每 5 min · mpt_reconciler 自愈丢失 callback
✅ 每 5 min · container_heartbeat 容器存活心跳
✅ 每 10 min · reply_density_alert (T-05 · X 发后 30min Lark 提醒 5-人 Reply 队伍)
✅ 每 10 min · linkedin_engagement_alert (T-07 · LinkedIn 发后 30min Lark 提醒回评)
⏸ 每日 08:30 · kol_daily_replier 备 5-8 KOL Reply 候选 (旁支 · cron 暂注释)
⏸ 每日 09:01 · kol_relation_tracker scan (T-03 · opt-in · 待你跑一周 log-dm 后启用)
✅ 每日 20:00 · metrics_collector 5 源拉数据
✅ 每日 21:00 · attribution_engine 4 模型归因
✅ 每日 23:00 · backup_sqlite 备份（保 14 天）
⏸ 周日 10:00 · kol_watch 抓 30 KOL (旁支 · cron 暂注释)
✅ 周日 18:00 · weekly_reporter 写周报（LLM 全挂 → _bare 兜底）
✅ 周日 18:30 · performance_analyzer Top1/Bottom1
✅ 周日 20:00 · topic_ranker 5 维 LLM Top10
✅ 周日 22:00 · schedule_planner 6 平台错峰排程（含 CTA 替换 + 媒体签名 + scheduled_at + 矩阵号路由）
⏸ 周一 09:00 · update_btouch (旁支 · cron 暂注释)
✅ 周一 11:00 · mpt_runner submit-and-exit
✅ 月末 19:00 · monthly_reporter
✅ 月末 19:30+ · cohort_analysis + ab_aggregator + channel_attribution (Markov)
```

⏸ = 暂注释,需要 Donald 主动 uncomment + `docker compose restart engine` 才启用。

---

## 8 · 第一次用本手册（W1 上手期 · 周日开始）

```
Day 0（周日 21:00）：选题 30min（环节 2.1）
Day 1（周一全天）：兼职女生起草 + 配图（环节 2.2 ★ 用 {{CTA_URL}} 占位符）
Day 2（周二全天）：兼职女生跑 adapter + 评审关 1-3（环节 2.3-2.5）
Day 3（周三 09-12）：你终审 3h（环节 2.5 关 4）
Day 3（周三 15:00）：兼职女生 UTM + dry-run（环节 2.6）
Day 3（周三 15:30）：你或兼职女生真排程（环节 2.7）
Day 4-7：Postiz 自动按时发 + 你 09:00-09:30 KOL Reply（环节 2 · 表中 ★）

每周日 19:00：你看周报 30min
每月末周日：你 1h 方向校准
```

跑通第一周后，整套就是「拍板 + 念读 + 改稿」三件事，剩下都自动。

---

## 9 · 进阶 · 当 Newsletter 启用后（⏸ 当前暂搁置）

> Donald 5/13 决策：W1 首跑流程不带 Newsletter；等 Listmonk + AWS SES 部署完成后启用（4-6h 工时另开会话）。

启用后增加：
- 月度 25 日 09:00 · engine `newsletter_assembler` dry-run
- 月末周三 09:00-10:00 · 兼职女生最终预览 + 你 30min 终审
- 月末周三 10:00 ET · Listmonk 触发发送（SES SMTP）
- 实时 · engine `/api/listmonk-webhook` + `/api/ses-bounce` 接收事件

你时间 +30min/月（月度 Newsletter 终审）。

---

## 10 · 配套阅读（深度参考）

| 文档 | 用途 |
|---|---|
| [`Cowork启动手册.md`](Cowork启动手册.md) | Cowork 端的对话模板（本手册前身 · 更技术） |
| [`B1流程跑通_首次执行指南.md`](B1流程跑通_首次执行指南.md) | 从 0 跑一条 piece 完整命令（兼职女生主用） |
| [`功能清单与外部访问.md`](功能清单与外部访问.md) | 每个 cron / endpoint / source 的 Cowork 调用 4 种姿势 |
| [`architecture.md`](architecture.md) | engine 完整架构 · 16 表 · 12 cron · A-design 时序图 |
| [`marketing/CLAUDE.md §4.5`](../../CLAUDE.md) | `{{CTA_URL}}` 占位符创作 SOP（红线） |
| [`全流程规划_v3/`](../../00_内容营销引擎/全流程规划_v3/) | 13 个模块的策略层规划文档（v3.5 现状版） |

---

## 11 · 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| **v1.0** | **2026-05-16** | **首版。Donald 要求把"全程 Cowork 完成 + engine 调用"梳理成可重复参考的每日操作手册。基于 5/16 三大改造（async + signed media + CTA placeholder）+ 7 个不可让渡时刻 + 12 cron 时间表整合** |
| **v1.1** | **2026-05-19** | **W22-W24 ship · 7 个 B3 任务 (T-02..T-08)**。新增 §2.3.1 Custom Slice DM 草稿(adapter 后一键出 3 KOL)· §2.5 voice_report 多出 `Algorithm Rules` 段(T-04 X 主推无外链)· §2.8 30min Lark 提醒(T-05/T-07 算法借力)· §2.9 KOL 触达 log-dm CLI(T-03 关系状态机)· §7 cron 表加 4 行 + ⏸ 标记 opt-in · §5 红线表 Pre-Read 8 标 ⏸(Donald 5/18 拍板 Benchmark 暂不出)· §6 真排程 verify 加 `scheduled_at` 列。container 5/19 09:01 重启 supercronic reload 完成。 |
