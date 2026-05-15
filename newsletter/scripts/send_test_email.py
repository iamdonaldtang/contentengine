#!/usr/bin/env python3
"""
SMTP 测试 · 验证 deliverability
================================
W1 D3 部署后跑此脚本，发 1-5 封测试邮件到 Donald 自己邮箱
看是否进 Inbox 还是 Spam

用法：
  python send_test_email.py --to donald@taskon.xyz --to dwt@gmail.com

注意：
  - SendGrid Free 阶段不要发到非自己控制的邮箱（容易触发投诉）
  - 测试通过后才能进入 W3 warm-up
"""

import argparse
import os
import sys
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--to', action='append', required=True,
                        help='收件邮箱（可重复指定）')
    parser.add_argument('--smtp-host', default=os.environ.get('SMTP_HOST', 'smtp.sendgrid.net'))
    parser.add_argument('--smtp-port', type=int, default=int(os.environ.get('SMTP_PORT', '587')))
    parser.add_argument('--smtp-user', default=os.environ.get('SMTP_USERNAME', 'apikey'))
    parser.add_argument('--smtp-pass', default=os.environ.get('SMTP_PASSWORD'))
    parser.add_argument('--from-email', default=os.environ.get('SMTP_FROM_EMAIL', 'newsletter@taskon.xyz'))
    parser.add_argument('--from-name', default=os.environ.get('SMTP_FROM_NAME', 'TaskOn Growth'))
    args = parser.parse_args()

    if not args.smtp_pass:
        print("ERROR: SMTP_PASSWORD env not set or --smtp-pass not provided")
        sys.exit(1)

    # HTML 测试邮件（最小可用结构）
    html = f"""\
<html>
<body style="font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif;
             max-width: 600px; margin: 0 auto; color: #333;">

  <div style="padding: 20px;">
    <h1 style="color: #1a73e8;">TaskOn Newsletter Engine · 测试邮件</h1>

    <p>这是 Listmonk + SMTP relay 部署后的内部测试邮件。</p>

    <h3>验证清单</h3>
    <ul>
      <li>邮件进 Inbox（不是 Spam）</li>
      <li>HTML 渲染正常（不被截断）</li>
      <li>From 字段显示 "TaskOn Growth &lt;newsletter@taskon.xyz&gt;"</li>
      <li>DKIM 通过（在 Gmail 点 ⋮ → "Show original" 查看）</li>
      <li>SPF 通过（同上）</li>
      <li>DMARC 通过（同上）</li>
    </ul>

    <p><strong>发送时间</strong>：{datetime.utcnow().isoformat()}Z</p>
    <p><strong>SMTP relay</strong>：{args.smtp_host}</p>

    <hr style="border: none; border-top: 1px solid #ddd; margin: 30px 0;">

    <p style="font-size: 12px; color: #999;">
      TaskOn HQ | Web3 Growth Engine<br>
      <a href="https://taskon.xyz/unsubscribe?test=1" style="color: #999;">
        Unsubscribe
      </a> | 公司地址占位 · W1 D5 前补真实地址
    </p>
  </div>

</body>
</html>
"""

    msg_subject = f"[TaskOn 内部测试] Newsletter Engine deliverability check {datetime.utcnow().strftime('%H:%M')}"

    success, failed = [], []

    try:
        server = smtplib.SMTP(args.smtp_host, args.smtp_port, timeout=30)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(args.smtp_user, args.smtp_pass)
        print(f"✓ Connected to {args.smtp_host}:{args.smtp_port}")

        for recipient in args.to:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = msg_subject
            msg['From'] = f"{args.from_name} <{args.from_email}>"
            msg['To'] = recipient
            msg['Reply-To'] = args.from_email
            msg['List-Unsubscribe'] = '<mailto:unsubscribe@taskon.xyz>, <https://taskon.xyz/unsubscribe>'
            msg.attach(MIMEText(html, 'html'))

            try:
                server.send_message(msg)
                print(f"  ✓ sent to {recipient}")
                success.append(recipient)
            except Exception as e:
                print(f"  ✗ FAIL {recipient}: {e}")
                failed.append((recipient, str(e)))

        server.quit()
    except Exception as e:
        print(f"✗ SMTP connection failed: {e}")
        sys.exit(2)

    print(f"\n=== 测试结束 ===")
    print(f"成功: {len(success)}")
    print(f"失败: {len(failed)}")

    if success:
        print(f"\n下一步：去收件邮箱查看")
        print(f"  - 是否进 Inbox（不是 Spam）")
        print(f"  - DKIM/SPF/DMARC 是否通过（Gmail: ⋮ → Show original）")
        print(f"  - HTML 渲染是否正常")


if __name__ == '__main__':
    main()
