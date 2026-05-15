# B1 全流程自动化自检 · 独立 Prompt

> **用法**：把下面整段（从 `--- BEGIN PROMPT ---` 到 `--- END PROMPT ---`）粘贴到一个**新的 Claude / Cowork 会话**里。
> **目的**：让另一个 Claude 实例独立审计 B1 工作流的自动化覆盖率，**对照** engine + MPT + Postiz + Listmonk 的真实部署状态，**输出**：
> 1. 一份"B1 每个动作能不能自动化"的逐行评估表
> 2. 所有人工卡点 / 断点写入 `todo.md` 一个新章节
> 3. 给出"补哪 3 个东西能让自动化率从 X% 提到 Y%"的优先级建议
> **运行环境**：Claude Code / Claude Desktop with Cowork（需要文件系统读权限）

---

```
--- BEGIN PROMPT ---

你是 TaskOn 内容营销引擎的**审计员**。你的唯一任务是判断"B1 内容生产全流程"在当前部署状态下能跑多自动化。

## 0 · 你的输出契约（先看）

完成下面所有步骤后，输出 3 份产物（按顺序，全部用中文）：

**产物 1**：一份 markdown 表格，列：
`B1 步骤 | 类型（生产/评审/数据/分发）| 当前状态（✅全自动 / 🟡半自动 / ❌人工） | 真正卡点 | 缺什么能升级一档`

**产物 2**：把表格中所有"❌人工" + "🟡半自动"的行**总结成 todo 章节**，按这个 markdown 块格式追加到 `D:\Taskon\marketing\engine\todo.md` 文件的末尾：

```
## G · B1 全流程自动化审计 · YYYY-MM-DD（自动审计生成）

### G.1 · 自动化覆盖率
- 全自动步骤：N / M = X%
- 半自动步骤：N / M = Y%
- 完全人工步骤：N / M = Z%

### G.2 · 不可让渡的人工卡点（永远人工，不要试图自动化）
1. [步骤名] — 卡点说明 — 为什么不该自动化
   ...

### G.3 · 可升级的半自动断点（补 X 就能全自动）
1. [步骤名] — 现状 — 升级路径 — 工时估算
   ...

### G.4 · 排序后的 Top 3 升级建议
1. 升级 [步骤] 从 🟡 → ✅，需要 [资源]，预计省人工 [N小时/周]
2. ...
3. ...
```

**产物 3**：一句话总结 "**目前 B1 自动化覆盖率 X%，Donald 实际还要花 N 小时/周人工介入**"。

---

## 1 · 强制前置阅读（按顺序，不允许跳读）

读完才能开始评估：

### 1.1 · 工作流真相源
- `D:\Taskon\marketing\00_内容营销引擎\全流程规划_v3\B1_内容生产全流程.md`
  - §1 · 6 路选题来源
  - §2 · 8 平台适配矩阵
  - §3 · 一鸡多吃 3 条防降权策略
  - §4 · UTM + CTA 模板
  - §5 · 评审 4 关 · 打回责任链
  - §6 · 1.5 人工时校验

### 1.2 · 工具栈职责边界
- `D:\Taskon\marketing\00_内容营销引擎\全流程规划_v3\C_工具栈职责边界.md`
  - 17 行决策矩阵（每行有"推荐方案 + 选择理由 + 排除理由"）
  - 6 大原则（驾驶舱 / Postiz 唯一入口 / MPT 不可替 / 本地脚本 / OpenClaw 移动入口 / 自写胶水）
  - 5 条不允许的偷懒决策

### 1.3 · engine 当前实现（已部署）
- `D:\Taskon\marketing\engine\README.md` — 部署 SOP
- `D:\Taskon\marketing\engine\docs\architecture.md` — 数据流图 + 15 表关系 + 跨模块调用
- `D:\Taskon\marketing\engine\docs\cowork_integration.md` — Cowork ↔ engine 集成图谱
- `D:\Taskon\marketing\engine\todo.md` — 已有 TODO 列表（你要追加 G 节）
- `D:\Taskon\marketing\engine\jobs\` 目录下所有 `.py` 文件名（用 ls 看一眼即可，**不要全读，太长**）

### 1.4 · 外部依赖部署状态（确认是否真的在跑）
- **MPT**（视频生产）：`docker ps | grep moneyprinter` 应该有 `moneyprinterturbo-webui:8501` + `moneyprinterturbo-api:8090`。如果有 → ✅已部署，可调
- **Postiz**（发布调度）：`docker ps | grep postiz` 看是否在跑。**如果没有 → 标为"未部署，需要 Donald 先部署"**
- **Listmonk**（Newsletter）：`docker ps | grep listmonk` 看是否在跑。Donald 已确认 `E:\AILife\listmonk` clone 了但**未部署** → 影响 B1 路 1 Newsletter 整段
- **engine**：`docker ps | grep taskon` 应有 `taskon-engine` + `taskon-ingestion`，都是 healthy

### 1.5 · LLM 通断
- 跑一条：`docker compose exec engine python -c "from lib.llm_client import llm; print(llm.complete('test','say one word'))"`
- 通 ✅ / 不通 ❌

---

## 2 · 评估方法（不允许偷懒）

### 2.1 · 每个 B1 步骤逐一过

把 B1 文档拆成最小可执行单位（粗略估 30-50 个步骤）：
- §1 6 路 = 6 个步骤
- §2 8 平台 × 起草 / 改写 / 校对 / 排程 = 32 个微步骤
- §3 3 策略 = 3 个步骤
- §4 UTM 生成 + 落地页路由 = ~3 个步骤
- §5 评审 4 关 = 4 个步骤
- §6 工时表里的每行 = ~12 个步骤

### 2.2 · 每个步骤打 3 个分

| 维度 | 取值 | 判定标准 |
|---|---|---|
| 当前状态 | ✅全自动 / 🟡半自动 / ❌人工 | engine + 已部署外部框架能跑多少 |
| 卡点类型 | 数据缺 / 服务未部署 / 凭据缺 / API 不存在 / 本质需人判断 / 其他 | 真正阻碍自动化的根因 |
| 升级可能 | 高 / 中 / 低 / 不应升级 | 投入工时能多少 ROI |

### 2.3 · 判定"全自动 ✅"的严格标准

**不允许把"理论可以"或"加 .env key 就行"算成✅**。判定 ✅ 必须同时满足：
- engine 的对应 module 真的写了
- 对应外部服务真的在本机/服务器跑着
- 所需 API key 真的填了
- 跑过至少 1 次没炸

任何 1 条不满足 → 🟡 半自动

### 2.4 · 判定"❌ 人工"的标准

只要满足任意 1 条：
- 需要 Donald 拍板 / 看数据后做判断（如选题决策、终审）
- 需要兼职女生人工感知（如客户痛点访谈、Fact-Check 找来源）
- 需要平台 UI 操作（如 Twitter Space、Messari 投稿、Canva 配图）
- engine 没写对应模块且按 C 文档应该是人做的（如 KOL Pre-Read DM）

### 2.5 · 关于"应该不应该自动化"的边界

**注意**：C 文档 §原则1（Cowork = 驾驶舱）和 B1 §6 已经明确**某些步骤永远应该人工**（Donald 终审、BD 客户访谈、Twitter Space）。这些步骤即使技术上能自动化，你也要**标注"不应升级"**——别给"用 AI 替代终审"这种烂建议。

---

## 3 · 输出格式样例（参考）

### 3.1 · 产物 1 表格样例

```markdown
| B1 步骤 | 类型 | 当前状态 | 真正卡点 | 升级路径 |
|---|---|---|---|---|
| §1 路 1 crypto-news-aggregator | 选题信号 | 🟡 半自动 | Cowork skill 在 Donald 桌面跑，不能 cron | 接 Dify 工作流（C 文档 §1 推荐 fallback）|
| §1 路 2 KOL 抓取 | 选题信号 | 🟡 半自动 | X_BEARER_TOKEN 未填 / Twikit pool 未配 | 填 X token（高 ROI）|
| §1 路 3 BD 客户痛点访谈 | 选题信号 | ❌ 人工 | 本质需要兼职女生人工感知 | **不应升级**（C §原则1）|
| §2 X Thread 起草 | 生产 | 🟡 半自动 | Cowork skill 需 Donald 触发 | 不应升级——Cowork 就是入口 |
| §2 多平台改写 | 生产 | ✅ 全自动 | — | engine adapter_orchestrator |
| §5 关 1 Voice Checker | 评审 | ✅ 全自动 | — | engine voice_checker |
| §5 关 4 Donald 终审 | 评审 | ❌ 人工 | 数据关 + 可操作关，B1 §5 红线 | **不应升级**（业务硬约束）|
| ... | ... | ... | ... | ... |
```

### 3.2 · 产物 2 todo.md 追加样例

```markdown
## G · B1 全流程自动化审计 · 2026-05-13（自动审计生成）

### G.1 · 自动化覆盖率
- 全自动步骤：12 / 47 = 25.5%
- 半自动步骤：21 / 47 = 44.7%
- 完全人工步骤：14 / 47 = 29.8%（其中 11 条是不应自动化的硬人工）

### G.2 · 不可让渡的人工卡点（永远人工）
1. **§1 路 3 BD 客户痛点访谈** — 需要兼职女生感知客户原话 — 让 AI 做会丢失行业感知（C §原则1 + B1 §1）
2. **§5 关 4 Donald 终审** — 数据关 + 可操作关 — 让 AI 改数字违反 PRD §11 红线
3. ...

### G.3 · 可升级的半自动断点（补 X 就能全自动）
1. **§1 路 2 KOL 抓取** — X token 未填 → kol_watch 全靠 Twikit 风险大 — 填 X_BEARER_TOKEN（5min）
2. **§1 路 1 crypto-news-aggregator** — Cowork skill 只在桌面 — 加 Dify 工作流副本走 cron（2-3 天）
3. ...

### G.4 · 排序后的 Top 3 升级建议
1. **填 LARK_WEBHOOK_URL**（5min）→ 现在告警进容器日志没人看，凌晨炸了不知道
2. **部署 Listmonk + 填 SES**（4h）→ 解锁 B1 §2 Newsletter 整段 + §5 关 1 邮件 voice check + B5 闭环
3. **TaskOn admin API 实现**（技术同事 2-3 天）→ 解锁 B1 update_btouch + B4 触点 CTR
```

### 3.3 · 产物 3 一句话总结样例

> **目前 B1 自动化覆盖率 25.5%（12/47），Donald 实际还要花 7.5h/周（B1 §6 锁死）+ 兼职女生 16h/周；其中 11 个硬人工不应自动化（按 C 红线）；填 3 个 key + 部署 Listmonk 可把覆盖率提到 ~50%（额外节省兼职女生 4h/周）**

---

## 4 · 你的工作纪律（不可让渡）

1. ❌ **不要假定 .env 都填了** — 我会让你看 `docker compose logs engine` 找 `WARNING: xxx empty`
2. ❌ **不要假定外部服务都部署了** — 实跑 `docker ps` 看
3. ❌ **不要把"理论上能自动化"算成 ✅**
4. ❌ **不要建议"用 AI 替代 Donald 终审"或类似偷懒方案** — 违反 C 红线
5. ✅ **每条结论要给出"为什么"** — 引用 B1 / C 具体节号
6. ✅ **写 todo.md G 节追加，不是覆盖** — 用 Edit 追加；如果 G 节已存在就编号 G.5 / G.6 继续往后追加
7. ✅ **产物 3 那句话总结必须含具体数字** — 百分比 + 工时

---

## 5 · 开始

按 §1 顺序读文档 → 按 §2 评估 → 输出 §3 三份产物。
读完所有强制前置阅读后，先输出**一段话**（≤100 字）总结"你看到的部署状态"，然后再开始评估。
评估完输出三份产物，三份产物之间用 `---` 分隔。
不要问 "需要我帮你做什么吗" 这种废话——直接干完。

--- END PROMPT ---
```

---

## 用法说明（不要复制下面这段进新会话）

1. 打开一个新的 Claude / Cowork 会话
2. 粘贴上面 `--- BEGIN PROMPT ---` 到 `--- END PROMPT ---` 之间的内容
3. 等它输出 3 份产物
4. todo.md 会被自动追加 G 节
5. **如果它给你"理论可以"的偷懒答案，回复"按 §2.3 重判"** 让它重做

**期望耗时**：审计员需要读 ~5 份文档（B1 + C + README + architecture + cowork_integration），跑 ~3 个 docker 命令检查部署状态。预计 8-12 分钟产出 3 份产物。

**审计员的产出会更新 todo.md**——如果你不想它写文件，把"产物 2"那段指令删掉，让它直接把内容贴在对话里。
