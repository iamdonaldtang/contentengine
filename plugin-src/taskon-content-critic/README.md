# TaskOn Content Critic

**版本：** v0.1.0
**作者：** Donald · TaskOn
**日期：** 2026-04-28

> TaskOn 外发内容发布前的辣评质检 plugin。

---

## 这是什么

一个 Cowork plugin，包含 1 个 skill（`content-critic`）。

当你提交一条 Twitter / 长文 / Newsletter 草稿要审稿时，会触发辣评模式：基于第一性原理 5 问 + 10 个评分维度（满分 50），输出 7 段结构化评审。

## 触发关键词

- "评审这条" / "这篇怎么样" / "内容打分"
- "check 这篇推" / "我这篇能发吗" / "帮我挑刺"
- "Review this", "Critique this post", "Score this content"

## 评估维度（满分 50）

| 维度 | 检测什么 |
|---|---|
| D1 受众精准度 | 是否让目标 persona 自我点名 |
| D2 价值密度 | 删了这篇读者损失什么 |
| D3 Hook 强度 | 第一句决定划走还是继续 |
| D4 完读率结构 | 节奏 / 句长波动（识 AI 味） |
| D5 故事性 | 能不能在饭局口述 |
| D6 STEPPS 传播性 | Social Currency / Triggers / Emotion / Public / Practical / Stories |
| D7 CTA 转化设计 | 单一动作 / 具体文案 / 情绪峰值 |
| D8 标题 4U | Useful / Urgent / Unique / Ultra-specific |
| D9 反 AI 味 | 8 个反样板检测 |
| D10 Web3 行业语境 | 术语准 / 项目名 / 不自吹 |

## 评审输出（7 段）

1. 一句话死穴
2. 总分 + 红黄绿灯
3. 十维度雷达表
4. 三个最该改的地方（before/after）
5. 标题候选 3 条
6. CTA 重写
7. 一句话毒舌总结

## 适用 / 不适用

✅ Twitter 短推 · Twitter Thread · Medium 长文 · Newsletter · Blog Post
❌ BD 话术 · 内部纪要 · SOP · 合规公告 · 视频脚本（v2 再加）
❌ 创意头脑风暴阶段（早期想法不要辣评）

## 安装

下载 `taskon-content-critic.plugin` 文件 → Cowork 中点击 plugin 卡片 → 一键安装。

## 升级路径

1. 修改 plugin 源目录下的 SKILL.md
2. `plugin.json` 里 bump version
3. 用 `cowork-plugin-management:create-cowork-plugin` skill 重新打包
4. 卸载旧版、装新版

## 配套资源（不在 plugin 内，作为团队 SOP 文档）

- `D:\TaskOn\marketing\content-review-framework.md` — 完整框架理论
- `D:\TaskOn\marketing\content-review-prompt.md` — 独立可复用提示词（任意 AI 适用）

## 已知限制

- 仅辣评模式，coach 模式 v2 待开发
- 评分权重未经真实数据校准，需积累 30 篇样本后调整
- 不评视频脚本 / BD 话术 / 内部内容
