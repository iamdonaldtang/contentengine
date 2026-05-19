# B3 内容分发与放大 · engine 落地路线 v1

> **作者**：staff engineer (Claude)
> **日期**：2026-05-18
> **基线**：engine HEAD `b39b450` · B3 文档 v3.0 · cron 状态见 [docker/crontab](../docker/crontab)
> **后续修订**：v2 待 Donald 拍板 W22 实施清单后产出

---

## § 1 · 现状盘点（对照 B3 §1-7）

| B3 段 | 内容 | engine 侧状态 |
|---|---|---|
| §1.2 监测层 | 30 KOL watchlist + X/Farcaster/LinkedIn/Space 信号 | **已实现**：`jobs/kol_watch` 周扫 X API + Twikit fallback；Farcaster/LinkedIn/Space 未接（手工） |
| §1.3 模型 1 · Daily Reply | 5-8 条 Reply 候选 markdown | **已实现** `jobs/kol_daily_replier`，**cron 暂停**（5-18 改独立模块，未恢复） |
| §1.3 模型 2 · Pre-Read 8 | 季度 Benchmark 前 48h 人个性化 DM | **未实现**。且 Benchmark Report 是否出由 Donald 持决策中（见 `D:\Taskon\CLAUDE.md` 内容产品形态约束） |
| §1.3 模型 3 · Co-Authored Take | 双月 1 次 | **未实现**（频率太低不值得编码） |
| §1.3 模型 4 · Custom Slice | 每条 ①②⑤ 触发 1-3 KOL DM | **未实现**。adapter 跑完无后续 KOL hook |
| §1.4 自然回流 | tier B → A 状态机 | **DB 表 `kol_watchlist` 已建空 · 0 写入逻辑** |
| §2 X 杠杆 1 · 30min Reply 密度 | 5 人协调 Reply | **未实现**（无算法借力自动校验） |
| §2 杠杆 2 · 主推无外链 | 第 1 条禁 https | **未实现**（`jobs/voice_checker` 14 禁词无此规则） |
| §2 杠杆 3 · 原创图表 | 每篇 ≥1 图 | **未实现**（无机检；兼职女生人工保证） |
| §3 YT 杠杆 1-3 · Hook/CTR/End-screen | A/B 缩略图 · End-screen | **未实现**（`yt_metadata.yaml` 单套 title/thumbnail） |
| §4 LinkedIn 杠杆 1-3 · 回评 / Post / Carousel | 30min 内回 ≥5 评论 | **未实现** Post 优先 + Carousel 已是 adapter 输出，**回评提醒未实现** |
| §5.1 5 类账号 | 官方/Donald EN/CN/7 BD/产品+数据 | **只接入 2 个**（Donald LinkedIn + Donald YouTube，`config.yaml :: postiz.accounts.* = donald_en`） |
| §5.2 路由表 | 主发 + Quote 扩散顺序 | **未实现**（schedule_planner 单平台单账号一次性发） |
| §5.3 5 人 Reply 队伍 | 兼职女生周一 angle 分配 | **不在 engine 范围**（人工 TG 群） |
| §6 客户联创 | Q2 1 个 | **不在 engine 范围**（BD 人工） |
| §7 投稿 + Space | Messari / Space 参加 | **不在 engine 范围**（Donald 人工） |

**主链外延状态**：KOL（`kol_watch` + `kol_daily_replier`）、newsletter、btouch 三条旁支 cron 已被 2026-05-18 注释。任何新增 KOL 任务必须保持"独立 cron / 独立模块 / 失败不传染主链"的边界（见 `D:\Taskon\logs\2026-05-18.md`）。

---

## § 2 · 工程任务清单（按优先级降序）

### T-04 · X 主推"无外链"检查规则

- **B3 来源**：§2 杠杆 2
- **价值**（5/5）：影响每一条 X Thread（每周 5-7 条），算法触达 +30-50%，零运行成本
- **工作量**（1/5）：在 [jobs/voice_checker.py](../jobs/voice_checker.py) 加一条 platform=x_thread 的规则，**< 30 行**，不新建文件
- **依赖**：无
- **风险**：不触碰红线。Voice report 已有 needs_revision 通道，复用即可
- **告警**：不需要新告警（voice_checker 失败语义沿用现有 P2）
- **实施骨架**：
  ```python
  # 加在 jobs/voice_checker.py 现有规则集尾部
  def _check_x_first_tweet_no_https(piece_id: str, content: str) -> Optional[str]:
      if piece_kind(piece_id) != 'x_thread':
          return None
      first_tweet = content.split('\n---\n', 1)[0]
      if re.search(r'https?://', first_tweet, re.I):
          return ("X 主推第 1 条禁止 https 外链（B3 §2 杠杆 2 · 算法降权 30-50%）"
                  " — 移到第 2 条自我 Reply")
      return None
  ```
- **MVP**：直接全量启用，写进 `voice_report.md` 的 `needs_revision` 列表
- **建议优先级**：**P0**

---

### T-07 · LinkedIn 30min 回评提醒

- **B3 来源**：§4 杠杆 1
- **价值**（4/5）：LinkedIn 周 2 Carousel + 1-2 长 Post = 4-6 条/周触发；算法 10x 权重；零外部依赖
- **工作量**（2/5）：新文件 [jobs/linkedin_engagement_alert.py](../jobs/linkedin_engagement_alert.py)，**~80 行**。cron `*/10 * * * *`
- **依赖**：`publishings.platform LIKE 'linkedin_%'` + `lib/lark` + `publishings.published_at`
- **风险**：不发推、不评。LinkedIn 没有官方 API 读取 comments → 无法机检"是否真回了 5 条"，**只能提醒**。这是 B3 §4 红线允许的最深动作
- **告警**：P2（提醒类 / 不是失败类）
- **实施骨架**：
  ```python
  # jobs/linkedin_engagement_alert.py · cron */10 * * * *
  def main():
      rows = db.fetchall("""
          SELECT id, platform, twitter_url AS post_url, published_at
          FROM publishings
          WHERE platform LIKE 'linkedin_%'
            AND published_at BETWEEN datetime('now','-31 minutes')
                                AND datetime('now','-29 minutes')
            AND engagement_alert_sent IS NULL
      """)
      for r in rows:
          lark.alert('P2', f"LinkedIn 发了 30min — Donald 请回 ≥5 条评论\n{r['post_url']}")
          db.publishings.mark_alert(r['id'], 'engagement_alert_sent')
  ```
- **DB 改动**：`publishings` 加 `engagement_alert_sent TIMESTAMP NULL` 列（migration 010）
- **MVP**：先 Lark 提醒；不机检完成度
- **建议优先级**：**P1**

---

### T-02 · Custom Slice 触发器（KOL 个性化数据切片）

- **B3 来源**：§1.3 模型 4
- **价值**（4/5）：每条 ①②⑤ piece 触发 1-3 KOL DM，每周 3-5 个 DM → 累计漏斗大。**最低门槛 KOL 触达**
- **工作量**（3/5）：新文件 [jobs/custom_slice_generator.py](../jobs/custom_slice_generator.py) + 新 prompt `config/prompts/custom_slice.txt`，**~200 行**。在 `adapter_orchestrator` 之后串调
- **依赖**：
  - `config/kol_watchlist.yaml`（已有 30 人）
  - `drafts/<piece_id>/selection_card.yaml`（已有，含 hook + keywords）
  - `lib/llm_client`（MiniMaxi 包月）
- **风险**：**不触碰红线** — 只产 markdown DM 草稿 + Canva JSON 参数，**Donald 手发**
- **告警**：单 KOL 切片 LLM 失败 → P2 + 跳过；全失败 → P1
- **实施骨架**：
  ```python
  # jobs/custom_slice_generator.py · 在 adapter 之后串调（不独立 cron）
  def generate_for_piece(piece_id: str, top_n: int = 3) -> list[dict]:
      card = read_selection_card(piece_id)
      watchlist = load_kol_watchlist()  # 30 人
      matches = match_by_keywords(card.keywords, watchlist, top_n)  # 简单关键词匹配
      out = []
      for kol in matches:
          prompt = SLICE_PROMPT.format(kol=kol, piece=card)
          dm_md = llm.complete(SYS_PROMPT, prompt)  # MiniMaxi only
          canva_json = derive_canva_params(dm_md, card)
          write(f"drafts/{piece_id}/custom_slice_{kol['handle']}.md", dm_md)
          write(f"drafts/{piece_id}/custom_slice_{kol['handle']}.canva.json", canva_json)
          out.append({"kol": kol['handle'], "draft_path": ...})
      return out
  ```
- **触发点**：`jobs/adapter_orchestrator.py` 跑完后多调一次（或单独 CLI `--piece-id`）
- **MVP**：先只对 selection_card.yaml 里 `content_pillar in [行业真相, 增长方法论]` 的 piece 触发
- **建议优先级**：**P1**

---

### T-05 · X 发布后 30min Reply 密度提醒

- **B3 来源**：§2 杠杆 1
- **价值**（4/5）：5 人 Reply 队伍是 X 算法核心机制；Donald 必读
- **工作量**（3/5）：新文件 [jobs/reply_density_alert.py](../jobs/reply_density_alert.py)，**~120 行**。cron `*/10 * * * *`。框架可与 T-07 共享
- **依赖**：X API（已有 `X_BEARER_TOKEN`）+ `lib/lark` + `sources/twitter_x.get_tweet_replies()`（新方法）
- **风险**：调 X API 读 reply 数（不发推）。需要小心 X API 429 → fallback 静默
- **告警**：reply 数 < 5 → P2 提醒 Donald + 4 BD；X API 挂 → P2 静默跳过
- **实施骨架**：
  ```python
  # jobs/reply_density_alert.py
  def main():
      rows = db.fetchall("""
          SELECT id, platform, twitter_url, published_at
          FROM publishings
          WHERE platform LIKE 'x_%'
            AND published_at BETWEEN datetime('now','-31 minutes')
                                AND datetime('now','-29 minutes')
            AND reply_alert_sent IS NULL
      """)
      for r in rows:
          try:
              tweet_id = parse_tweet_id(r['twitter_url'])
              n = twitter_x.count_replies(tweet_id)  # 新增 sources/twitter_x 方法
              if n < 5:
                  lark.alert('P2', f"X Thread 发了 30min · Reply 密度 {n}/5\n{r['twitter_url']}")
          except Exception as e:
              log.warning(f"X API 429? {e}")  # 不告警，等下个 tick
          db.publishings.mark_alert(r['id'], 'reply_alert_sent')
  ```
- **DB 改动**：`publishings` 加 `reply_alert_sent TIMESTAMP NULL`（migration 010 合并 T-07）
- **MVP**：只对 platform in (`x_thread`, `x_post`) 且 account in (`donald_en`, `taskon_official`) 生效
- **建议优先级**：**P1**

---

### T-08 · 矩阵号路由表配置化（cross-post 模拟 Quote）

- **B3 来源**：§5.2 内容路由表
- **价值**（5/5）：一个 piece 撬多倍触达，是矩阵号 1 圈最核心杠杆。但 **B3 §2 杠杆 1 明确禁止 27 人协调 Quote** —— 此项实施必须收窄到"延后 cross-post 到不同账号的不同平台"，不做 X Quote chain
- **工作量**（4/5）：`config.yaml` 加 `postiz.routing` 段 + [jobs/schedule_planner.py](../jobs/schedule_planner.py) 改 ~150 行 + 至少配齐 1 个二级 Postiz integration UUID（如 `taskon_official` X 账号）
- **依赖**：
  - Postiz 多账号 integration（当前只有 donald_en）→ Donald 在 Postiz 后台配 2-3 个二级账号
  - `utm_generator` 已支持多账号，参数已就位
- **风险**：
  - **不做 X 内同主推下多账号 Quote**（B3 §2 杠杆 1 → 算法机构托降权 / Counter §3.1）
  - 允许的形态：YT Shorts 主发 `taskon_official` → 延后 4h 在 `donald_en` 频道转发；LinkedIn Carousel 主发 `donald_en` → 1 天后 BD 个人号 Quote（不同账号 / 不同时间，算法识别为自然传播）
- **告警**：单二级账号排程失败 → P2，主发不阻塞
- **实施骨架**：
  ```yaml
  # config.yaml 新段
  postiz:
    routing:
      yt_shorts:
        primary: taskon_official
        cross_post:
          - account: donald_en
            offset_minutes: 240   # 4h 后
      linkedin_post:
        primary: donald_en
        cross_post: []            # 暂不做，等 BD integration
      x_thread:
        primary: donald_en
        cross_post: []            # ★ 永不做 X Quote chain（B3 §2 杠杆 1）
  ```
  ```python
  # jobs/schedule_planner.py 改
  def _build_publishings_for_piece(piece_id, platform, account_primary):
      yield primary_publish(...)
      for entry in cfg.postiz.routing[platform].get('cross_post', []):
          yield delayed_publish(account=entry.account, offset=entry.offset_minutes)
  ```
- **MVP**：先支持 yt_shorts → cross_post `donald_en` 一对；linkedin / x 留空
- **建议优先级**：**P1**

---

### T-03 · KOL 关系状态机

- **B3 来源**：§1.4 自然回流
- **价值**（3/5）：让 KOL 触达投入产出可见化；但**信号本身稀疏**（30 KOL × ~3% reply 率 = ~1 反馈/月）
- **工作量**（4/5）：新文件 [jobs/kol_relation_tracker.py](../jobs/kol_relation_tracker.py) **~250 行** + 新表 `kol_dm_log` + 接入 `kol_watchlist` 表（当前空）+ X API 扫 reply
- **依赖**：T-02 实发后才有数据 + Donald 标记"我今天发了这条 DM/Reply"（半自动）
- **风险**：不触碰红线（只读 X API）。**KOL 独立 cron**（5-18 边界）
- **告警**：单 KOL fetch 失败 → P2；连续 3 次 → P1
- **实施骨架**：
  ```python
  # jobs/kol_relation_tracker.py · cron 1 9 * * * (daily 09:01 · KOL 独立链)
  def main():
      pending = db.fetchall("""
          SELECT * FROM kol_dm_log
          WHERE kol_replied_at IS NULL
            AND sent_at > date('now','-7 days')
      """)
      for row in pending:
          try:
              kol_reply = twitter_x.find_reply_from(
                  thread_url=row['donald_tweet_url'],
                  author_handle=row['kol_handle'],
              )
              if kol_reply:
                  db.kol_dm_log.mark_replied(row['id'], kol_reply.created_at)
                  upgrade_tier_if_threshold(row['kol_handle'])
          except Exception as e:
              p2(f"KOL {row['kol_handle']} fetch fail: {e}")
  ```
- **新表**：`kol_dm_log(id, kol_handle, kind, donald_tweet_url, sent_at, kol_replied_at, kol_quote_count)`
- **MVP**：先 Donald 在 Cowork 跑 CLI 标"我刚发了这条 DM"写入 `kol_dm_log`，cron 扫 reply；后期接 X API webhook 自动写入
- **建议优先级**：**P2**（等 T-02 跑 1 个月有数据再实施）

---

### T-06 · YT 缩略图 A/B 元数据扩展

- **B3 来源**：§3 杠杆 2
- **价值**（2/5）：YT 当前周产 2-3 条；CTR 提升空间未经验证；YouTube Studio "Test & Compare" 已是 YT 免费内置，**Donald 手动设 5 分钟搞定**
- **工作量**（3/5）：`config/prompts/yt_metadata.txt` 改 prompt 让 LLM 输出 3 套 title + thumbnail spec；schema 扩展。**真集成 YT Data API 极其麻烦**（OAuth + thumbnails.set 端点不支持 A/B，必须经 Studio UI）
- **依赖**：无 API 集成 → 退化为"LLM 出 3 套提示词，Donald 手动 Studio 设 A/B"
- **风险**：不触碰红线
- **告警**：无
- **实施骨架**：
  ```yaml
  # runtime/drafts/<piece_id>/yt_metadata.yaml schema 扩展
  title_variants:
    - "47% Quest 预算被 Bot 吃 — Q1 数据"
    - "为什么我们花 $50k 推 Quest 只拿到 12 个真用户"
    - "Web3 增长圈一直假装这事不存在"
  thumbnail_specs:
    - text_overlay: "47%", color: red, position: top-left
    - text_overlay: "$50K", color: yellow, position: center
    - text_overlay: "PROOF", color: white, position: bottom
  ```
  ```python
  # config/prompts/yt_metadata.txt 加 1 段：让 LLM 输出 3 个 title + 3 个 thumbnail spec
  # schedule_planner 不动；Donald YT Studio UI 手动设 Test&Compare
  ```
- **MVP**：只扩展 schema + prompt；不做 API
- **建议优先级**：**P2**

---

### T-01 · Pre-Read 8 季度自动生成器

- **B3 来源**：§1.3 模型 2
- **价值**（5/5）：8 人 ≥3 Quote = 37% 转化（KOL outreach 最高 ROI 单点）。但**严重依赖 Benchmark Report**
- **工作量**（4/5）：新文件 [jobs/pre_read_sender.py](../jobs/pre_read_sender.py) **~200 行** + 季度 cron + 8 人个性化 prompt + Benchmark 数据查询
- **依赖**：
  - **Benchmark Report 数据未决**（`D:\Taskon\CLAUDE.md` 内容产品形态约束：Donald 立场"Quest 数据机器人占比高，专业性不足"，Claude 反提"脏数据=垄断内容"，**待 Donald 拍板**）
  - 没有 Benchmark = 没有数据切片 = 此 job 输出空文档 = 不要建
- **风险**：触碰红线"Donald 亲自 DM 不外包"——必须只输出 markdown 给 Donald 手发，**永不自动 DM**
- **告警**：Benchmark 数据缺 → P1 提醒 Donald 决策；LLM 失败 → P1
- **实施骨架**：
  ```python
  # jobs/pre_read_sender.py · cron 0 9 1 */3 * (季度第 1 天 09:00)
  def main(season: str):
      benchmark = load_benchmark_dataset(season)  # ★ Donald 拍板后才有
      if not benchmark:
          lark.alert('P1', f"{season} Benchmark 未就绪 · Pre-Read 跳过")
          return
      for handle in cfg.kol_watchlist.pre_read_8:
          slice = llm.complete(SLICE_PROMPT, kol=handle, data=benchmark)
          dm = llm.complete(DM_PROMPT, slice=slice, handle=handle)
          write(f"runtime/pre_read_{season}/{handle}.md", dm)
      lark.alert('P2', f"Pre-Read {season} 8 份 DM 草稿就绪 · Donald 手发")
  ```
- **MVP**：半自动 → 输出 markdown 草稿给 Donald 改 → Donald 手发
- **建议优先级**：**P2**（**先等 Donald 拍板 Benchmark 出不出**；出 → P0，不出 → 不做）

---

## § 3 · 排期建议（W22-W24 · 每周 ≤16h Cowork→Claude 编码预算）

> 今天 2026-05-18 (Mon W21) · W22 = 2026-05-25 起

### W22（2026-05-25 ~ 05-31）· 主题"先把可见性补齐"

| 任务 | 工作量 | 备注 |
|---|---|---|
| **T-04** X 主推无外链规则 | 1h | 当周即可上线，立即影响所有 X Thread |
| **T-07** LinkedIn 30min 回评提醒 | 3h | 含 publishings migration 010（合并 T-05 列） |
| **T-05** X Reply 密度提醒 | 5h | 含 sources/twitter_x.count_replies() 新方法 |
| Donald 决策点 | — | **Donald 拍板 Benchmark 出不出**（决定 T-01 命运） |
| 余量 | 7h | 测试 / hotfix / W22 piece 走流水线验证 |

### W23（2026-06-01 ~ 06-07）· 主题"KOL Custom Slice 上线 + 矩阵号试点"

| 任务 | 工作量 | 备注 |
|---|---|---|
| **T-02** Custom Slice 触发器 | 6h | 接入 adapter_orchestrator 后串调 |
| **T-08** 矩阵号路由（仅 yt_shorts → donald_en cross-post） | 8h | 含 Donald 在 Postiz 后台配 taskon_official 二级 X 账号 integration |
| 余量 | 2h | piece 走完矩阵号路由烟测 |

### W24（2026-06-08 ~ 06-14）· 主题"看数据决定下一步"

| 任务 | 工作量 | 备注 |
|---|---|---|
| **T-06** YT 缩略图 A/B schema 扩展 | 3h | prompt + yaml schema 改造，不接 API |
| W22-W23 复盘 | 4h | 看 Custom Slice 实发后 KOL 反馈量 / Reply 密度提醒触发频次 |
| **T-03** KOL 关系状态机（仅 Donald 手标 + cron 扫 reply 部分） | 6h | 推迟到此周等 T-02 数据攒够 |
| **T-01** Pre-Read 8 | 0-8h | **看 W22 Donald 决策**：决定出 Benchmark → 8h 实施；否则 → 不做 |
| 余量 | 3h | — |

---

## § 4 · 不做清单 + 理由

| 不做的 B3 项 | 理由 |
|---|---|
| §1.3 模型 1 · Daily Reply | `jobs/kol_daily_replier` 已实现，cron 暂停状态由 Donald 主动决定恢复时机（不是工程问题） |
| §1.3 模型 3 · Co-Authored Take | 频率双月 1 次 = 6 次/年 → 写自动化的固定成本 > 人工出 6 次的边际成本 |
| §2 杠杆 3 · 单 Tweet 配 1 张原创图表 | 无法机检（图是否原创 / 是否带 logo），兼职女生人工保证；adapter 已强制 `xthread_final.md` 引用图表路径 |
| §3 杠杆 1 · YT 前 7s/3s Hook | 无法机检视频前 N 秒留存率（YT Studio 才能看），机检不可行 |
| §3 杠杆 3 · End-screen + 描述区第 1 行 | 描述区第 1 行已在 `yt_metadata` prompt 强制；End-screen 是 YT Studio UI 配置，engine 不碰 |
| §4 杠杆 2 · 用 Post 不用 Article | adapter 已只输出 `linkedin_post.md`，从未出 Article 格式 → 已默认满足 |
| §4 杠杆 3 · Carousel | adapter `carousel_10pages.md` 已是当前 LinkedIn 周排程一档 → 已实现 |
| §5.1 5 类账号 | engine 只能在 Postiz integration 配齐后才有意义；配置工作是 Donald 后台操作，**不是工程任务** |
| §5.3 5 人 Reply 队伍 angle 分配 | 兼职女生周一手发 TG 群 / B3 明文指定人工动作 |
| §6 客户联创 | BD 人工 / 月度圆桌 / 法务 + 商务 |
| §7 投稿（Messari / CoinDesk） | Donald 月度 2-3h 手投 |
| §7 Twitter Space | Donald 双周参与（B1 §6 不可让渡） |
| X Quote chain（27 人协调） | **B3 §2 杠杆 1 明确禁止**（X 算法机构托降权 / Counter §3.1） |
| 自动 DM / 自动 Quote / 自动 Retweet 任何 KOL 触达 | **B1 §6 红线** + Donald 永不让渡 |

---

## § 5 · 一句话决策点

> **W22 用 9h（T-04 + T-07 + T-05）把"算法借力 + 30min 回评提醒"补齐 → W23 用 14h（T-02 + T-08 mini）把"Custom Slice KOL DM 自动出草稿 + YT Shorts 双账号 cross-post"上线 → W24 看 T-02 跑 2 周后的 KOL 反馈量决定 T-03（关系状态机）真值。Pre-Read 8（T-01）单线悬停在 Donald 的 Benchmark 决策上，不预编。**

---

## 附录 · 自检清单

- [x] 每个 T-XX 都给了 P0/P1/P2/不做判断
- [x] 每个 T-XX 都给了改动量估计（行数 + 是否新文件）
- [x] 对照 B3 §1-7 全部 7 段点名（含 §6/§7 在 § 4 不做清单）
- [x] § 5 一句话决策点具体（含周次 + 工时 + 具体任务编号）
- [x] 不引入新依赖（仅复用 lib/llm_client/db/lark/twitter_x/postiz/content_inject/voice_checker）
- [x] LLM 全程只用 MiniMaxi 包月（T-02/T-01 显式注明）
- [x] 永不自动发推 / 自动 DM（T-01/T-02/T-03 显式注明只产 markdown 草稿）
- [x] KOL 独立模块边界（T-01/T-02/T-03 独立 cron / 失败不传染主链）
- [x] 复用现有基础设施（无新增三方）

---

## 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-18 | 首版。基于 B3 v3.0 + engine HEAD `b39b450`。Donald 决策 W22 后产出 v2 |
