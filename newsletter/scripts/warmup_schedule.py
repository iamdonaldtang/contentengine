#!/usr/bin/env python3
"""
Warm-up 自动化辅助
==================
W3-M2 阶段，按预定 warm-up 阶梯检查"该发多少邮箱"+"上期数据是否达标"

不直接发邮件——是个"前置 checklist 校验器"，告诉兼职女生当周该不该发。

用法：
  python warmup_schedule.py --week 2026W21
"""

import argparse
import sys
import requests
import os
from datetime import datetime

# ============================================================
# Warm-up 阶梯（W1 决策固化）
# ============================================================
WARMUP_SCHEDULE = [
    {"week": "2026W22", "label": "第 1 期", "max_send": 200,  "min_open_rate_prev": None},
    {"week": "2026W23", "label": "第 2 期", "max_send": 500,  "min_open_rate_prev": 0.25},
    {"week": "2026W26", "label": "第 3 期", "max_send": 1500, "min_open_rate_prev": 0.25},
    {"week": "2026W29", "label": "第 4 期", "max_send": 2000, "min_open_rate_prev": 0.25},
    {"week": "M3+",      "label": "全量稳定", "max_send": 9999, "min_open_rate_prev": 0.25},
]


def check_listmonk_last_campaign(listmonk_url, user, pwd):
    """拉上一期 Newsletter 数据"""
    try:
        r = requests.get(
            f"{listmonk_url}/api/campaigns?status=finished&order_by=updated_at&order=DESC&per_page=1",
            auth=(user, pwd),
            timeout=10
        )
        r.raise_for_status()
        data = r.json().get('data', {}).get('results', [])
        if not data:
            return None
        c = data[0]
        sent = c.get('sent', 0)
        views = c.get('views', 0)
        clicks = c.get('clicks', 0)
        bounces = c.get('bounces', 0)
        open_rate = views / sent if sent > 0 else 0
        click_rate = clicks / sent if sent > 0 else 0
        return {
            'id': c.get('id'),
            'name': c.get('name'),
            'sent': sent,
            'views': views,
            'clicks': clicks,
            'bounces': bounces,
            'open_rate': open_rate,
            'click_rate': click_rate
        }
    except Exception as e:
        print(f"Listmonk API error: {e}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--week', required=True, help='本周 (如 2026W22)')
    parser.add_argument('--listmonk-url', default=os.environ.get('LISTMONK_URL',
                                                                  'https://newsletter.taskon.xyz'))
    parser.add_argument('--user', default=os.environ.get('LISTMONK_ADMIN_USERNAME', 'admin'))
    parser.add_argument('--pwd', default=os.environ.get('LISTMONK_ADMIN_PASSWORD'))
    args = parser.parse_args()

    # 找当前阶段
    stage = None
    for s in WARMUP_SCHEDULE:
        if s['week'] == args.week:
            stage = s
            break
    if not stage:
        print(f"⚠️  {args.week} 不在 warm-up 阶梯里，默认按"全量稳定"处理")
        stage = WARMUP_SCHEDULE[-1]

    print(f"=== Warm-up 检查 {args.week} ({stage['label']}) ===")
    print(f"本期上限: ≤ {stage['max_send']} 封")

    # 检查上一期
    if stage['min_open_rate_prev'] is None:
        print(f"✓ 第 1 期无前置 / 直接发 ≤200 封最活跃邮箱")
        sys.exit(0)

    if not args.pwd:
        print("⚠️  LISTMONK_ADMIN_PASSWORD 未设置，无法查上一期数据")
        sys.exit(2)

    last = check_listmonk_last_campaign(args.listmonk_url, args.user, args.pwd)
    if not last:
        print("⚠️  Listmonk 没找到上一期已完成 campaign")
        sys.exit(3)

    print(f"\n上一期：{last['name']}")
    print(f"  Sent:      {last['sent']}")
    print(f"  Open Rate: {last['open_rate']:.1%}  (目标 ≥ {stage['min_open_rate_prev']:.1%})")
    print(f"  Click:     {last['click_rate']:.1%}")
    print(f"  Bounces:   {last['bounces']}")

    bounce_rate = last['bounces'] / last['sent'] if last['sent'] > 0 else 0
    print(f"  Bounce Rate: {bounce_rate:.2%}  (危险阈值 0.5%)")

    if bounce_rate > 0.005:
        print(f"\n⛔ Bounce Rate {bounce_rate:.2%} 超阈值 0.5% — 立即清洗邮箱再发！")
        sys.exit(10)

    if last['open_rate'] < stage['min_open_rate_prev']:
        print(f"\n⛔ Open Rate {last['open_rate']:.1%} < {stage['min_open_rate_prev']:.1%}")
        print(f"   建议：本周不要发更大量；缩到一半（{stage['max_send']//2}）继续 warm-up")
        sys.exit(11)

    print(f"\n✓ 上一期达标，可以发 ≤ {stage['max_send']} 封")
    print(f"  下一步：到 Listmonk Admin UI > Campaigns > New")


if __name__ == '__main__':
    main()
