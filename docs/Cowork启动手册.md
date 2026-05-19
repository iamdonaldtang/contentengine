# Cowork 起步:今天就能跑通的一条真实稿子

> **目的**:你今天/今晚就要在 Cowork 里第一次完整跑一条稿子,这份手册告诉你**3 个连接好的平台**(LinkedIn / LinkedIn Carousel / YouTube Shorts)端到端怎么走。
>
> **engine 当前状态**(2026-05-19 W22-W24 ship 后):
> - ✅ 249 pytest 全过(container py3.12.13)
> - ✅ Postiz API 完全打通(LinkedIn + YouTube 2 个 integration UUID 已写 config.yaml)
> - ✅ MPT 视频渲染就位
> - ✅ shlink 短链跑着(`l.taskon.xyz`)
> - ✅ yt_metadata 三级 fallback(Cowork 优先 + LLM + 硬模板)+ T-06 新增 `title_variants[3]` + `thumbnail_specs[3]`(YT Studio Test & Compare)
> - ✅ W22-W24 ship · T-02..T-08 · 4 new cron jobs + 3 migrations
> - ✅ supercronic 2026-05-19 09:01 重启 reload · T-05/T-07 已在 */10 min 调度
> - ⚠️ X / Medium / TikTok 未连,会自动 skip(不阻塞)
> - ⚠️ LinkedIn Carousel 需要 PDF 文件,本次先发文本版,Carousel PDF 留到 T15
> - ⚠️ T-08 矩阵号 cross-post plumbing 就绪 · 但默认 `cross_post: []` · 待 Donald 连 2nd 账号 integration 后启用

---

## 0 · 入场前确认 (30s)

容器全 healthy:

```powershell
docker compose ps
```

应该看到:`taskon-engine` healthy / `taskon-ingestion` healthy / `taskon-shlink` (unhealthy 是 healthcheck 配错,不影响功能)。**postiz / postiz-redis / postiz-postgres 也要在**(你独立的 docker-compose)。

---

## 1 · 选题 (10 min)

> **位置纪律**: 你 100% 在 Cowork (Claude Desktop) 里说话。Cowork 用它的 bash 工具去触发 engine CLI 命令。engine **没有 UI** — 它就是 headless 容器 + supercronic + Python scripts;你看不到也不需要登它任何界面。所有 engine 的输入(选题、终审、决策)都从 Cowork 喂下去,所有输出(markdown / SQLite 数据)都被 Cowork 读上来念给你听。

### 1.1 · 在 Cowork 里说这句话 → Cowork 用 bash 调 engine

```
我要起一条本周稿子,先跑选题流水线给我选 2-3 条候选。
帮我跑这 3 个命令然后把 selection_2026W20.md 念给我听:

bash:
docker compose exec engine python -m jobs.kol_watch --week 2026W20
docker compose exec engine python -m jobs.topic_ranker --week 2026W20
type D:\Taskon\marketing\engine\runtime\selection_2026W20.md
```

发生了什么:
1. Cowork 解析你的自然语言 → 决定用 bash 工具
2. Cowork 喊 `docker compose exec engine python -m jobs.kol_watch --week 2026W20` → engine 容器**内部** 跑 `jobs/kol_watch.py` → 调 X API → 写 candidates 表 + `runtime/kol_topics_2026W20.json`
3. Cowork 喊 `docker compose exec engine python -m jobs.topic_ranker --week 2026W20` → engine **内部** 跑 5 维评分 + 用上周 weekly_aggregates 调权 → 写 `runtime/selection_2026W20.md`
4. Cowork 用 `type` 读 `selection_2026W20.md` 全文返回给你
5. Cowork 用对话视角组织 Top10 念给你

**你全程不离开 Cowork 对话框**。

### 1.1.b · 为什么不让 Cowork 自己用 twitter-search-scraper 替代?

Cowork 确实有 `twitter-search-scraper` skill,能在 Cowork 里直接抓 KOL 推文。但 3 个理由让我们坚持走 engine:

| 维度 | engine `kol_watch` | Cowork `twitter-search-scraper` |
|---|---|---|
| 周日 10:00 自动 cron | ✅ supercronic | ❌ Donald 桌面关机就没了 |
| 写 `candidates` 表持久化 | ✅ SQLite | ❌ in-memory artifact |
| X API quota 失败 → Twikit cookie pool | ✅ 4 账号轮换 | ❌ 失败就失败 |
| 跟 topic_ranker 联动 | ✅ candidates 表直接读 | ❌ 数据不在同一个地方 |

**例外** — 这 3 种情况你可以让 Cowork 直接用 `twitter-search-scraper`:
1. engine 容器挂了(应急)
2. "我想看 @0xngmi 现在在聊什么"(ad-hoc 探索,不进候选池)
3. 冷启动头一周,X API key 还没拿到

**注意**: `kol_watch` 可能因 X API quota 429,会切 Twikit fallback 然后也挂(没配 cookie pool)。这种情况选题池只剩路 5(Donald 立场)+ 路 4(competitive-brief 上月跑过的)。**第一周可以选个手动议题先跑通流程**,这里给 3 个推荐:

| 议题 | hook | narrative_anchor | risk_level |
|---|---|---|---|
| **47% Quest 预算被 Bot 吃** | `47pct_bot` | `trust_collapse` | low |
| **Perps DEX 增长谎言** | `perps_dex_lies` | `pmf_missing` | low |
| **VC 代币 85% 破发的另一面** | `vc_dump_85pct` | `internal_fracture` | medium |

### 1.2 · Donald 拍板 1 条,Cowork 写选题卡

选完跟 Cowork 说(以下用议题 1 举例):

```
我选「47% Quest 预算被 Bot 吃」。
帮我新建一个 piece 占位,piece_id=2026W20-thread01。
落到 D:\Taskon\marketing\engine\runtime\drafts\2026W20-thread01\selection_card.yaml,字段:

piece_id: 2026W20-thread01
hook_type: 47pct_bot
narrative_anchor: trust_collapse
target_persona: crypto_cmo
risk_level: low
data_sources:
  - https://dune.com/...  # Donald 知道实际链接
  - https://defillama.com/...
title_hypothesis: 47% Quest 预算被 Bot 吃 — Q1 全平台数据交叉

写完确认给我。
```

Cowork 会把 yaml 写好,你看一眼。

---

## 2 · 起 X Thread 主稿 (30-60 min)

Cowork 用 `crypto-twitter-creator` skill 起 X Thread 终稿,落 `drafts/2026W20-thread01/xthread_final.md`。

Cowork prompt 模板:

```
用 crypto-twitter-creator skill 起一条 X Thread 终稿。

【选题卡】见 D:\Taskon\marketing\engine\runtime\drafts\2026W20-thread01\selection_card.yaml

【硬要求】
- 5-7 条推文
- 第一条:数字钩子 + 反共识结论
- 倒数第二条:埋钩子说"完整数据在评论区/replied"
- 最末条:CTA + 不带外链
- 数据 100% 真实,我会逐个核
- 14 禁词清单:全方位/革命性/颠覆/赋能/闭环/抓手/价值赋能/显著/dive into/let's explore/综上所述
- 涉及 TaskOn 业务(anti-Sybil / Quest 平台)主动披露利益

落到 D:\Taskon\marketing\engine\runtime\drafts\2026W20-thread01\xthread_final.md
```

写完 Cowork 念给你看,你改/通过。

---

## 3 · 改 4 平台版 + 视频脚本 + 渲染 (15 min · 全自动)

### 3.1 · adapter_orchestrator 产 4 平台版

```
docker compose exec engine python -m jobs.adapter_orchestrator --piece-id 2026W20-thread01
```

跑 ~ 30s-2min(4 个 LLM call),产出:

```
drafts/2026W20-thread01/
├── linkedin_post.md      ← 1500-2500 字 B2B 视角
├── carousel_10pages.md   ← 10 页文案(本次发不了,要 PDF 渲染)
├── medium_long.md        ← 长文(Medium 没连,自动 skip)
└── shorts_60s.md         ← 60s 视频脚本
+ voice_report.md         ← 自动跑 voice_checker
```

### 3.2 · 看 voice_report 有没有禁词

```
type D:\Taskon\marketing\engine\runtime\drafts\2026W20-thread01\voice_report.md
```

如果出现 `state=needs_revision`,**兼职女生改禁词**(不是 AI 自动改 — B1 §5 关 1 红线),改完重跑 adapter。

### 3.3 · 渲染 60s Shorts 视频 ★ 2026-05-16 改成 async submit-and-exit

```
docker compose exec engine python -m jobs.mpt_runner --piece-id 2026W20-thread01 --voice zh-CN-YunxiNeural-Male
```

**< 1 秒返回**(不再阻塞 5-10 min)。命令立即写一行 `mpt_tasks` 行(`status='submitted'`)然后退出。MPT 渲染完(5-40 min)用 HMAC webhook POST 回 `/api/mpt-callback`,engine 异步下载 mp4 → `drafts/2026W20-thread01/shorts_60s.mp4`(20-50 MB)。

监控渲染进度(可选):
```
docker compose exec engine python -c "from lib.db import db; row=db.mpt_tasks.get_in_flight_for_piece('2026W20-thread01'); print(dict(row) if row else 'done — check completed_at')"
```

⚠️ **已废弃** `--timeout` 参数(旧 sync poll 用)。dropped callback 场景由 `jobs/mpt_reconciler` 每 5 min 自愈兜底。详见 [`architecture.md §9`](architecture.md)。

### 3.4 · Custom Slice KOL DM 草稿 (可选 · 5 min) ★ T-02 W23 新增

跑完 adapter 后,顺手为这条 piece 出 3 条 KOL Custom Slice DM 草稿:

```
docker compose exec engine python -m jobs.custom_slice_generator --piece-id 2026W20-thread01
```

engine 自动:
1. 读 `selection_card.yaml` 抽 narrative_anchor / hook_type / key_data_points 做 token bag
2. 跟 `config/kol_watchlist.yaml` 30 KOL 的 focus + angle 做 token-overlap 匹配
3. 同分按 tier(A → B → C)排序 · 取 top-3
4. MiniMaxi LLM 出每位 KOL 个性化 DM(≤280 字)+ Canva 改图参数 JSON

输出:
```
drafts/2026W20-thread01/
├── custom_slice_<handle1>.md       ← Donald 周四发布后 1h 内手发
├── custom_slice_<handle1>.canva.json  ← 兼职女生 Canva 改图参数
├── custom_slice_<handle2>.md
└── custom_slice_<handle2>.canva.json
```

**红线**:engine 永不自动 DM · Donald 手发(B1 §6 + B3 §1.3 模型 4)。

⚠️ 过滤:`content_type` 必须 in `{thread, long, methodology, case_study, data_insight, playbook}`,不在的话用 `--force` 跳过过滤。

---

## 4 · UTM 短链 (5 min)

```
docker compose exec engine python -m jobs.utm_generator \
  --piece-id 2026W20-thread01 \
  --target-url https://taskon.xyz/benchmark-report \
  --platforms twitter,linkedin,medium,youtube \
  --accounts donald_en,taskon_official \
  --hook-type 47pct_bot
```

产出 `drafts/2026W20-thread01/utm_links.json` ,每个平台 1 个长链 + 1 个 shlink 短链。

---

## 5 · 准备 YouTube 元数据 (5 min · 2 选 1)

### 5.1 · 选项 A · Cowork 帮你写 yt_metadata.yaml(推荐)★ 2026-05-16 改成占位符

```
帮我为这条 piece 写 YouTube 上传元数据,落到 yt_metadata.yaml。

参考 docs/yt_metadata_spec.md §2.1 完整字段示例 + §2.2 硬限制。

【取数】
- 从 D:\Taskon\marketing\engine\runtime\drafts\2026W20-thread01\selection_card.yaml 取 hook_type 和 narrative_anchor
- shorts_60s.md 全文做 description 第一段的钩子复述

字段:
- title (≤95 字符,含数字钩子和"TaskOn",任一即可,不要堆)
- description (3 段:① 钩子复述 60-120 字 ② CTA 句子末尾放 {{CTA_URL}} 占位符 ③ TaskOn 引导)
  ★ 2026-05-16 SOP 变更:**不要写真实 URL**,description 唯一的链接形式是字面量 {{CTA_URL}} —
    schedule_planner 真发前会按 (platform, account) 替换为 utm_links.json 里的长链
  例:`你做的是 Schwab 卖得了的还是卖不了的? 评论告诉我 -> {{CTA_URL}}`
- privacy: public
- tags: 5-8 个英文小写中划线 SEO 词
- category_id: 22
- not_made_for_kids: true

写好后落到 D:\Taskon\marketing\engine\runtime\drafts\2026W20-thread01\yt_metadata.yaml
```

写完你看一眼,确认 title 不别扭 + description 含 `{{CTA_URL}}` 占位符(必须有,且通常只 1 次)。

**为什么改占位符**:utm_links.json 里有 `youtube_donald_en` / `youtube_taskon_official` 两个账号,不同 piece 用不同账号 — 让 yt_metadata 写死 URL 会绑死账号。占位符 + schedule_planner 注入按 `config.yaml :: postiz.accounts` 决定哪个账号。详见 [`marketing/CLAUDE.md §4.5`](../../CLAUDE.md)。

### 5.2 · 选项 B · 让 engine LLM 派生(不动手)

跳过 5.1,直接到第 6 步。schedule_planner 在 yt_shorts 平台前会自动派生 → 写 `yt_metadata_auto.yaml`。但你**应该终审一下**,跑完第 6 步 dry-run 后看:

```
type D:\Taskon\marketing\engine\runtime\drafts\2026W20-thread01\yt_metadata_auto.yaml
```

不满意可以覆盖一份 `yt_metadata.yaml`(无 _auto 后缀)再重跑。

---

## 6 · 调度 dry-run 检查时间表 (1 min)

```
docker compose exec engine python -m jobs.schedule_planner \
  --piece-id 2026W20-thread01 --dry-run --base-monday 2026-05-25
```

(`--base-monday` 是下周一锚,真排程不用传)

期望输出:

```
DRY-RUN · platform=linkedin_post     scheduled_at=2026-05-26T09:00:00-04:00 ...
DRY-RUN · platform=linkedin_carousel scheduled_at=2026-05-26T10:00:00-04:00 ...
WARNING · skip medium_long: ... not configured
DRY-RUN · platform=yt_shorts         scheduled_at=2026-05-28T09:00:00-04:00 ... yt_meta.source=cowork title='47% Quest...'
WARNING · skip tiktok: ... not configured
WARNING · skip x_thread: ... not configured
schedule_planner done: planned=6 scheduled=3 skipped=3 failures=0 status=warning
```

3 平台 scheduled、3 platform skip — **健康状态**。

如果 yt_meta.source=`llm_auto` 而不是 `cowork`,说明你跳过了 5.1 — 去看 `yt_metadata_auto.yaml`,不满意再回 5.1 手写覆盖。

---

## 7 · Donald 终审 (3h 周三 09-12)

按 B1 §5 关 4,**这是不可让渡的人工动作**。检查清单:

| 关 | 检查项 |
|---|---|
| **数据关** | 每个数字打开 DefiLlama / Dune / 内部 dashboard 比对 |
| **可操作关** | 把自己当客户读一遍,回答"明天第一步做什么" |
| **YT 元数据** | title 不别扭、description CTA 链接正确、tags 命中 SEO 关键词 |
| **★ W22 新增 · Algorithm Rules 段** | 看 `voice_report_x_thread.md` 末尾 `## Algorithm Rules: PASS \| FAIL` 段 — FAIL 即 X 主推第 1 条含 `https://` 外链(B3 §2 杠杆 2 算法降权 30-50%)· 修复:把 URL 移到 thread 第 2 条自我 Reply |
| **★ T-06 YT A/B variants** | 如有 yt_shorts,看 `yt_metadata_auto.yaml` 里 `title_variants[3]` + `thumbnail_specs[3]` · Donald 发布后到 YT Studio "Test & Compare" 手动 A/B 测试 |

数据关失败 → **这条死了,不发**。**绝不允许** AI 改数字。

---

## 8 · 真发(命令一行,但慎按)

```
docker compose exec engine python -m jobs.schedule_planner --piece-id 2026W20-thread01
```

⚠️ **真在 Postiz 里建 3 个 scheduled post**:

| 平台 | scheduled_at | 内容来源 |
|---|---|---|
| LinkedIn Post | 下周二 09:00 ET | `linkedin_post.md` |
| LinkedIn Carousel | 下周二 10:00 ET | `carousel_10pages.md`(注意:Postiz 拿不到 PDF 可能只发文本) |
| YouTube Shorts | 下周四 09:00 ET | `shorts_60s.mp4` + yt_metadata |

跑完去 Postiz UI `http://localhost:4007` → Launches/Scheduled 应该看到 3 条新任务。

### 8.1 · 30min 算法借力 Lark 自动提醒 ★ T-05 / T-07 W22 新增

publishings 行写入后,`scheduled_at` 列存了 Postiz-promised 发布时刻(UTC)。每 10 分钟两个 cron 自动扫:

| Job | 触发 | Lark 提醒 |
|---|---|---|
| `reply_density_alert` */10min | X 发后 30min(`platform LIKE 'x_%'`)| P2 · "X 主推发了 30min · 5-人 Reply 队伍 请上场 (B3 §2 杠杆 1)" |
| `linkedin_engagement_alert` */10min | LinkedIn 发后 30min(`platform LIKE 'linkedin%'`)| P2 · "LinkedIn 发了 30min · 请回 ≥5 条评论 (B3 §4 杠杆 1)" |

Idempotent: `publishings.<col>_alert_sent` sentinel 列 · 每 piece 每平台只 nudge 一次。

**红线**:engine 只产生 Lark P2 提醒 · 永远不替你发 Reply / 回评论(B1 §6 + B3 纪律)。

Donald 看到 Lark 后的动作:
- X Thread → 立即打开 X,自己 + 4 BD 上 ≥1 条带新观点的 Reply(每条 ≥30 字)
- LinkedIn → 立即打开 LinkedIn,给评论区前 5 条回复 1-2 句

详见 [`Donald_每日操作手册_v1.md §2.8`](Donald_每日操作手册_v1.md)。

---

## 9 · 后续观察(自动)

| 时点 | 自动跑 | 输出 |
|---|---|---|
| 发布后 30min | metrics_collector | metrics_daily 30m 时点 |
| 发布后 24h | metrics_collector | metrics_daily 24h 时点 |
| 发布后 7d | metrics_collector | metrics_daily 7d 时点 |
| 周日 18:00 | weekly_reporter | runtime/weekly_report_2026W20.md(LLM 写或 _bare 兜底) |
| 周日 18:30 | performance_analyzer | Top1/Bottom1 复盘 |

下周一上午 Cowork 念周报给你听。

---

## 10 · 出错怎么办 cheatsheet

| 现象 | 命令 |
|---|---|
| 哪个 cron 挂了 | `docker compose exec engine python -c "from lib.db import db; [print(dict(r)) for r in db.fetchall('SELECT job_name,last_run_at,status,error_message FROM heartbeat ORDER BY last_run_at DESC LIMIT 10')]"` |
| 哪里 P 告警了 | `... SELECT * FROM publish_failures ORDER BY id DESC LIMIT 10 ...` |
| Postiz 通不通 | `docker compose exec engine python -c "from sources.postiz import postiz; print(len(postiz.list_integrations()))"` |
| 容器日志 | `docker compose logs -f engine` |
| 删一条没用的 piece | 删 `runtime/drafts/<piece_id>/` 整个目录 + `state.db` 里 pieces 表 |
| **★ W22-W24 新增** 看 4 新 job 心跳 | `docker compose exec engine python -c "from lib.db import db; [print(dict(r)) for r in db.fetchall(\"SELECT job_name,last_run_at,status,rows_written FROM heartbeat WHERE job_name IN ('reply_density_alert','linkedin_engagement_alert','custom_slice_generator','kol_relation_tracker') ORDER BY last_run_at DESC LIMIT 12\")]"` |
| **★ T-03** Donald 实发 KOL Reply 后记账 | `docker compose exec engine python -m jobs.kol_relation_tracker log-dm --kol @handle --kind reply --tweet-url https://x.com/.../status/<id> --piece-id <piece>` |
| **★ T-03** 启用 KOL scan cron | 编辑 `docker/crontab` uncomment `1 9 * * *` 行 → `docker compose restart engine` |
| **★ T-08** 启用矩阵号 cross-post | Donald 在 Postiz 连 2nd 账号 → 填 `config.yaml :: postiz.routing.<platform>.cross_post[0]` → 下次 schedule_planner 自动多排 |

---

## 11 · 阻塞清单(下一次再补)

| 阻塞                          | 谁                                | ETA         |
| --------------------------- | -------------------------------- | ----------- |
| LARK_WEBHOOK_URL 没填         | Donald 群里加机器人                    | 5 min       |
| X (Twitter) Postiz 没连成      | Donald 重 OAuth 一次 OR 用 x-browser | 10 min - 1h |
| Medium Integration Token 没拿 | Donald Medium Settings 拿         | 5 min       |
| TikTok Developer 审批         | 1-3 天                            | TikTok 那边   |
| LinkedIn Carousel PDF 渲染    | T15 待做                           | 1 天工时       |
| Listmonk + AWS SES 部署       | 另会话                              | 4-6h        |
| 落地页前端嵌入 3 JS                | TaskOn 前端                        | 1 天         |

最快增量:**填 LARK + 完成 X OAuth** = 1 小时内多 1 个真发平台 + 凌晨告警可见。

---

## 12 · 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-05-15 | 首版,T14 完成后写,3 平台真发就绪状态 |
| v0.2 | 2026-05-16 | §3.3 mpt_runner 改 async submit-and-exit (删除 `--timeout`)。§5.1 yt_metadata.yaml description 改用 `{{CTA_URL}}` 占位符(不写真实 URL)。配套 [`architecture.md §9-§10`](architecture.md) async webhook + CTA placeholder 重大改造。**piece 02 首次真上 YouTube** [es7XQWghoSM](https://www.youtube.com/watch?v=es7XQWghoSM) |
| **v0.3** | **2026-05-19** | **W22-W24 ship · 7 个 B3 任务 (T-02..T-08)**。§0 现状加 249 pytest + supercronic reload + 4 new cron jobs;§3.4 Custom Slice 出 KOL DM 草稿(T-02);§7 Donald 终审多看 `Algorithm Rules` 段(T-04)+ YT title_variants/thumbnail_specs(T-06);§8.1 30min Lark 自动提醒(T-05/T-07);§10 cheatsheet 加 4 新条目(看新 job 心跳 / log-dm / 启 T-03 cron / 启 T-08 路由)。详见 [`B3_engine_落地路线_v1.md`](B3_engine_落地路线_v1.md) + [`architecture.md §12`](architecture.md) |
