# Troubleshooting Runbook

> 出问题先查这里。**不要乱改代码 / DDL — 先看告警 + heartbeat 表 + 日志**。

---

## 0 · 第一步永远是这 3 行

```powershell
# 看最近一次告警
python -c "from lib.db import db; rows = db.fetchall('SELECT * FROM publish_failures ORDER BY occurred_at DESC LIMIT 10'); [print(dict(r)) for r in rows]"

# 看每个 job 最近心跳
python -c "from lib.db import db; rows = db.fetchall('SELECT job_name, last_run_at, status, duration_seconds, error_message FROM heartbeat ORDER BY last_run_at DESC LIMIT 20'); [print(dict(r)) for r in rows]"

# 看 Lark 告警渠道
# 浏览器打开 Lark webhook 对应群
```

---

## 1 · LLM 调用问题

### 症状：`LLMClientError: all MiniMaxi keys exhausted`
1. 检查 `.env` 里 3 把 `MINIMAXI_API_KEY_*` 是否都填了 + 是否有效
2. 检查 `ENABLE_ANTHROPIC_FALLBACK=true` + `ANTHROPIC_API_KEY` 是否填
3. 临时绕过：把所有 MiniMaxi key 留空，强制走 Anthropic

### 症状：`complete_json` 一直失败
- 模型返回非 JSON。改 prompt 里加 "Respond with ONLY a valid JSON object, no markdown fences"
- 如果是 MiniMaxi 模型问题：临时切 Anthropic — `ENABLE_MINIMAXI=false`

### 症状：成本异常
- 查 heartbeat 表 `error_message` 看是不是 retry 过多
- 调小 `LLM_MAX_RETRIES_PER_KEY=1`（默认 2，减半）

---

## 2 · SQLite 问题

### 症状：`database is locked`
- 单实例使用时不应该出现。如果出现：检查是否有别的进程持有连接（任务管理器搜 `python.exe`）
- WAL 模式下并发读 OK，并发写串行化。如果有 webhook server + cron 同时写，确保都走 `lib.db.db` 单例
- 临时手段：`python -c "from lib.db import db; db.execute('PRAGMA wal_checkpoint(TRUNCATE)')"`

### 症状：`no such column: ...`
- DDL 没跑或不一致。**先备份**再 `python -m scripts.init_db --verify-only`
- 缺列怎么补：写 migration 文件 `lib/migrations/NNN_description.sql`，重启即应用

### 症状：state.db 损坏
- 恢复最近 backup：`copy runtime\backups\state-YYYYMMDD-HHMMSS.db runtime\state.db`
- 损坏前几天的数据丢失 = 跑当天 `metrics_collector` 补

---

## 3 · 外部 API 失败

### Postiz 拉不到数据
1. 浏览器访问 `{POSTIZ_BASE_URL}/public-api/v1/posts?date=YYYY-MM-DD` 直接看 raw response
2. Postiz 后台重新生成 API key
3. 如 Postiz 自身 down：跳过 → `python -m jobs.metrics_collector --skip-source postiz`

### X Premium API 返回 429
- $200/月套餐 = 3000 reads/day，30 条 Tweet × 3 时点 = 90 calls 远远不够吃
- 大概率是凌晨集中调，把 metrics_collector 触发改成"分段"：08:00 拉 30m / 20:00 拉 24h / 周日 20:00 拉 7d
- 真限流：等到次日自动恢复，本次跑跳过 X 源即可

### GA4 配额耗尽
- 25K queries/day —— 不可能耗尽，除非死循环
- 查 `heartbeat` 看 `metrics_collector` 是否被重复触发

### Listmonk webhook 没收到
1. VPS `webhook_server` 是否在跑：`docker ps | grep listmonk-webhook`
2. Cloudflare Tunnel 是否通：`cloudflared tunnel info <id>`
3. Donald 桌面 `:5051` ingestion endpoint 是否监听：`netstat -ano | findstr :5051`

### TaskOn admin API 失败
- 内部技术同事维护。失败时 `update_btouch` 自动写 `publish_failures` P1
- 临时绕过：跳过本周 push，下周补

---

## 4 · 调度问题

### Windows Task Scheduler 任务没跑
1. 确认任务存在：`schtasks /Query /TN "TaskOn-metrics-collector" /V /FO LIST`
2. 确认账号有 "Run whether user is logged on or not" 权限
3. 查任务历史：任务计划程序 → 历史记录
4. 手动跑一次验证：`schtasks /Run /TN "TaskOn-metrics-collector"`

### Linux systemd timer 没触发
1. `systemctl list-timers 'taskon-*'` —— `NEXT` 列应该是未来时间
2. `journalctl -u taskon-metrics-collector.service -n 50` 看具体错误
3. 时区不对：`timedatectl set-timezone Asia/Shanghai` + 重启 timer

### 桌面睡眠导致漂移
- Windows 电源选项 → 高级 → 允许唤醒计算机执行计划任务
- 真在意：迁 VPS（systemd timer 7×24 不睡）

---

## 5 · 内容生产链问题

### voice_checker 误报 / 漏报
- 漏报：在 `config/voice_disabled_words.yaml` 补词
- 误报：在 `cta_patterns` 列里检查是不是把正常词当 CTA 了
- jieba 切错词：在 jieba 自定义词典加专用术语（如 "TaskOn", "Quest"）

### adapter_orchestrator 跨平台首段太像
- PRD §2.4 要求首段相似度 <30%
- prompt 不够硬：在 `config/prompts/adapter_*.txt` 加 "首句必须独立写，禁止复用 X Thread 第一句的任何短语"
- 测试：`pytest tests/test_adapter.py::test_first_paragraph_distinct -v`

### topic_ranker 分数偏高 / 偏低
- 查 `weekly_aggregates` 看 weight_for_next_week 是否合理（应在 [0.8, 1.2]）
- 改 `config/topic_ranker_rubric.md` 调严/松每个维度
- 极端：临时 `--no-weight-adjust` flag（如未实现可加）

### update_btouch 找不到匹配内容
- 上周 `published` 状态的 pieces 不够 6 条
- 临时：手动给 1-2 条 piece 改状态 `db.pieces.update_state('id', 'published', 'manual')`
- 长期：上游 selection / drafting 速度跟上

---

## 6 · Lark 告警没收到

1. `.env` 里 `LARK_WEBHOOK_URL` 填了吗 + `ENABLE_LARK_ALERTS=true`
2. 浏览器 POST 测试：
   ```bash
   curl -X POST $LARK_WEBHOOK_URL -H "Content-Type: application/json" -d '{"msg_type":"text","content":{"text":"manual test"}}'
   ```
3. 群里把机器人 unmute
4. 程序内测试：`python -m lib.lark`

---

## 7 · Twikit fallback 登录失败

```bash
# 删 cookie 重新登录
del runtime\twikit_cookies.json
python -m jobs.kol_watch --fallback-only
```

如仍登录失败：
- 账号被风控（X 反爬）→ 换备用号
- 加 3-5 个备用号轮询（修改 `lib/twikit_pool.py` 如已实现）
- 长期方案：跑在住宅 IP / 真实浏览器 fingerprint 的 playwright

---

## 8 · shlink 短链问题

### shorten() 失败
- 自托管 shlink 服务挂了：`docker ps | grep shlink`
- 上游 `utm_generator` 已有 fallback：失败时返回长链，不阻塞流程
- 长链可读性差：发布前 Donald 手动编辑

### 同 long URL 生成多个短链
- 不应该 —— `findIfExists=true` 已设
- 真发生：手动 dedupe `DELETE FROM short_urls WHERE ...`（shlink 自有 DB）

---

## 9 · 性能问题

### metrics_collector 跑很慢（>5 分钟）
- 单条 publishing × 3 时点 × 5 源 = 慢源在拖。看 heartbeat `duration_seconds` 拆分
- 改成并发（concurrent.futures.ThreadPoolExecutor）—— W3 后期优化

### topic_ranker LLM 调用太慢
- 30 候选 × 1 次 LLM = 30 调用。改批量：在一次调用里塞多个候选打分
- 缓存：同 candidate_id 已 scored 则跳过

---

## 10 · 紧急恢复（数据 / 服务）

### state.db 完全丢
1. 从 backup 恢复：`runtime/backups/` 找最近一份
2. 当天数据补：跑 `metrics_collector --date YYYY-MM-DD` 重拉
3. user_journey 重建：从落地页 ingestion endpoint 的日志 replay（如未存 → 接受丢失）

### 所有 LLM 都不可用（罕见但要演练）
1. 内容生产线停 24-48h（Donald 手写应急）
2. metrics + attribution 不依赖 LLM，继续跑
3. weekly_reporter 改"裸数据 markdown" 模板（手写 fallback）

### Lark 渠道挂
- 退到 邮件 / SMS 告警（修改 `lib/lark.py` 加 fallback）—— 优先级低，先把 Lark 弄回来

---

## 11 · 升级 / 迁移

### 从开发桌面迁 VPS
1. 停掉桌面 cron
2. `python -m scripts.backup_sqlite` 备份
3. 把 `runtime/state.db` + `runtime/drafts/` 拷到 VPS `/var/lib/taskon/`
4. VPS 跑 `bash scripts/setup_systemd.sh`
5. 24h 双跑观察对账（VPS 写 + 桌面只读）确认一致后关桌面

### Python 升级（3.12 → 3.13+）
- `pyproject.toml` 改 `requires-python`
- `mypy / ruff` 改 target-version
- `pytest -q` 必须先全绿

---

## 12 · 联系方式

- **Donald**：iamdonaldtang@gmail.com / Lark
- **兼职女生**：内部 CRM
- **TaskOn 内部技术同事**（admin API + Cloudflare Tunnel）：内部 IM
