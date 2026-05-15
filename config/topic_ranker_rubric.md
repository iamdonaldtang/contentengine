# Topic Ranker · 5 维评分 Rubric（v1）

> 评分对象：内容选题候选 (`candidates` 表一行)。每维 **1-10 整数**，越高越好。
> 评分主体：LLM via `lib.llm_client.llm.complete_json()`，由 `jobs/topic_ranker.py` 调度。
> 上下文：TaskOn 内容营销 · 3 大支柱 + 红线 + 弹药库 8 大矛盾（详见 `D:\Taskon\marketing\CLAUDE.md`）。

---

## 1 · 受众契合度 (audience_fit)

候选话题对 TaskOn 一级/二级受众（**Crypto CMO / Growth Lead / CEO**）的相关性。

- **10**：CMO/CEO 在 Slack 中会主动转发；命中 Q2/Q3/Q4 季度叙事锚点；`target_persona` 字段明确。
- **7-8**：相关但非关键决策时机；命中三级受众（KOL / 重度玩家）。
- **4-6**：泛 Web3 话题；需要 1 层解释才能让目标受众产生兴趣。
- **1-3**：偏离目标受众（如 NFT 玩家 / 散户 / 纯技术开发者）。

## 2 · 数据扎实度 (data_solidity)

`data_sources_hint` 字段或正文提到的数据点是否**可查可验**。

- **10**：≥2 个独立数据源（链上 + 平台 + 第三方），均可附 URL。
- **7-8**：1 个主数据源 + 1 个佐证；可信第三方（Dune / DefiLlama / Messari）。
- **4-6**：仅自家平台数据 / 单一来源；需脱敏聚合。
- **1-3**：无数据 / 仅"业内观察" / 估算无法溯源。**红线**：无数据空议论直接判 1-2 分。

## 3 · 钩子强度 (hook_strength)

前 30 字（推文首句 / 标题）是否能让目标受众**0.5 秒内停下来**。

- **10**：反直觉数字 + 行业熟悉名词的颠覆（如"47% Quest 预算被 bot 吃"）。
- **7-8**：具体数字 / 具体客户案例 / 具体时间窗。
- **4-6**：常规疑问句 / 抽象观点 / 缺数字。
- **1-3**：模糊形容词（"显著""大量"）/ FOMO 词 / AI 味套话。**红线**：触发反 AI 味清单直接判 1-2 分。

## 4 · STEPPS 传播性 (stepps)

按 Jonah Berger 的 6 维传播力打分（取平均后映射 1-10）：Social Currency（社交货币）/ Triggers（触发点）/ Emotion（情绪）/ Public（可见性）/ Practical Value（实用价值）/ Stories（故事性）。

- **10**：≥4 维高分；社交转发动机明确（"转给我老板看"）。
- **7-8**：2-3 维高分；实用价值 + 1 个情绪点。
- **4-6**：仅 1 维突出（多为单纯实用 Playbook）。
- **1-3**：低传播性 / 自说自话。

## 5 · 品牌一致性 (brand_consistency)

是否命中 TaskOn 三大支柱 **（① 行业真相 ② 增长方法论 ③ TaskOn 视角）** + 是否踩 **7 条红线**。

- **10**：明确命中 1 支柱 + 至少 1 条建设性张力（指问题 + 留希望）+ Voice 克制。
- **7-8**：命中支柱但缺建设性张力 / 偶有 vendor 自吹苗头。
- **4-6**：擦边球；与品牌音不冲突但无加成。
- **1-3**：踩 ≥1 条红线（为黑而黑 / 点名客户 / "TaskOn 能解决一切" / FOMO / Benchmark Report 类）。**踩红线直接 1 分**。

---

## 评分输出契约

LLM 必须返回 JSON：

```json
{
  "audience_fit": 8,
  "data_solidity": 7,
  "hook_strength": 9,
  "stepps": 6,
  "brand_consistency": 8,
  "rationale": "一句话说明此打分的关键依据（≤80 字）"
}
```

总分 `ranker_score = audience_fit + data_solidity + hook_strength + stepps + brand_consistency`（满分 50）。
