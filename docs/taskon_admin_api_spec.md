# TaskOn Internal Admin API · 6 触点 + 内容卡片 推 / 拉规约

> **本文档定位**:给 TaskOn 内部技术同事的接口实施规约。营销引擎的 `update_btouch` cron(周一 09:00)按下面 2 个 endpoint 调你们的内部 admin API。
>
> **真相源**:本规约 = `Metrics_Collector_归因引擎_需求文档.md §2.5`(数据契约)+ `Engine_Components_PRD.md §2.9`(业务语义)+ 本文(工程同事 onboarding 视角)
>
> **维护人**:Donald(产品) + 内部技术(实施)+ engine 维护者(消费方)
> **上次更新**:2026-05-13

---

## 0 · TL;DR

```
营销引擎 → POST /admin/content_card    × 6 次/周(周一 09:00)
营销引擎 → GET  /admin/touchpoint_stats × 每天(20:00)拉昨日 CTR

→ 6 个产品内触点(dashboard_card / docs_link / changelog / template_link / metrics_page / error_explain)
  每周都换上最新一条内容(blog / playbook / benchmark / case study)的卡片
  + 把昨日 CTR 回流到 TaskOn 中台 SQLite 喂归因
```

工程同事的活:实现 2 个 endpoint 按下表契约,Auth 用内部 admin bearer。

---

## 1 · 认证

```http
Authorization: Bearer {TASKON_ADMIN_TOKEN}
```

`TASKON_ADMIN_TOKEN` 是内部不可外泄的长期 bearer。**强烈建议**:

- Token 与 user-facing API 完全隔离(别用同一个 token bus)
- 失效后只影响营销 cron,**不影响 TaskOn 产品本身**
- 通过 IP allowlist 收口到 engine 容器出口 IP

`engine/sources/btouch.py:BTouchClient` 已经按此 Auth 调,无需改变 client 侧。

---

## 2 · Endpoint 1 · POST /admin/content_card

把一张内容卡片 push 到指定触点位。

### 2.1 · Request

```http
POST /admin/content_card HTTP/1.1
Host: api.taskon.xyz
Authorization: Bearer {TASKON_ADMIN_TOKEN}
Content-Type: application/json

{
  "touchpoint_id": 1,
  "title": "47% Quest 预算被 Bot 吃 — Q1 2026 Benchmark",
  "link": "https://taskon.xyz/benchmark-report?utm_source=taskon&utm_medium=dashboard&utm_campaign=2026q1_benchmark&utm_content=dashboard_top&utm_term=47pct_bot",
  "preview": "本季全平台 Quest 数据交叉验证:47% 的 claim 来自重复地址或脚本钱包。给增长负责人的反 Sybil playbook。"
}
```

### 2.2 · Field 契约

| Field | Type | 必填 | 说明 |
|---|---|---|---|
| `touchpoint_id` | int | ✅ | 1..6,见 §4 触点定义 |
| `title` | string ≤ 80 字 | ✅ | 卡片标题(用户端看见) |
| `link` | string URL | ✅ | 点击跳转 URL,**必须含 5 段 UTM**(否则归因失败) |
| `preview` | string ≤ 200 字 | ✅ | 卡片副标题 / 摘要 |

`engine/sources/btouch.py:push_content_card()` 已强校验 `touchpoint_id ∈ 1..6` 和 4 个字段非空,**所以服务端就算回 200 也别忘自己校验**。

### 2.3 · Response

```json
{
  "status": "ok",
  "touchpoint_id": 1,
  "card_id": "card_abc123",
  "effective_at": "2026-05-13T09:00:00Z"
}
```

| Field | 说明 |
|---|---|
| `card_id` | TaskOn 内部生成的卡片 id(用于后续 delete / replace) |
| `effective_at` | 卡片可见时间 ISO8601;通常 = 请求时间 |

### 2.4 · 错误码

| HTTP | Error code | 触发条件 |
|---|---|---|
| 400 | `bad_touchpoint_id` | 不在 1..6 |
| 400 | `bad_link` | URL 不合法或缺 UTM 5 段 |
| 401 | `unauthorized` | Bearer token 无效 |
| 409 | `card_replaced` | 同 touchpoint_id 已有卡片(可选语义:服务端默认覆盖,这里 409 仅做提示) |
| 500 | `internal_error` | 内部错误 |

营销引擎对 4xx 不重试(写 publish_failures + 告警),对 5xx 用指数退避 3 次(`@retryable()`)。

---

## 3 · Endpoint 2 · GET /admin/touchpoint_stats

拉 6 触点的曝光 / 点击 / CTR。

### 3.1 · Request

```http
GET /admin/touchpoint_stats?date_from=2026-05-12&date_to=2026-05-12 HTTP/1.1
Host: api.taskon.xyz
Authorization: Bearer {TASKON_ADMIN_TOKEN}
```

`date_from` / `date_to` 都是 ISO8601 日期(`YYYY-MM-DD`),**Asia/Shanghai 时区**(与 TaskOn 内部日报对齐;若是 UTC 请明示)。包含端点。

### 3.2 · Response

```json
{
  "touchpoints": [
    {
      "touchpoint_id": 1,
      "touchpoint_name": "dashboard_card",
      "impressions": 850,
      "clicks": 42,
      "ctr": 0.0494,
      "utm_medium": "dashboard"
    },
    {
      "touchpoint_id": 2,
      "touchpoint_name": "docs_link",
      "impressions": 320,
      "clicks": 18,
      "ctr": 0.0563,
      "utm_medium": "docs"
    }
    // ...4 more
  ]
}
```

### 3.3 · Field 契约

| Field | Type | 说明 |
|---|---|---|
| `touchpoint_id` | int | 1..6 |
| `touchpoint_name` | enum string | 见 §4 触点定义 |
| `impressions` | int | 当天该触点位被渲染次数 |
| `clicks` | int | 卡片被点击次数 |
| `ctr` | float | `clicks / impressions`(0..1) |
| `utm_medium` | enum string | 必须等于 `engine/config.yaml :: btouch.touchpoints[i].utm_medium`(见 §4) |

营销引擎 `BTouchClient.get_touchpoint_stats()` 兼容两种响应壳:

- `{"touchpoints": [...]}`(推荐)
- `{"data": [...]}`
- 裸数组 `[...]`

请实现时选 `{"touchpoints": [...]}` —— 跟 `sources/btouch.py:189-202` 一致。

### 3.4 · 错误码

| HTTP | Error code | 触发条件 |
|---|---|---|
| 400 | `bad_date_range` | 缺日期或 `date_from > date_to` |
| 401 | `unauthorized` | Bearer token 无效 |
| 500 | `internal_error` | 内部错误 |

---

## 4 · 6 触点定义(必须与 engine/config.yaml 对齐)

| ID | Name | Preferred Content Type | UTM Medium | 备注 |
|---|---|---|---|---|
| 1 | `dashboard_card` | blog / data_insight | `dashboard` | 用户登录后首页右上 |
| 2 | `docs_link` | playbook / methodology | `docs` | 文档站头部横幅 |
| 3 | `changelog` | product_update / extended_reading | `changelog` | 更新日志页底部 |
| 4 | `template_link` | methodology / case_study | `template` | 模板库列表底部 |
| 5 | `metrics_page` | benchmark / data_insight | `metrics_page` | 数据看板侧栏 |
| 6 | `error_explain` | methodology / design_rationale | `error_explain` | 错误页 / 空状态推荐 |

这 6 个 utm_medium 是 `lib/utm.py` 已锁定的枚举,**不允许新增**。如果产品想换其他位置,先去 engine 端 PR 改枚举,再改 admin API。

---

## 5 · UTM 链接生成纪律(开发自测必看)

营销引擎在调 `push_content_card` 之前会拼好完整 5 段 UTM:

```
https://taskon.xyz/<route>
  ?utm_source=taskon
  &utm_medium=<dashboard|docs|changelog|template|metrics_page|error_explain>
  &utm_campaign=<piece_id 全小写下划线>
  &utm_content=<dashboard_top|docs_header|...>
  &utm_term=<hook_type,例 47pct_bot>
```

工程同事的 `/admin/content_card` 实施**不要**改动这个 link(回写或重新拼接都会破坏归因)。原样存,原样吐给前端渲染。

---

## 6 · 实施测试用例(6 条 curl 给工程同事自测)

```bash
# 设置 base + token(本地开发)
export TASKON_ADMIN_API_BASE="http://localhost:8000/admin"
export TOKEN="dev-admin-token-xxx"

# 1 · push card to touchpoint 1 (happy path → 200)
curl -X POST "$TASKON_ADMIN_API_BASE/content_card" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "touchpoint_id": 1,
    "title": "Q1 Benchmark · 47% Sybil",
    "link": "https://taskon.xyz/benchmark-report?utm_source=taskon&utm_medium=dashboard&utm_campaign=2026q1_benchmark&utm_content=dashboard_top&utm_term=47pct_bot",
    "preview": "全平台 Quest 数据反 Sybil 视角的季度报告。"
  }'
# 期望 200 + {status:"ok", card_id:"..."}

# 2 · push card with bad touchpoint_id → 400 bad_touchpoint_id
curl -X POST "$TASKON_ADMIN_API_BASE/content_card" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"touchpoint_id":99,"title":"x","link":"https://x.com","preview":"x"}'

# 3 · push card without auth → 401
curl -X POST "$TASKON_ADMIN_API_BASE/content_card" \
  -H "Content-Type: application/json" \
  -d '{"touchpoint_id":1,"title":"x","link":"https://x.com","preview":"x"}'

# 4 · pull yesterday's stats (happy path → 200)
curl "$TASKON_ADMIN_API_BASE/touchpoint_stats?date_from=2026-05-12&date_to=2026-05-12" \
  -H "Authorization: Bearer $TOKEN"
# 期望 200 + {"touchpoints":[{touchpoint_id:1, impressions:N, clicks:M, ctr:0.0xx, utm_medium:"dashboard"}, ...×6]}

# 5 · pull with bad date range (from > to) → 400 bad_date_range
curl "$TASKON_ADMIN_API_BASE/touchpoint_stats?date_from=2026-05-12&date_to=2026-05-11" \
  -H "Authorization: Bearer $TOKEN"

# 6 · pull with missing date → 400
curl "$TASKON_ADMIN_API_BASE/touchpoint_stats" \
  -H "Authorization: Bearer $TOKEN"
```

通过 6 条全部预期返回 = 你的实施可以让 engine 直接调通。

---

## 7 · engine 侧消费契约(参考)

完整代码:[`engine/sources/btouch.py`](../sources/btouch.py:1)

`engine/jobs/update_btouch.py`(周一 09:00 cron)调用模式:

```python
from sources.btouch import btouch

# Push 6 cards
for tp in CONFIG_TOUCHPOINTS:  # config.yaml :: btouch.touchpoints
    btouch.push_content_card(
        touchpoint_id=tp["id"],
        title=card.title,
        link=card.utm_link,
        preview=card.preview,
    )

# Pull yesterday stats (每天 20:00 由 metrics_collector 调)
stats = btouch.get_touchpoint_stats(
    date_from=yesterday_iso,
    date_to=yesterday_iso,
)
for row in stats:
    db.btouch_daily.insert(
        snapshot_date=yesterday_iso,
        touchpoint_id=row["touchpoint_id"],
        touchpoint_name=row["touchpoint_name"],
        impressions=row["impressions"],
        clicks=row["clicks"],
        ctr=row["ctr"],
    )
```

---

## 8 · 阻塞 / 当前状态

| 项目 | 状态 | 阻塞方 |
|---|---|---|
| engine `sources/btouch.py` 已实现 | ✅ | — |
| engine `jobs/update_btouch.py` 已实现 + cron 在跑 | ✅ | — |
| `.env :: TASKON_ADMIN_TOKEN` 填值 | ❌ | 内部技术给 token |
| `.env :: TASKON_ADMIN_API_BASE` 填值 | ❌ | 内部技术给 URL |
| **TaskOn 内部技术实现 `/admin/content_card`** | ❌ | 内部技术(本文档驱动) |
| **TaskOn 内部技术实现 `/admin/touchpoint_stats`** | ❌ | 内部技术(本文档驱动) |

当前 `update_btouch` cron 周一 09:00 会一直 P1 告警(无 endpoint 可调)。建议:**短期** 在 `docker/crontab` 注释该行;**长期** 内部技术按本文档实施 2 个 endpoint(2-3 天工时)。

---

## 9 · 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-05-13 | 首版 T12 实施;字段 / 错误码 / 6 触点 / 6 条 curl 自测 |
