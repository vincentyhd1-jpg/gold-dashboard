#!/usr/bin/env python3
"""
获取黄金期货（GC=F）周线收盘价，与 COT 报告日期（每周二）对齐，
保存到 data/gold_price.json，格式：[{"date":"YYYY-MM-DD","price":XXXX.XX}, ...]

数据源优先级：
1. Stooq https://stooq.com/q/d/l/?s=xauusd&i=w （CSV，无需 API key）
2. Yahoo Finance GC=F 周线 JSON（无需 API key）
"""

import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

COT_PATH = os.path.join(os.path.dirname(__file__), "data", "cot.json")
OUT_PATH  = os.path.join(os.path.dirname(__file__), "data", "gold_price.json")

STOOQ_URL = "https://stooq.com/q/d/l/?s=xauusd&i=w"
YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF"
    "?interval=1wk&range=2y"
)


def fetch_stooq() -> dict[str, float]:
    """Return {date_str: close_price} from Stooq CSV (xauusd, weekly)."""
    req = urllib.request.Request(STOOQ_URL, headers={
        "User-Agent": "gold-dashboard/1.0",
        "Accept":     "text/csv,text/plain,*/*",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8").strip()

    if text.startswith("Get your apikey") or "<html" in text.lower():
        raise ValueError(f"Stooq returned non-CSV response: {text[:120]}")

    prices: dict[str, float] = {}
    reader = csv.DictReader(text.splitlines())
    for row in reader:
        date      = row.get("Date",  "").strip()
        close_str = row.get("Close", "").strip()
        if date and close_str:
            try:
                prices[date] = round(float(close_str), 2)
            except ValueError:
                pass
    if not prices:
        raise ValueError("Stooq returned empty price data")
    return prices


def fetch_yahoo() -> dict[str, float]:
    """Return {date_str: close_price} from Yahoo Finance chart API (GC=F, weekly).

    Yahoo weekly bars are dated on the Monday that opens each week (UTC-4/EDT).
    """
    req = urllib.request.Request(YAHOO_URL, headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    result_obj = data["chart"]["result"][0]
    timestamps  = result_obj["timestamp"]
    closes      = result_obj["indicators"]["quote"][0]["close"]

    prices: dict[str, float] = {}
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        prices[date_str] = round(float(close), 2)
    if not prices:
        raise ValueError("Yahoo returned empty price data")
    return prices


def align_price(cot_date_str: str, price_map: dict[str, float]):
    """
    For a COT Tuesday date, find the corresponding weekly bar.

    Stooq  bars close on Friday  → try Fri (+3), Thu (+2), Wed (+1), Tue (0).
    Yahoo  bars open  on Monday  → try Mon (-1) first, then Tue (0).
    The combined probe order covers both sources.
    """
    tuesday = datetime.strptime(cot_date_str, "%Y-%m-%d")
    for delta in (-1, 3, 2, 1, 0):   # Mon (Yahoo) | Fri→Wed (Stooq) | Tue
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

    price_map: dict[str, float] = {}
    source = ""

    try:
        print("尝试 Stooq (xauusd) ...")
        price_map = fetch_stooq()
        source    = "stooq.com"
        print(f"  成功，获取 {len(price_map)} 条记录")
    except Exception as e:
        print(f"  Stooq 失败: {e}")
        try:
            print("尝试 Yahoo Finance (GC=F) ...")
            price_map = fetch_yahoo()
            source    = "Yahoo Finance (GC=F)"
            print(f"  成功，获取 {len(price_map)} 条记录")
        except Exception as e2:
            print(f"  Yahoo 失败: {e2}")
            print("ERROR: 所有数据源均失败，终止。")
            sys.exit(1)

    result = []
    missing = 0
    for date in cot_dates:
        price = align_price(date, price_map)
        if price is None:
            missing += 1
        result.append({"date": date, "price": price})

    recent = [r for r in result if r["price"] is not None][-5:]
    print(f"\n最新 5 条数据（来源：{source}）：")
    for r in recent:
        print(f"  {r['date']}  ${r['price']:,.2f}")
    print(f"\n共 {len(result)} 条，缺失 {missing} 周")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已保存至 {OUT_PATH}")


if __name__ == "__main__":
    main()
