#!/usr/bin/env python3
"""
从 stooq.com 获取黄金期货（GC.F）周线收盘价，
与 COT 报告日期（周二）按 ISO 周对齐（取当周周五收盘），
保存到 data/gold_price.json。
"""

import csv
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request

STOOQ_URL = "https://stooq.com/q/d/l/?s=gc.f&i=w"
COT_PATH  = os.path.join(os.path.dirname(__file__), "data", "cot.json")
OUT_PATH  = os.path.join(os.path.dirname(__file__), "data", "gold_price.json")


def fetch_stooq() -> dict[str, float]:
    """Download stooq weekly CSV and return {date_str: close_price}."""
    req = Request(STOOQ_URL, headers={
        "User-Agent": "gold-dashboard/1.0",
        "Accept": "text/csv,text/plain,*/*",
    })
    with urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")

    prices = {}
    reader = csv.DictReader(text.splitlines())
    for row in reader:
        date = row.get("Date", "").strip()
        close_str = row.get("Close", "").strip()
        if date and close_str:
            try:
                prices[date] = round(float(close_str), 2)
            except ValueError:
                pass
    return prices


def align_price(cot_date_str: str, price_map: dict[str, float]):
    """
    For a COT Tuesday date, find the closing price of the Friday
    (or nearest prior trading day) of the same ISO week.
    Returns float or None.
    """
    tuesday = datetime.strptime(cot_date_str, "%Y-%m-%d")
    # Friday=+3, Thursday=+2, Wednesday=+1, Tuesday=0
    for delta in (3, 2, 1, 0):
        candidate = (tuesday + timedelta(days=delta)).strftime("%Y-%m-%d")
        if candidate in price_map:
            return price_map[candidate]
    return None


def main():
    print("读取 COT 日期列表...")
    with open(COT_PATH, encoding="utf-8") as f:
        cot = json.load(f)
    cot_dates = [r["date"] for r in cot["weekly"]]
    print(f"  COT 包含 {len(cot_dates)} 周，范围 {cot_dates[0]} ~ {cot_dates[-1]}")

    print("正在请求 stooq.com 周线数据...")
    price_map = fetch_stooq()
    print(f"  获取到 {len(price_map)} 条周线记录")

    weekly = []
    missing = 0
    for date in cot_dates:
        close = align_price(date, price_map)
        if close is None:
            missing += 1
        weekly.append({"date": date, "close": close})

    output = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "weekly": weekly,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"已保存 {len(weekly)} 条数据到 {OUT_PATH}（缺失 {missing} 周）")
    last = next((w for w in reversed(weekly) if w["close"] is not None), None)
    if last:
        print(f"最新一期：{last['date']}  金价收盘 ${last['close']:,.2f}")


if __name__ == "__main__":
    main()
