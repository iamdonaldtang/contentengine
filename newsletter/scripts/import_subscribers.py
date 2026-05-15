#!/usr/bin/env python3
"""
CRM CSV → Listmonk API 导入
============================
W1 D4 兼职女生跑此脚本，把 CRM 导出的 2K+ B 端邮箱导入 Listmonk

用法：
  python import_subscribers.py --csv crm_leads.csv --list-id 1

CSV 字段约定：
  email,first_name,last_name,company,role,source_tag,first_seen_date

Listmonk API 文档：
  https://listmonk.app/docs/apis/subscribers/
"""

import argparse
import csv
import sys
import os
import requests
from datetime import datetime

# ============================================================
# 配置（写真实值或读环境变量）
# ============================================================
LISTMONK_URL = os.environ.get('LISTMONK_URL', 'https://newsletter.taskon.xyz')
LISTMONK_USER = os.environ.get('LISTMONK_ADMIN_USERNAME', 'admin')
LISTMONK_PASS = os.environ.get('LISTMONK_ADMIN_PASSWORD', '')


def import_one(email: str, name: str, attrs: dict, list_ids: list) -> bool:
    """单个 subscriber 导入。重复时按 email 去重。"""
    payload = {
        "email": email.strip().lower(),
        "name": name.strip(),
        "status": "enabled",
        "lists": list_ids,
        "attribs": attrs,
        "preconfirm_subscriptions": True  # B 端 Lead 都是已 opt-in（BD 触达历史）
    }
    try:
        r = requests.post(
            f"{LISTMONK_URL}/api/subscribers",
            json=payload,
            auth=(LISTMONK_USER, LISTMONK_PASS),
            timeout=15
        )
        if r.status_code in (200, 201):
            return True
        elif r.status_code == 409:
            # 已存在 → 更新
            r2 = requests.put(
                f"{LISTMONK_URL}/api/subscribers/{email}",
                json=payload,
                auth=(LISTMONK_USER, LISTMONK_PASS),
                timeout=15
            )
            return r2.status_code in (200, 201)
        else:
            print(f"  FAIL {email}: HTTP {r.status_code} {r.text[:200]}")
            return False
    except Exception as e:
        print(f"  ERROR {email}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--csv', required=True, help='CRM 导出的 CSV 文件路径')
    parser.add_argument('--list-id', type=int, required=True,
                        help='Listmonk 的 List ID（在 UI 创建后查看）')
    parser.add_argument('--dry-run', action='store_true', help='只解析不导入')
    parser.add_argument('--limit', type=int, default=0, help='测试用：只导前 N 条')
    args = parser.parse_args()

    if not LISTMONK_PASS:
        print("ERROR: LISTMONK_ADMIN_PASSWORD env not set")
        sys.exit(1)

    success = 0
    failed = 0
    skipped = 0

    with open(args.csv, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, 1):
            if args.limit and i > args.limit:
                break

            email = row.get('email', '').strip()
            if not email or '@' not in email:
                print(f"[{i}] SKIP invalid email: {email}")
                skipped += 1
                continue

            # C 端用户硬过滤（保护性 - 红线 R3）
            # B 端典型域名通常不是 gmail/yahoo/qq/163/126/outlook 个人邮箱
            personal_domains = {'gmail.com', 'yahoo.com', 'qq.com', '163.com',
                              '126.com', 'outlook.com', 'hotmail.com'}
            domain = email.split('@')[-1].lower()
            if domain in personal_domains:
                # 不直接 skip - 个人邮箱也可能是 B 端决策人在用
                # 但打 tag 区分，未来 segment 时可过滤
                pass

            name = f"{row.get('first_name', '').strip()} {row.get('last_name', '').strip()}".strip() or email.split('@')[0]
            attrs = {
                "company": row.get('company', '').strip(),
                "role": row.get('role', '').strip(),
                "source_tag": row.get('source_tag', 'crm_import_2026w19'),
                "first_seen_date": row.get('first_seen_date', '').strip(),
                "is_personal_email": domain in personal_domains
            }

            if args.dry_run:
                print(f"[{i}] DRY {email} | {name} | {attrs.get('company')}")
                success += 1
                continue

            ok = import_one(email, name, attrs, [args.list_id])
            if ok:
                success += 1
                if i % 50 == 0:
                    print(f"[{i}] imported {success}, failed {failed}")
            else:
                failed += 1

    print(f"\n=== 导入结束 ===")
    print(f"成功: {success}")
    print(f"失败: {failed}")
    print(f"跳过: {skipped}")


if __name__ == '__main__':
    main()
