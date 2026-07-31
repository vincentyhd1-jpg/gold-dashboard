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
QUARANTINE_DIR = os.path.join(os.path.dirname(__file__), "data", "quarantine")

# 缺失比例上限。单周对齐失败（±3 天窗口内无 bar）是正常态，
# 超过此比例说明数据源时间轴与 COT 系统性错位。
MAX_MISSING_RATIO = 0.2

# 金价合理区间（USD/oz）。解析错列会取到成交量之类的数量级。
PRICE_MIN = 100.0
PRICE_MAX = 100000.0

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


# ── 采集层校验 ────────────────────────────────────────────────────────────────
#
# 堵「对齐失败静默归 null」：现状 missing 只 print 不拦截，52 周全部对齐失败
# 也会正常落盘 exit 0。单周 null 是正常态（±3 天窗口内无 bar），
# 但全 null / 高比例 null / 最新一期 null 都是真损坏。

def validate_gold(result: list[dict]) -> list[str]:
    """返回失败原因列表；空列表表示通过。"""
    failures: list[str] = []
    if not result:
        return ["a) result 为空 —— 无 COT 日期可对齐"]

    n = len(result)
    prices = [r.get("price") for r in result]
    missing = sum(1 for p in prices if p is None)
    non_null = [p for p in prices if p is not None]

    # a) 全部对齐失败 —— 日期格式或对齐逻辑坏了
    if missing == n:
        failures.append(
            f"a) 全部 {n} 周对齐失败（price 全为 null）"
            f" —— 日期格式或对齐窗口逻辑异常"
        )
        return failures   # 后续判据无数据可查

    # b) 缺失比例过高 —— 时间轴系统性错位
    ratio = missing / n
    if ratio > MAX_MISSING_RATIO:
        failures.append(
            f"b) 缺失 {missing}/{n} 周（{ratio:.1%}），超过上限 "
            f"{MAX_MISSING_RATIO:.0%} —— 数据源时间轴与 COT 系统性错位"
        )

    # c) 最新一期缺失 —— 所有 KPI 卡的数据源
    if prices[-1] is None:
        failures.append(
            f"c) 最新一期 {result[-1]['date']} 的 price 为 null"
            f" —— 页面 KPI 将显示 --"
        )

    # d) 价格越界 —— 解析错列
    bad = [(r["date"], r["price"]) for r in result
           if r.get("price") is not None
           and not (PRICE_MIN <= r["price"] <= PRICE_MAX)]
    if bad:
        failures.append(
            f"d) {len(bad)} 周价格越界（合理区间 "
            f"{PRICE_MIN:.0f}~{PRICE_MAX:.0f}）：{bad[:3]}"
            f" —— 可能取到了成交量等其他列"
        )

    # e) 价格序列退化 —— 取到了固定列
    if len(set(non_null)) == 1:
        failures.append(
            f"e) 全部 {len(non_null)} 个非 null 价格均为 {non_null[0]}"
            f" —— 可能取到了常量列"
        )

    return failures


def quarantine_gold(result: list[dict], raw_note: str,
                    failures: list[str]) -> str:
    """坏数据存入隔离区，data/gold_price.json 保持上一份不被覆盖。"""
    os.makedirs(QUARANTINE_DIR, exist_ok=True)
    stamp = (result[-1]["date"] if result
             else datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    path = os.path.join(QUARANTINE_DIR, f"gold-{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
            "reason":         failures,
            "source":         raw_note,
            "parsed_result":  result,
        }, f, ensure_ascii=False, indent=2)
    return path


def main():
    if "--test" in sys.argv:
        run_tests()
        return

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

    # ── 校验：坏数据不得写入 gold_price.json ───────────────────────────
    print("校验中...")
    failures = validate_gold(result)
    if failures:
        path = quarantine_gold(result, source, failures)
        print("  校验失败，已隔离，data/gold_price.json 保持上一份可用数据：")
        for f in failures:
            print(f"    - {f}")
        print(f"  隔离区：{path}")
        print(f"::error title=金价采集校验失败::"
              f"未通过校验，已隔离；gold_price.json 未更新。"
              f"原因：{'；'.join(failures)}")
        sys.exit(1)
    print("  5 项校验通过（全 null / 缺失比例 / 最新一期 / 价格区间 / 序列退化）")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已保存至 {OUT_PATH}")


# ── 校验逻辑单元测试 ──────────────────────────────────────────────────────────

def run_tests():
    """python fetch_gold.py --test（不联网）"""
    passed = failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}  {detail}")

    def series(prices):
        base = datetime(2025, 7, 29)
        return [{"date": (base + timedelta(days=7 * i)).strftime("%Y-%m-%d"),
                 "price": p} for i, p in enumerate(prices)]

    good = series([4000.0 + i for i in range(52)])

    print("\n[validate_gold]")
    check("正常 52 周 → 无失败",
          validate_gold(good) == [], validate_gold(good))

    # a) 全 null
    f = validate_gold(series([None] * 52))
    check("a) 52 周 price 全 null → 命中",
          any(x.startswith("a)") for x in f), f)

    # b) 缺失比例过高：15/52 = 28.8%
    p = [4000.0 + i for i in range(52)]
    for i in range(15):
        p[i] = None
    f = validate_gold(series(p))
    check("b) 缺失 15/52（28.8%）→ 命中",
          any(x.startswith("b)") for x in f), f)

    # c) 仅最新一期缺失
    p = [4000.0 + i for i in range(52)]
    p[-1] = None
    f = validate_gold(series(p))
    check("c) 仅最新一期 null → 命中",
          any(x.startswith("c)") for x in f), f)

    # d) 价格越界
    for bad in (0.0, -100.0, 999999.0):
        p = [4000.0 + i for i in range(52)]
        p[10] = bad
        f = validate_gold(series(p))
        check(f"d) 价格 {bad} 越界 → 命中",
              any(x.startswith("d)") for x in f), f)

    # e) 序列退化：全为同一值
    f = validate_gold(series([4000.0] * 52))
    check("e) 52 周价格全为 4000.0 → 命中",
          any(x.startswith("e)") for x in f), f)

    # e) 边界：3 个唯一值不该误杀（修正2）
    f = validate_gold(series([4000.0, 4100.0, 4200.0] * 17 + [4000.0]))
    check("e) 3 个唯一值的价格序列 → 不命中（不误杀）",
          not any(x.startswith("e)") for x in f), f)

    # e) 边界：2 个唯一值也不该命中 —— 退化的定义是「只剩 1 个值」
    p = [4000.0] * 52
    p[0] = 4100.0
    f = validate_gold(series(p))
    check("e) 2 个唯一值 → 不命中",
          not any(x.startswith("e)") for x in f), f)

    # 正常态：少量缺失且最新一期有值
    p = [4000.0 + i for i in range(52)]
    p[5] = p[20] = p[33] = None
    f = validate_gold(series(p))
    check("3/52 缺失且最新一期有值 → 无失败（正常态，不误杀）", f == [], f)

    check("空 result → 命中 a)",
          any(x.startswith("a)") for x in validate_gold([])))

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
