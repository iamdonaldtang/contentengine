# Dify 端 Wave 1 节点搭建补充

> **作用**：Wave 1 落地后，Claude 端会传错峰序列 + 多平台 payload 给 Dify。Dify 工作流需要 3 处改造来匹配。本文件给你具体步骤。
> **配套文档**：`PIPELINE.md § 8` + `DIFY_POSTIZ_SETUP_CHECKLIST.md`
> **日期**：2026-05-12

---

## 0. 改造 3 处

| # | 节点 | 改什么 | 难度 |
|---|---|---|---|
| 1 | Webhook Trigger 入参 schema | 加 `drafts[]` 数组结构（支持多平台 1 次提交） | 低 |
| 2 | Human Input Node | 一次审核 N 平台全包；按钮变成"Approve All / Edit Specific / Reject All" | 中 |
| 3 | HTTP Request Node（→ Postiz） | 改成循环节点，遍历 `drafts[]` 数组逐条发 | 中 |

---

## 1. 改造 1 · Webhook Trigger 入参 schema 升级

### 现状 schema（PIPELINE § 9.1 旧）

```json
{
  "platform": "twitter",
  "draft_md": "...",
  "scheduled_at_iso": "...",
  ...
}
```

### 升级后 schema（错峰多平台一次提交）

```json
{
  "inputs": {
    "primary_idea_id": "perps_dex_lies_2026W19",
    "audience": "CMO",
    "contradiction": 4,
    "hook": "<一句话钩子>",
    "score": 42,
    "dry_run": false,
    "drafts": [
      {
        "platform": "twitter",
        "draft_md": "<X Thread 正文>",
        "scheduled_at_iso": "2026-05-18T09:00:00-04:00"
      },
      {
        "platform": "linkedin",
        "draft_md": "<LinkedIn Carousel 10 页 markdown>",
        "scheduled_at_iso": "2026-05-19T08:00:00-04:00"
      },
      {
        "platform": "blog",
        "draft_md": "<Blog 长文>",
        "scheduled_at_iso": "2026-05-20T10:00:00-04:00"
      },
      {
        "platform": "yt_shorts",
        "draft_md": "<YT Shorts 脚本>",
        "scheduled_at_iso": "2026-05-21T14:00:00-04:00"
      }
    ]
  },
  "response_mode": "blocking",
  "user": "claude-pipeline"
}
```

### Dify 操作步骤

1. 打开 `content.taskon.xyz` 现有内容生产工作流
2. Webhook Trigger 节点 → 编辑入参 schema → 删旧 `platform / draft_md / scheduled_at_iso` 三字段
3. 加新字段：
   - `primary_idea_id` (string, required)
   - `audience` (string, required)
   - `contradiction` (number 1-8, required)
   - `hook` (string, required)
   - `score` (number, required)
   - `dry_run` (boolean, default true)
   - `drafts` (array, required, schema 见下)

`drafts` 数组每项 schema：
- `platform` (enum: twitter | linkedin | blog | yt_shorts | tiktok | threads | bluesky | telegram | farcaster | reddit)
- `draft_md` (string)
- `scheduled_at_iso` (string, ISO 8601 with offset)

---

## 2. 改造 2 · Human Input Node 升级

### 关键变化：批次审核而非逐条

Donald 在邮件 / Dify Web app 里看到的是**整批 N 平台**草稿（不是 1 条 1 条），节省 4x 时间。

### Human Input Node 配置

**Title**: `审核 <primary_idea_id> · <drafts.length> 个平台`

**Form Fields**（让 Donald 看的）：
- 显示 `hook` + `audience` + `contradiction` + `score` 头部信息（read-only）
- 遍历 `drafts[]` 每条显示：
  - `platform`（read-only）
  - `scheduled_at_iso`（可编辑——Donald 可微调时段）
  - `draft_md`（可编辑——Donald 可直接改文案）
  - 单条复选框：「☑ 包含在批准批次」（默认勾上）

**Decision Buttons**:
- 🟢 **Approve All Checked** —— 发布所有勾选的 drafts
- 🟡 **Edit & Resend** —— Donald 改完点这个，工作流回到 LLM 重新格式化（暂存待二次审）
- 🔴 **Reject All** —— 全部废弃 + 收集"拒因"自由文本 → 回流 Claude 记忆

**Timeout**: 4 小时
**Timeout 分支**: 自动 Reject + Email + 飞书 "审核超时" 通知 Donald

**Delivery**: Email → `donald@taskon.xyz`

### Dify 操作步骤

1. 找到 Human Input Node（如未建过先按 `DIFY_POSTIZ_SETUP_CHECKLIST.md` § A.1 Step 3 建）
2. Form Fields 改成"动态遍历 drafts[]"（Dify 支持 `{{ #drafts # }}` 循环模板）
3. 调整三个 Decision Buttons 名称
4. Timeout 改成 4 小时
5. Delivery 配 `donald@taskon.xyz`

---

## 3. 改造 3 · HTTP Request Node 循环改造

### 现状（单平台单调用）

一个 HTTP Node 调一次 Postiz API。

### 升级后（多平台循环调用）

加一个 **Iteration / Loop Node**（Dify 1.x 原生支持 `Iteration` 节点），遍历 `drafts[]` 数组，对每条勾选的草稿独立调用 Postiz。

### 流程图

```
Human Input (Approve All Checked)
    ↓
Code Node: filter checked drafts
    ↓ 输出 checked_drafts: [...]
    ↓
Iteration Node: for each draft in checked_drafts
    ├── HTTP Request Node: POST <POSTIZ_BASE_URL>/api/public/v1/posts
    │       body: {
    │         "type": "schedule",
    │         "date": "{{ draft.scheduled_at_iso }}",
    │         "tags": ["{{ contradiction }}", "{{ audience }}"],
    │         "posts": [{
    │           "integration": "{{ integration_id_lookup_by_platform[draft.platform] }}",
    │           "value": [{ "content": "{{ draft.draft_md }}", "image": [] }],
    │           "settings": {}
    │         }]
    │       }
    └── (失败时) Retry 2 次 → 仍失败转 Notify
    ↓
Aggregate Node: 合并所有 Postiz API responses
    ↓
Notify Node: 飞书推 "✅ 已排程 N 平台，Postiz 分别返回 ID: ..."
    ↓
End
```

### Dify 操作步骤

1. Human Input 之后加 Code Node：
   ```javascript
   // 过滤勾选的 drafts
   const checked = drafts.filter((_, i) => human_input.checkboxes[i] === true);
   return { checked_drafts: checked };
   ```

2. 加 Iteration Node：
   - Input array: `checked_drafts`
   - Item variable name: `draft`

3. Iteration 内放 HTTP Request Node：
   - URL: `<POSTIZ_BASE_URL>/api/public/v1/posts`
   - Method: POST
   - Headers: 同 PIPELINE § 9.1
   - Body：用 `{{ draft.* }}` 引用单条字段
   - **integration ID 查表**（按 `draft.platform` 字段）：
     ```javascript
     const map = {
       twitter:    "<your postiz int id 1>",
       linkedin:   "<your postiz int id 2>",
       blog:       "<your postiz int id 3>",
       yt_shorts:  "<your postiz int id 4>",
       tiktok:     "<your postiz int id 5>",
       threads:    "<your postiz int id 6>",
       bluesky:    "<your postiz int id 7>",
       telegram:   "<your postiz int id 8>",
       farcaster:  "<your postiz int id 9>",
       reddit:     "<your postiz int id 10>",
     };
     return { integration_id: map[draft.platform] || null };
     ```

4. 加 Retry 配置（HTTP Node 设置）：最多 2 次，指数退避（30s / 5min / 30min，对应 v3 B2 §3.1 限流策略）

5. Aggregate Node 收所有结果

6. Notify Node 调飞书 webhook（或 Telegram bot），格式：
   ```
   ✅ <primary_idea_id> 已排程
   - Twitter: <postiz_post_id> · <scheduled_at>
   - LinkedIn: <postiz_post_id> · <scheduled_at>
   - Blog: <postiz_post_id> · <scheduled_at>
   - YT Shorts: <postiz_post_id> · <scheduled_at>
   ```

---

## 4. （可选）改造 4 · 平台后台 6 触点工作流

> Wave 1 不必做，但 Wave 2 B2-2 时要做。先记下。

Dify 工作流 `weekly-btouch-push`：
- Trigger: Cron 每周一 08:00 ET
- Step 1: HTTP GET `D:\TaskOn\marketing\engine\update_btouch.py` 输出（从本地通过 SSH 或 webhook）
- Step 2: HTTP POST 到 TaskOn admin API `/admin/content_card`（6 触点逐个）
- Step 3: 失败 → 飞书告警兼职女生

但这个 Wave 1 不动。等技术先把 `/admin/content_card` endpoint 加上 + Cowork 写好 `update_btouch.py`。

---

## 5. 测试节奏

### 第 1 步 · Schema 升级测试（本周）

- Donald 改完 Webhook schema → Claude 这边发一次假 payload（4 平台 dummy 草稿，dry_run: true）
- 验证 Dify 入参解析正确（在 Dify Run history 看 input log）

### 第 2 步 · Human Input 批次审核测试

- Donald 邮箱收到一封审核邮件，里面有 4 个草稿可勾选 + 时间可改 + 文案可改
- 试三种动作各一次（全 Approve / Edit & Resend / Reject All）

### 第 3 步 · Postiz 循环调用测试

- Approve 后 Iteration 跑通，飞书收到 4 平台 排程通知
- 在 Postiz 后台看到 4 条草稿（不是发布，因为 dry_run）

### 第 4 步 · 真实发布

- 把 dry_run 改 false
- 验证 Postiz 真的发到 4 平台

---

## 6. 阻塞项

| 阻塞项 | 谁解决 | SLA |
|---|---|---|
| 4 个 Dify 参数（base URL / workflow ID / API key / 已挂 Postiz integration IDs） | Donald | 本周 |
| Dify 1.13+ 版本确认（Human Input 节点 + Iteration 节点都需要） | Donald 验证 | 本周 |
| Postiz 本地部署各平台账号挂入 | Donald 在 Postiz UI | 本周 |

回完 Donald 立刻：
- 写 `D:\TaskOn\marketing\.env`
- 写测试脚本 `D:\TaskOn\marketing\scripts\test_dify_wave1.sh`
- 在 PIPELINE.md § 9.2 把占位符填实
- 跑第 1 步 Schema 升级测试
