# SendGrid Free → AWS SES 切换手册（M4）

> **本文档定位**：M3 末（约 7 月下旬）从 SendGrid Free 切换到 AWS SES 的完整 SOP
> **触发条件**：M3 末发送量 >100/天 OR Open Rate <22% OR Bounce Rate >0.3%
> **前提**：W1 D1 已申请 SES Production Access，此时已批准（应有 1-2 个月时间）

---

## 切换前 Checklist

```
□ SES Production Access 已批准（SES Console 看 "Reputation Tracking" 显示 "Healthy"）
□ SES 在 us-east-1 已验证发件域名 taskon.xyz
□ SES SNS topic 已创建（bounces / complaints / deliveries）
□ SES SMTP credentials 已生成（IAM User + Access Key → 转 SMTP）
□ 当前 SendGrid 30 天数据已备份（用于切换前后对比）
□ Donald 当周日历有 30min 切换 + 1h 监控窗口
```

---

## 切换步骤（30min 操作 + 1 周观察）

### Step 1 · SES SMTP Credentials 创建

```
1. AWS Console → SES → SMTP settings
2. "Create SMTP credentials" → 生成 IAM User
3. 复制 SMTP username + password（只显示一次！）
4. 域名验证（如未做）：SES → Verified identities → Create identity
   - Domain: taskon.xyz
   - DKIM: 使用 Easy DKIM（推荐）→ AWS 给 3 个 CNAME 添加到 DNS
   - Verification: TXT 记录添加到 DNS
   - 等 5-10 分钟 DNS 生效
5. SPF 已含 `include:amazonses.com`（W1 D2 已配） → 不用动
```

### Step 2 · SNS Topic 创建（接 Bounce / Complaint webhook）

```
1. AWS Console → SNS → Create topic
   - Type: Standard
   - Name: taskon-ses-bounces
   - 同样创建 taskon-ses-complaints

2. 配 HTTPS 订阅
   - Subscriptions → Create subscription
   - Protocol: HTTPS
   - Endpoint: https://newsletter-wh.taskon.xyz/ses/sns
   - Donald 等待 webhook_server 自动确认（首次 SubscriptionConfirmation 需手动访问 URL）

3. 看 webhook_server 日志找 confirm URL
   docker-compose logs webhook_server | grep "SubscriptionConfirmation"
   curl <confirm URL>

4. 关联 SES 域名到 SNS topic
   SES Console → Configuration sets → Create configuration set
   Name: taskon-default
   Event destinations:
     - Bounce → SNS taskon-ses-bounces
     - Complaint → SNS taskon-ses-complaints
```

### Step 3 · 切换 Listmonk SMTP

```
1. Listmonk Admin UI → Settings → SMTP

2. 修改：
   Host:        email-smtp.us-east-1.amazonaws.com
   Port:        587
   Auth:        LOGIN
   Username:    <SES SMTP username>
   Password:    <SES SMTP password>
   TLS:         STARTTLS
   Hello hostname: newsletter.taskon.xyz

3. Performance 调整（SES Production 比 SendGrid Free 快得多）:
   Concurrency: 50（之前 10）
   Message rate: 10 邮件/秒（之前 1）

4. "Save and reload"

5. 测试：跑 scripts/send_test_email.py 用 SES credentials
   docker-compose exec webhook_server python scripts/send_test_email.py \
     --smtp-host email-smtp.us-east-1.amazonaws.com \
     --smtp-user <SES username> \
     --smtp-pass <SES password> \
     --to donald@xxx
```

### Step 4 · 更新 .env（保留 SendGrid 作 fallback）

注：SMTP 配置实际在 **Listmonk Admin UI > Settings > SMTP** 改（不在 .env）。.env 这里仅记录新 SES SNS 配置。

```bash
# D:\Taskon\marketing\engine\newsletter\.env

# 新增 SES SNS（M4 切换后启用）
AWS_SES_REGION=us-east-1
AWS_SNS_BOUNCE_TOPIC_ARN=arn:aws:sns:us-east-1:xxx:taskon-ses-bounces
AWS_SNS_COMPLAINT_TOPIC_ARN=arn:aws:sns:us-east-1:xxx:taskon-ses-complaints
```

重启 webhook_server 让 SNS 接入生效：
```powershell
cd D:\Taskon\marketing\engine\newsletter
docker compose restart webhook_server
```

SES SNS 公网入口：通过 Cloudflare Tunnel 已暴露的 `https://newsletter-wh.taskon.xyz/ses/sns`（W1 D5 已配）。SNS 订阅时填这个 URL。

### Step 5 · 切换后第 1 期 warm-up

**不要直接发全量**——SES 共享 IP 池虽然比 SendGrid Free 信誉好，但 TaskOn 域名信誉对 SES 来说仍是新的。

```
切换后第 1 期：发 500 封（已 warm-up 过 SendGrid 的活跃邮箱）
观察 24h:
  ✓ Bounce <0.5%        → 下一期 1500
  ✓ Complaint <0.1%     → ↑
  ✓ Open Rate ≥27%      → SES 比 SendGrid Free 提升 30%+（预期）
  ⚠ 任一不达标          → 缩量到 200，继续 warm-up
```

---

## 观察期（7 天）

切换后 7 天监控指标对比 SendGrid 30 天平均：

| 指标 | SendGrid 30 天 | SES 第 1 周 | 健康判断 |
|---|---|---|---|
| Open Rate | X% | 应 ≥ X% + 5pp | 提升明显 ✅ / 平 ⚠ / 跌 P0 |
| Click Rate | X% | 应 ≥ X% | 平或提升 ✅ |
| Bounce Rate | X% | <0.5% | <0.5% ✅ |
| Complaint Rate | — | <0.1% | <0.1% ✅ |
| Inbox 率（Gmail spot check） | ~60-70% | ≥85% | 提升明显 ✅ |

任一持续不达标 → 立即回滚到 SendGrid（保留的 SMTP_*_FALLBACK 变量）。

---

## SES 限额扩容

SES Production 初始限额 50K 邮件/24h。M6 量 ~5K/月不会触上限。

如未来 >50K/月（多 list / 高频 nurture），向 AWS Support 申请扩容：

```
AWS Support → Create case → Service limit increase
Service: SES
Region: us-east-1
Limit: Sending Quota / Sending Rate
New limit: 100,000 / 24h（按预计 12 个月需求填）
Use case description: 沿用 W1 SES 申请文案
```

通常 1-2 个工作日批准。

---

## 月度成本对比（M4 后 12 个月）

| 月度发送量 | SendGrid Essentials | AWS SES | 差额 |
|---|---|---|---|
| 3K | $19.95/月（>100/天必须升） | $0.30/月 | $19.65/月 |
| 5K | $19.95/月 | $0.50/月 | $19.45/月 |
| 10K | $19.95/月 | $1/月 | $18.95/月 |
| 50K | $89.95/月（Pro 套餐） | $5/月 | $84.95/月 |

**年度节省 ~$200-1000**（视量级）。

---

## 不切换的反向触发条件

如 M3 末满足以下条件，可继续 SendGrid Free 再撑 1-2 个月（不强切）：

```
SendGrid Free 仍达标：
  Bounce < 0.3%
  Open Rate ≥ 25%
  发送量 < 90/天（留 10% buffer）
```

但 M6 量级一定要切——SendGrid Free 100/天 = 月 3000，撑不过 4K Lead × 月度 1 期。

---

## 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-09 | 首版。M4 切换 SOP，含 Step 1-5 + 7 天观察 + 月度成本对比 |
