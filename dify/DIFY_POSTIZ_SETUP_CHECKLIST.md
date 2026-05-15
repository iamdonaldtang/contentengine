# Dify + Postiz 接入 Checklist · 待 Donald 执行

> **目标**：让 Claude PIPELINE 流水线能端到端走通："草稿 → Dify 审核 → Postiz 发布"。
> **更新日期**：2026-05-12
> **配套文档**：`D:\TaskOn\marketing\PIPELINE.md § 8` 是接入规范全文。

---

## A. Dify 端（在 content.taskon.xyz）

### A.1 在现有"内容生产工作流"上加节点

按 PIPELINE.md § 8.3 表，从现有工作流开始增改：

- [ ] **Step 1 · Webhook Trigger**
  - 把工作流入口改成 Webhook Trigger（如果现在是 Chat / Workflow 类型）
  - 记下生成的回调 URL（形如 `https://content.taskon.xyz/v1/workflows/<id>/run`）
  - 记下 API Key（在 Dify 工作流编辑页右上角"API 访问"获取）
  - 入参 schema：
    ```
    platform: string (twitter | linkedin | threads | bluesky | telegram | medium)
    draft_md: string (markdown 草稿正文)
    audience: string (CMO | CEO | KOL | Web2-cross)
    contradiction: number (1-8)
    hook: string
    score: number
    scheduled_at_iso: string (ISO 8601 含时区)
    dry_run: boolean (默认 true)
    ```

- [ ] **Step 2 · LLM Node 平台格式化**（可选；如果 Claude 端已经按平台格式化好就跳过）
  - 输入：`platform` + `draft_md`
  - 输出：`formatted_draft`（Twitter 长 thread 拆成数组；LinkedIn 加分段空行等）

- [ ] **Step 3 · Human Input Node** ★ 这是要新加的核心节点
  - 入参：`platform / formatted_draft / hook / score / scheduled_at_iso`
  - 三个 Decision Buttons：
    - **Approve** —— 走发布分支
    - **Edit** —— 暂存到 Dify 端继续编辑（不发布也不拒绝）
    - **Reject** + 让 Donald 填一个原因（自由文本）
  - Delivery method：选 **Email** 推到 `donald@taskon.xyz`，或者飞书机器人 webhook
  - Timeout：4 小时 → 走超时分支（默认拒绝）

- [ ] **Step 4 · If/Else 按按钮分流**

- [ ] **Step 5 · HTTP Request Node**（Approve 分支）
  - URL: `<POSTIZ_BASE_URL>/api/public/v1/posts`（你 Postiz 的本地地址）
  - Method: POST
  - Headers:
    ```
    Authorization: Bearer <POSTIZ_API_KEY>
    Content-Type: application/json
    ```
  - Body（用 Dify 变量填）：
    ```json
    {
      "type": "schedule",
      "date": "{{ scheduled_at_iso }}",
      "shortLink": false,
      "tags": ["{{ contradiction }}", "{{ audience }}"],
      "posts": [
        {
          "integration": "{{ postiz_integration_id_lookup_by_platform }}",
          "value": [
            { "content": "{{ formatted_draft }}", "image": [] }
          ],
          "settings": {}
        }
      ]
    }
    ```
  - **关键**：`integration` ID 要按 `platform` 字段查表，建议在 Dify 工作流里建一个变量 map：
    ```
    twitter   → "<postiz integration id 1>"
    linkedin  → "<postiz integration id 2>"
    threads   → "<postiz integration id 3>"
    bluesky   → "<postiz integration id 4>"
    telegram  → "<postiz integration id 5>"
    ```
  - **首次接通强制 dry_run**：在 body 顶层加 `"dryRun": true` 字段（如 Postiz 支持；否则用 `type: "draft"` 而非 `"schedule"` 也能起到不真发的效果）

- [ ] **Step 6 · 通知 + 日志**（Reject 分支 / 超时分支）
  - 把拒因或超时事实推飞书 / Telegram 给 Donald
  - 同时调一个 HTTP 把拒因 POST 回 Claude 这边的 logs 目录（可选；或者 Donald 手动告知）

### A.2 在 Postiz 端配账号

- [ ] 在 Postiz UI（本地后台）连接 Twitter / LinkedIn / Threads / Bluesky / Telegram 等账号
- [ ] 每个连接后会得到一个 `integration ID`，记下 5-6 个 ID 填到 Dify Step 5 的变量 map 里
- [ ] 在 Postiz 生成一个 API Key（设置 → API Access），填到 Dify Step 5 的 Authorization header

---

## B. Claude 端（在 marketing 目录）

### B.1 配 .env（Donald 提供 4 个参数后我来填）

- [ ] 把以下 4 个值告诉我，我会写到 `D:\TaskOn\marketing\.env`（gitignore）：
  ```
  DIFY_BASE_URL=https://content.taskon.xyz
  DIFY_WORKFLOW_ID=<from Dify UI>
  DIFY_API_KEY=<from Dify UI>
  POSTIZ_BASE_URL=<your local Postiz domain>
  ```
- POSTIZ 的 API key 不需要给 Claude，Dify 端用就行

### B.2 测试脚本（B.1 完成后我创建）

- [ ] 我会在 `D:\TaskOn\marketing\scripts\test_dify_pipeline.sh` 写一个 curl 测试脚本
- [ ] 先发一条 dummy 草稿，全链路 dry_run，确认 Dify Human Input 触达 Donald 邮箱

---

## C. 接通后的第一周测试节奏

| 天 | 测试 | 验收 |
|---|---|---|
| Day 1 | dry_run dummy 草稿 → 收到 Dify 审核邮件 | 邮件来；点 Approve 后 Postiz 端有草稿状态 |
| Day 2 | Approve 路径真发到 Twitter（仍只 1 条）| 推送到 Twitter；Postiz 后台看到发布记录 |
| Day 3 | Reject 路径 + 写拒因 | 飞书 / 邮件收到拒因；Claude 这边能看到 |
| Day 4 | Edit 路径 + Donald 在 Dify 端改后再发 | 改后版本最终发布 |
| Day 5 | Timeout 路径（4h 不响应）| 自动 Reject + 通知 |
| Day 6 | 一天发 3 条不同平台 Twitter+LinkedIn+Threads | 三个平台都收到 |
| Day 7 | 触发 `weekly-content-review` 看上周数据回流 | 周复盘 markdown 出现 + perf_*.md 创建 |

---

## D. 现在阻塞的 4 个参数

请你回我：

1. **Dify workflow ID**（从 Dify UI 右上角"API 访问"复制；如果旧工作流是 Chat 类型，建议新建一个 Workflow 类型，参数 schema 见 A.1）
2. **Dify API Key**（同上 UI 获取）
3. **Postiz 本地部署的域名**（如 `https://postiz.taskon-internal.local` 或类似）
4. **Postiz 已连接哪些社交账号**（告诉我账号名 + integration ID 5-6 个，我会配进 Dify 变量 map）

回完这 4 个，我立刻写 `.env` + 测试脚本，并把 PIPELINE.md § 8.2 的 `<DIFY_WORKFLOW_ID>` 等占位符填实。

---

## E. 一个我建议你考虑的小决策

**Email 还是飞书做 Human Input 通道？**

| 方式 | 优 | 劣 |
|---|---|---|
| Email | Dify 原生支持，配置最简单 | 邮件容易漏；移动端体验一般 |
| 飞书机器人 webhook | 移动端推送强；可加丰富按钮 | 需要在 Dify HTTP Node 自定义；不能用 Human Input 原生分支，要绕一下 |
| Telegram bot | 你日常已用 | 同飞书，需要自定义 |

**我推荐**：第一周用 Email 跑通（最快），跑稳后第二周升级到飞书 / Telegram。
