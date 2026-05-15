# YouTube 元数据规约 · Cowork ↔ engine 接触面

> **谁该读这个**:
> - Donald — 知道 Cowork 起草视频时多写一个 `yt_metadata.yaml` 文件就能完全控制 YT 上传内容
> - Cowork `mpt-video` / `shorts` skill 维护者 — 把 yaml 输出加进 skill 产物清单
> - 兼职女生 — 终审时知道在 `drafts/<piece_id>/` 看哪个 yaml
>
> **真相源代码**:[`lib/yt_metadata.py`](../lib/yt_metadata.py) · [`config/prompts/yt_metadata.txt`](../config/prompts/yt_metadata.txt)
> **接入位置**:[`jobs/schedule_planner.py`](../jobs/schedule_planner.py)(yt_shorts 平台发布前调 `load_or_derive`)
>
> 最后更新:**2026-05-15** · T14 完成时

---

## 0 · 为什么需要这个文件

YouTube Data API 上传必填:title / description / privacy / category / madeForKids 标记。Postiz 转发我们的 `create_post()` 时,要把这些填进 `settings: {...}` 字段才不会被 YT 拒。

`xthread_final.md` / `linkedin_post.md` 都是「单一文本字段就够发」,**只有 YouTube 例外** —— 它需要分开的 title + description + tags。

---

## 1 · 三级 fallback 链(你不用写代码就能跑)

engine 的 [`lib.yt_metadata.load_or_derive`](../lib/yt_metadata.py) 按这个顺序解析每条 piece 的 YT 元数据:

```
1. drafts/<piece_id>/yt_metadata.yaml      ← Cowork 起草时手写(推荐)
              ↓ 不存在 OR 校验失败
2. engine LLM 用 selection_card + shorts_60s.md + utm_links.json 派生
   → drafts/<piece_id>/yt_metadata_auto.yaml   ← Donald 终审能看
              ↓ LLM 全 provider 都挂
3. 硬模板 fallback(script 第一行 + 默认 footer + selection_card tag)
   → drafts/<piece_id>/yt_metadata_fallback.yaml   ← P2 Lark 告警 + Donald 看到弱兜底
```

**最坏情况都不会让 publishing 挂** — 总有元数据上去。但 tier 1 是最快、最有把控感的路径。

---

## 2 · Cowork 起草时这么写 yt_metadata.yaml(tier 1)

落到 `D:\Taskon\marketing\engine\runtime\drafts\<piece_id>\yt_metadata.yaml`,跟 `xthread_final.md` 同目录。

### 2.1 · 完整字段示例

```yaml
# 必填
title: "47% Quest 预算被 Bot 吃 | Q1 数据真相"
description: |
  本月 TaskOn 全平台数据交叉验证发现:47% 的 claim 来自重复地址。
  Sybil 过滤后真实新增 -60%,但 D7 留存 +14×。

  🔗 拿完整 Q1 Benchmark PDF
  https://l.taskon.xyz/q1-bench-yt

  TaskOn · Web3 增长平台
  我们的视角:Quest 数据脏不是 bug,是行业现状的最大真相
privacy: public        # 三选一: public / unlisted / private

# 可选(给默认值就行)
tags:                  # 最多 8 个,英文小写,中划线连接(YT SEO 友好)
  - quest-anti-sybil
  - web3-marketing
  - crypto-growth
  - sybil-detection
  - taskon
category_id: 22        # 22 = People & Blogs(B2B 增长); 28 = Science & Tech
not_made_for_kids: true   # B2B 内容默认 true
thumbnail_path: thumb.jpg  # 可选;相对 drafts/<piece_id>/ 路径
```

### 2.2 · 硬限制(engine 会拒)

| 字段 | 硬限制 | 违反后果 |
|---|---|---|
| `title` 长度 | ≤ 95 字符(YT 上限 100,留 5 字头) | yaml 整个被拒,落到 LLM 派生 |
| `description` 长度 | ≤ 5000 字符 | 同上 |
| `privacy` 取值 | 只能 `public` / `unlisted` / `private` | 同上 |
| `tags` 数量 | ≤ 8 个 | 同上 |
| 14 禁词(title + description 任一处) | 全方位 / 革命性 / 颠覆 / 赋能 / 闭环 / 抓手 / 价值赋能 / 显著 / dive into / let's explore / 综上所述 / 在当今快速发展的 | 同上 |

被拒不会报错,**会静默回退到 tier 2(LLM 派生)**,日志里看 WARNING。Donald 可以在 `runtime/logs/` 或 Lark 群看到信号。

### 2.3 · Cowork prompt 模板 — 复制贴用

如果你想让 Cowork 一次性出 5 个 platform 草稿 + yt_metadata.yaml,在 `crypto-twitter-creator` 或 `shorts` skill 起草时加这一段提示词:

> 完稿后还要为 YouTube 上传准备元数据,落到 `drafts/<piece_id>/yt_metadata.yaml`,5 字段:
> - title: ≤ 95 字符,含一个数字或反共识词,品牌词「TaskOn」可加可不加
> - description: 多段,第一段 60-120 字 SERP 友好钩子;中段含 UTM 链接(从 utm_links.json 取 youtube.short_url);末段固定 TaskOn footer
> - privacy: 默认 public,选题卡 risk_level=high 才 unlisted
> - tags: 5-8 个英文小写中划线 SEO 词
> - category_id: 22(增长/B2B)或 28(技术)
>
> 14 禁词清单见 `config/voice_disabled_words.yaml`(全方位 / 颠覆 / 赋能 等),title + description 都不能用。

---

## 3 · 不写 yt_metadata.yaml 会怎样(tier 2 / 3)

### 3.1 · LLM 自动派生(tier 2)

engine 读:
- `drafts/<piece_id>/selection_card.yaml` 的 hook_type / narrative_anchor / target_persona
- `drafts/<piece_id>/shorts_60s.md` 全文
- `drafts/<piece_id>/utm_links.json` 里 youtube.short_url

调 MiniMaxi M2.7-highspeed(失败 fallback Anthropic Opus 4.7)出一份 yaml,**写到 `yt_metadata_auto.yaml`**。

文件名带 `_auto` 后缀,Donald 一眼分辨。schedule_planner cron 周日 22:00 跑完后,Donald 周一上午终审时可以瞄一眼,觉得 LLM 写得不好就直接覆盖一份 `yt_metadata.yaml`(无后缀)再重跑。

**LLM 派生效果(本会话实测,2026W19-thread01 占位脚本)**:

```
INFO  yt_metadata LLM-derived: title='47% Bot 假用户:Perps DEX 增长真相' tags=7
```

LLM 能从极短的占位脚本派生出像样的中文 SEO title + 7 个 tag,真实稿子效果只会更好。

### 3.2 · 硬模板兜底(tier 3)

LLM 三家 provider 全挂:

- title = `shorts_60s.md` 第一行去 timecode → 加 ` | TaskOn` 后缀
- description = 脚本全文 + UTM 链 + 固定 TaskOn footer
- tags = `selection_card.hook_type` + `narrative_anchor` + `web3` + `taskon`
- privacy = `public`
- category_id = `22`

写到 `yt_metadata_fallback.yaml` + **P2 Lark 告警**:

```
P2 · yt_metadata: fell back to hard template for 2026W19-thread01
     (LLM unavailable or output rejected)
```

Donald 看到这条告警 = 当周 YT 元数据是「能发但不漂亮」状态,可决定:① 不管,先发再说;② 手写 `yt_metadata.yaml` 覆盖;③ 等 LLM 恢复后跑 `schedule_planner` 重生成。

---

## 4 · 校验和测试

5 个 pytest 用例覆盖三级 fallback + 边界:

```bash
docker compose exec engine python -m pytest tests/test_yt_metadata.py -v
```

| 用例 | 验证 |
|---|---|
| `test_cowork_yaml_used_as_is` | tier 1,不写任何审计 yaml |
| `test_llm_derives_and_persists_auto_yaml` | tier 2,写 `_auto.yaml`,Postiz settings shape 正确 |
| `test_llm_failure_engages_hard_fallback_and_alerts` | tier 3,P2 告警,fallback 内容合理 |
| `test_invalid_cowork_yaml_falls_through_to_llm` | Cowork 写了脏 yaml(含禁词)→ 优雅退到 tier 2 |
| `test_llm_banned_phrase_output_rejected` | LLM 也写脏 → 退到 tier 3 |

---

## 5 · schedule_planner 怎么用它

`jobs/schedule_planner.py` 在 plan 到 yt_shorts 平台时:

```python
if plat in ("yt_shorts", "yt_long"):
    yt_meta = load_or_derive(piece_id, drafts_dir / piece_id)
    extra_settings = yt_meta.to_postiz_settings()
    # → {title, description, type, tags, category, notMadeForKids}

postiz.create_post(
    integration_id=...,
    content=plan["content"],         # = shorts_60s.md 全文
    scheduled_at=plan["scheduled_at"],
    extra_settings=extra_settings,   # ← 这里塞进 YT 必填字段
)
```

Postiz 收到后转给 YouTube Data API,字段映射:
- `settings.title` → YT video title
- `settings.description` → YT video description
- `settings.type` → YT privacyStatus
- `settings.tags` → YT tags array
- `settings.category` → YT categoryId
- `settings.notMadeForKids` → YT madeForKids 反向

---

## 6 · 跟其他元数据文件的关系

`drafts/<piece_id>/` 完整产物清单(adapter_orchestrator + utm_generator + Cowork 共同产出):

```
2026W19-thread01/
├── selection_card.yaml          ← Donald 选题时 Cowork 写
├── xthread_final.md             ← Cowork crypto-twitter-creator 写
├── linkedin_post.md             ← engine adapter_orchestrator 改
├── carousel_10pages.md          ← 同上
├── medium_long.md               ← 同上
├── shorts_60s.md                ← 同上(MPT 用这个渲染视频)
├── shorts_60s.mp4               ← engine mpt_runner 渲染
├── yt_metadata.yaml             ← ★ Cowork 起草时多写一个(本文档主角)
├── voice_report.md              ← engine voice_checker 输出
└── utm_links.json               ← engine utm_generator 输出
```

只有 `yt_metadata.yaml` 是给 Cowork skill 维护者**新加**的输出。其他都已经在跑。

---

## 7 · 还要做的(本文档不覆盖)

- **LinkedIn Carousel PDF** —— `carousel_10pages.md` 还需要渲染成 PDF/图片集才能真发 LinkedIn Carousel。这是另一个开放工作,跟 `yt_metadata` 是同类问题。建议放到 T15 单独做。
- **YouTube thumbnail 自动生成** —— `yt_metadata.yaml.thumbnail_path` 字段已留好,但 engine 目前不主动生成缩略图。可以接 MPT 的视频首帧导出,也可以让兼职女生 Canva 手出。

---

## 8 · 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-05-15 | 首版 · T14 完成时;3-tier fallback + 5 pytest 全过 |
