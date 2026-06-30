# Postiz x-browser bridge —— X thread 多 value block 验证 / 改动交接

> 交接对象:引擎机 `D:\engine-host\postiz-app`(Postiz fork · x-browser / local-twitter provider)
> 触发背景:2026-06-05 免费号 @donaldlongsbtc 发 X thread,Postiz 把**单个** 2765 字 value 自动切成"1 主推 + 多回复",但**切点不合理**(切在句中)。
> 引擎端已改:不再发一坨,改成**前端 AI 按 `## Tweet N` 语义边界预切**、每条 ≤280(免费)/≤25k(Premium),按 Postiz 标准 thread 结构发多个 value block。

---

## 0. 一句话结论(先看这个)

**大概率 bridge 完全不用动**——前提是你们的 x-browser provider 遵循 Postiz 标准 thread 语义:`value[]` 数组 = 一条 thread,`value[0]` 用 `post()` 发主推,`value[1..]` 用 `comment()` 逐条挂回复。

但因为这是 fork + 我们实测到过"单 value 被自动切",**必须先验证下面 2 点**再下定论。验证通过 = 零改动;不通过 = 按 §4 改一小块。

---

## 1. 引擎端现在发什么(新契约 · 已是这样)

`POST /api/public/v1/posts`,body 关键结构:

```json
{
  "type": "schedule",
  "date": "2026-06-08T13:00:00.000Z",
  "posts": [
    {
      "integration": { "id": "cmp6pg3jj0001n57c0j34jevs" },
      "value": [
        { "content": "主推文案(已 ≤280 加权字符)", "image": [] },
        { "content": "第 2 条(回复)", "image": [] },
        { "content": "第 3 条(回复)", "image": [] }
      ],
      "settings": { "who_can_reply_post": "everyone" }
    }
  ]
}
```

要点:
- `value` 数组**每个元素 = thread 里的一条推**,顺序即发布顺序,`value[0]` = 主推。
- **每条 content 已经在引擎端切到 ≤280 加权字符(免费号)**——CJK/emoji 按 2 计。bridge **不需要、也不应该再切**。
- 媒体(图/视频)只挂在 `value[0]`(主推)。
- `settings.who_can_reply_post` 在 post 级(主推)。

---

## 2. 必须验证的 2 个点

### 验证点 A —— bridge 是否"逐条发"多 value block

在 x-browser / local-twitter provider 里找到处理一条 post 的入口(通常是 `post()` 方法,thread 用 `comment()` 挂回复)。确认逻辑是:

```
对 posts[i].value 数组:
  value[0]      -> post()      // 主推,拿回 tweet id
  value[1..n]   -> comment(上一条 tweet id, value[k].content)  // 回复链
```

**预期**:provider 遍历 `value[]`,逐条发。若是这样 → ✅ 通过。

### 验证点 B —— core / bridge 会不会对"多条 value"再切 / 再合并

之前**单条** 2765 字能出多回复,说明某处有"按字数自动切 thread"的逻辑。找到它(可能在 Postiz core 的 post 预处理,或 provider 内部),确认:

- 它只在**单条 value 超长**时才切(把 1 条切成 N 条);
- 对**已经多条且每条 ≤280** 的 value 数组**不再动**(不拼回一坨、不重切、不归一化)。

**预期**:标准 Postiz 不会重切一个多元素 value 数组。若确认如此 → ✅ 通过。

### 怎么验证(最快)

引擎端代码部署后,跑一次 X 的 schedule_planner dry-run/真发,看 X 上**实际出来的分条**是否与引擎日志里 `x_thread split: ... tweets=N weighted_lens=[...]` **完全一致**(条数、每条内容边界)。一致 = bridge 照单全发,零改动。不一致(条数变了/边界被重切)= 命中 §4。

---

## 3. 如果两点都通过

**bridge 不用动。** 引擎端预切 + 标准 value 数组就是完整方案。本 MD 归档即可。

---

## 4. 如果验证不通过(fallback 改动)

按命中的情况改 provider(只改投递,不要把业务/长度逻辑塞进来):

- **情况 1:provider 只读了 `value[0]`,忽略其余** → 改成遍历 `value[]`:`value[0]` 走 `post()` 拿 id,`value[1..]` 依次 `comment(prevId, content)`,每次用上一条返回的 tweet id 串成回复链。
- **情况 2:provider/core 把多条 value 拼回一坨再自己切** → 增加一个"已分条则不切"的短路:当 `value.length > 1` 时,**禁用**内部自动切,直接逐条 post/comment;只有 `value.length === 1 && 超长` 时才走旧的自动切。
- **情况 3:每条仍被按 280 再切** → 不应发生(引擎已 ≤280);若发生,说明 bridge 的字数口径和 X 加权口径不一致,**以引擎切好的为准,bridge 不要再切**。

红线:**长度/切分是引擎(Cowork)端的职责**(它知道 free/premium、知道 `## Tweet` 语义边界)。bridge 只负责"把引擎切好的多条,按 post()+comment() 原样发出去"。不要在 Postiz fork 里复制一份长度规则——会和引擎分叉、难维护。

---

## 5. 关联

- 引擎端改动:`lib/x_thread_split.py`(切分器)、`jobs/schedule_planner.py`(X 走 thread)、`sources/postiz.py`(`create_post(thread=[...])` → value 数组)、`config.yaml`(`postiz.x_premium` 按账号档位)。
- 账号档位:免费=280/条 thread;Premium=25k/条(可发长贴或长条 thread)。@donaldlongsbtc 当前免费。
- Premium 状态靠 `config.yaml postiz.x_premium` **声明**,不做运行时自动探测(浏览器自动化无可靠信号)。
