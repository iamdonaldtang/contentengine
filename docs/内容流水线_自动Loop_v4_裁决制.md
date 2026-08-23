# 内容流水线 · 自动 Loop v4（裁决制）

> 立于 2026-07-06 ｜ 决策人：Donald（四项拍板见 §3.0）｜ 赛道：内容#taskon ｜ mode: growthos
> **一句话**：机器每天把 1 条选题产到「可发状态」，Donald 每天只在裁决台打一个勾，其余全自动。
> 本文件是 Loop 的**宪法**——三个定时任务（briefing / produce / harvest）与周复盘的 prompt 均引用本文件，冲突以本文件为准。
> 上游：`全流程操作手册_v3.md` v3.2（13 步工程层）+ `HTTP-first_方案A_部署与13步映射_v1.md`（命令层）+ `PIPELINE.md`（编排层 · 含 6-25 动机双轴）。

---

## 1 · 流水线最新版本地图（截至 2026-07-06 · 本次审计确认）

| 层 | 现行版本 | 载体 | 自动化状态（审计时） |
|---|---|---|---|
| **选题层**（模式 A） | PIPELINE.md + GrowthOS 改造 v2（6-25 动机双轴 A/B/C/D × 五局 · 品牌:现金 6:4） | `daily-content-briefing` 每日 09:00 | ✅ 全自动，质量稳定（简报已带四字段+破零候选标注） |
| **拍板/裁决** | 红线 4：必须 Donald | 无固定触点 → **本方案改为裁决台打勾** | ❌ 曾是最大断点：张力选题连指 4 天零产出 |
| **生产层**（模式 C · 13 步之 1-11） | 手册 v3.2 + HTTP-first 方案A（tk.sh / admin API）+ X 种子 config 治理（xthread/xshort_seed） | Cowork skills + engine jobs（adapter / voice / critic / mpt / utm） | ❌ 纯手动，W20 后基本停摆 → **本方案改 content-loop-produce 每日自动** |
| **发布层**（步 12） | schedule_planner 错峰 → Postiz 真发；X 手发（红线 2）；急发 `POST /admin/run_publish` | engine + Postiz（LinkedIn/YT/Medium；fork 含 local-twitter 未启用） | ❌ perf 台账 W20-W22 连续 0 发布后再无记录 → **本方案改 content-loop-harvest 收割自动发（非 X）** |
| **回流层**（步 13 + 模式 B） | metrics_collector / attribution_engine / weekly_reporter（引擎机 cron 全自动）+ weekly-content-review（Cowork 侧） | 引擎机 cron ✅ · Cowork 周复盘 | ❌ weekly-content-review 自 ~6-08 disabled；perf 台账停在 6-01 → **prompt 已升级并待重启** |
| **状态机** | CONTEXT / STATE / LEARNINGS 三件套 + 路由表收工标准 | `engine\STATE.md` 等 | ❌ STATE/LEARNINGS 停在 5-15（7 周失守）→ **produce/harvest/weekly 三处强制回写** |
| **基建控制面** | HTTP-first admin API（Bearer + 白名单 job）+ smoke_httpfirst.sh 四层防线 | `ingest.taskon.xyz/admin/*` | ⚠️ 审计当日 ingest + l.taskon.xyz 双 502（引擎机 cloudflared 或整机 down）；watchdog `httpfirst-daily-smoke` 自 6-11 disabled |

**已废弃路径（文档里还在、执行上已死，见 §6 清理）**：PIPELINE.md §1模式C-步5 / §3 / §9 的 **Dify（content.taskon.xyz）+ Human Input 邮件审核**链路——被 v3 引擎直连 + 本裁决台取代，Dify 不在现役服务清单。

---

## 2 · 审计结论：六个断点（为什么之前 Loop 转不起来）

| # | 断点 | 证据 | 本方案对策 |
|---|---|---|---|
| A | **拍板→生产之间没有自动衔接**：简报天天出，无人产稿 | 7/1-7/6 张力选题连指 4 天零产出；简报沦为日抛库存 | produce 工位：拍板从「产前」移到「发前」，机器先产 Donald 后裁（红线 4 语义升级，见 §4） |
| B | **发布出口依赖 Donald 手动**，且无固定裁决触点 | W20-W22 连续 0 publish；6-05 checklist「手贴 ≤5min」也没执行 | 裁决台打勾 + 15:00 收割自动真发（非 X）；X 手发素材直接喂到 checklist 置顶 |
| C | **回流断链**：weekly review 停摆 → perf/LEARNINGS/STATE 全冻结，自进化归零 | perf 停在 6-01；STATE/LEARNINGS 停在 5-15 | weekly-content-review prompt 升级（含 Loop 体检 + STATE/LEARNINGS 强制重写）待重启 |
| D | **watchdog 关了**：控制面坏了没人知道 | httpfirst-daily-smoke 自 6-11 disabled；审计当日双 502 无告警 | 重启 smoke 任务；harvest 遇 502 自动降级 + checklist 置顶修复指引 |
| E | **状态机失守**：收工动作没人执行，跨会话记忆断 | STATE 7 周未更新，与路由表 §4 收工标准冲突 | 三个任务 prompt 内置强制收工动作（STATE + log + LEARNINGS append） |
| F | **文档漂移**：PIPELINE.md 残留 Dify 死路径；CONTEXT 短指令歧义（LEARNINGS L2）；根法 memory 文件在当前记忆空间缺失 | 本次审计逐文件比对 | §6 清理清单（Dify 段落标废、字典补裁决口令）；根法缺失已报 Donald（6-05 待决策方案 B 是解法） |

---

## 3 · Loop v4 设计（裁决制）

### 3.0 Donald 四项拍板（2026-07-06）

1. 裁决触点 = **文件打勾 + 定时收割**（不需要每天开会话）
2. 生产范围 = **每日 Top-1 产到可发**（简报标注的破零候选优先）
3. 发布出口 = **非 X 平台自动真发（红线 2 保留），X thread 手发**
4. 施工深度 = 方案 + 当日施工，任务先 disabled 待过目；引擎 502 修复后点火

### 3.1 每日节拍（全部北京时间）

```
09:00  daily-content-briefing     选题 3 条 → logs（已有 · enabled）
09:49  content-loop-produce       Top-1 产到可发：起稿→4平台改写→voice→critic≥35→配源→UTM→dry-run
                                  → 写「裁决台」块（含 X 全文供手发）
任意    Donald 裁决（唯一人工动作）打开 D:\TaskOn\marketing\裁决台.md，三选一打勾：
                                  ✅ 发 ｜ ✏️ 改（写一行意见）｜ ❌ 砍
15:07  content-loop-harvest       读勾执行：✅→tk_schedule 错峰真发（LinkedIn/YT/Medium）+ X 素材喂 checklist
                                  ✏️→revision_note + 明早优先返工 ｜ ❌→tk_kill 级联删
                                  未打勾→pending；连续 2 天未裁 = 自动砍（防淤积）
夜间    引擎机 cron               metrics 5 源采集 / 归因 / 备份（原有 · 不动）
周一10:00 weekly-content-review   复盘+排期+Loop体检+重写 STATE/LEARNINGS（待重启）
```

**Donald 的全部日常投入**：打 1 个勾（约 2 分钟）+ 想发 X 时把 checklist 里的现成全文贴出去（约 5 分钟）。

### 3.2 裁决台契约（`D:\TaskOn\marketing\裁决台.md`）

- 单一滚动文件，最新块在最上；终态块由 harvest 移入 `裁决台_archive.md`
- 每块必含：piece_id / 选题一句话 + [动机][局][轨] / critic 分 / voice / 数字源状态 / 稿件路径 / **X thread 全文 + 置顶评论** / 三个勾 / engine 状态
- 打勾规则：三选一；✏️ 必须在「修改意见:」后写至少一句，否则按未裁决处理
- 超时规则：连续 2 天未裁 → harvest 自动砍并留痕（宁可砍错不淤积；砍掉的选题弹药还在，可再来）

### 3.3 降级与兜底

| 故障 | 行为 |
|---|---|
| 引擎公网 502 / 不可达 | produce 降级只产本地稿（marketing\drafts\），裁决台标「仅本地稿」；harvest 对 ✅ 件标 blocked_by_engine + checklist 置顶修复三步（开机 → Start-Service cloudflared → smoke 全绿） |
| critic 两轮 &lt;35 | 换角度一次，仍不过 = 今日不产，裁决台记原因（质量 &gt; 破零） |
| 选题全部标「须 Donald 校准」 | 不产稿，裁决台列出待校准问题 |
| skill 不可用 | 按 PIPELINE §6：报错给 Donald 选，不硬扛 |
| X API 配额 | 与本 Loop 无关（选题走 news-aggregator 合池；X 发布是手发） |

### 3.4 红线（继承 + 一处语义升级）

1. 数据关失败 = 砍，绝不让 AI 改数字（编造 URL 同罪）
2. **engine 永不自动发 X / 自动 DM**（Postiz local-twitter 保持不启用；将来要改需 Donald 明示修订本条）
3. 非 X 平台文案含 `{{CTA_URL}}`；X 正文零外链，CTA 进置顶评论
4. **拍板必须 Donald —— 语义升级（2026-07-06 Donald 拍板）**：由「产前挑选题」改为「发前裁决稿」。机器可自主选 Top-1 试制（试制无成本），但**任何内容公开发布前必须有 Donald 的 ✅**。超时自动砍 = 默认不发布，方向永远收敛到「不发」，安全侧不变
5. 主笔记本不跑 docker / cloudflared；跨网络只走 HTTP-first（沙箱连不到 Tailscale/SSH）
6. Benchmark Report 类不出（L0）；现金轨内容不进官网/署名长文门面（GrowthOS v2 自保线）

---

## 4 · 点火 checklist（Donald 按顺序做）

- [ ] **P0 修基建**（今天，~10 分钟）：引擎机开机/检查 → `Start-Service cloudflared` → 引擎机跑 `bash scripts/smoke_httpfirst.sh` 13 项全绿 → 主笔记本 `curl https://ingest.taskon.xyz/health` 返回 ok
- [ ] 过目三个任务 prompt（Scheduled 侧栏）：`content-loop-produce` / `content-loop-harvest` / `weekly-content-review`
- [ ] Enable 四个任务：上述三个 + `httpfirst-daily-smoke`（watchdog，本次 502 就是它 disabled 才没被发现）
- [ ] 对 produce / harvest 各点一次「Run now」预授权工具（否则首个自动运行会卡权限弹窗）；produce 首跑会拿今天简报的选题 1（Saylor 数字能源 · 30min 短推）试制
- [ ] 首件走通后：在裁决台打第一个勾 → 15:07 看 harvest 是否真发 LinkedIn/YT → X 全文从 checklist 贴出（破零 W20 以来第一发）
- [ ] （可选 P1）恢复 `daily-kol-replies`；处理矛盾 3 解冻裁决（周一复盘会提）

## 5 · 本次同步施工记录（2026-07-06 · Cowork）

- 新建：本文件、`裁决台.md`、`裁决台_archive.md`、定时任务 ×2（disabled）
- 升级：`weekly-content-review` prompt（Loop 体检 + STATE/LEARNINGS 强制重写，保持 disabled）
- 重写：`engine\STATE.md`（7 周失守 → 当前真相）；append `LEARNINGS.md` 新教训 ×3
- 修正：`PIPELINE.md` Dify 段标废 + 分发默认路径改 HTTP-first；`CONTEXT.md` 短指令字典补「裁决」口令（顺带修 L2 歧义）
- 登记：`COWORK_AUTOMATION_CHECKLIST.md` 条目 A9/A10；`donald_master_memory.md` §2/§6；`daily_checklist.md` 今日块；`logs\2026-07-06.md`

## 6 · 文档漂移清理清单

- [x] PIPELINE.md §1模式C-步5 分发默认路径：Dify → HTTP-first admin（Dify 段标 DEPRECATED 保留存档）
- [x] CONTEXT.md 字典加「裁决」+「跑 W{N} 周池」（修 LEARNINGS L2）
- [ ] 根法 memory（feedback_root_principles.md）在当前记忆空间缺失 → 建议执行 6-05 待决策「方案 B：根法精简版嵌入 CLAUDE.md」（零依赖，Donald 拍板后做）
- [ ] daily-content-briefing SKILL.md 与 PIPELINE 模式 A 有轻微滞后（无动机轴字样但实跑已带）——运行正确，P2 再对齐，不动 enabled 任务

## 7 · 变更记录

| 日期 | 变更 |
|---|---|
| 2026-07-06 | v4 立此存照：审计六断点 + 裁决制 Loop 设计 + 当日施工。四项拍板：文件打勾裁决 / Top-1 产到可发 / 非 X 自动发 / 当日施工任务先 disabled |
