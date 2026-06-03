#!/usr/bin/env python3
"""
从 CFTC Socrata API 获取黄金期货 COT 数据（代码 088691），
计算管理基金净多持仓、商业套保净持仓、COT Index，
保存到 data/cot.json。
"""

import json
import os
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.parse import urlencode

API_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
GOLD_CODE = "088691"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "data", "cot.json")


def fetch_api(limit: int = 52) -> list[dict]:
    params = urlencode({
        "cftc_contract_market_code": GOLD_CODE,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": limit,
    })
    url = f"{API_URL}?{params}"
    req = Request(url, headers={
        "User-Agent": "gold-dashboard/1.0",
        "Accept": "application/json",
    })
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_row(row: dict) -> dict | None:
    date = row.get("report_date_as_yyyy_mm_dd", "")
    if not date:
        return None
    # 日期字段有时带时间戳，截取前10位
    date = date[:10]

    def i(key: str) -> int:
        return int(float(row.get(key) or 0))

    mf_long  = i("m_money_positions_long_all")
    mf_short = i("m_money_positions_short_all")

    # 注意：API 实际字段名无 _all 后缀
    prod_long  = i("prod_merc_positions_long")
    prod_short = i("prod_merc_positions_short")
    swap_long  = i("swap_positions_long_all")
    # 注意：CFTC 字段名 swap 后有两个下划线
    swap_short = i("swap__positions_short_all")

    return {
        "date":     date,
        "mf_net":   mf_long - mf_short,
        "comm_net": (prod_long - prod_short) + (swap_long - swap_short),
        "open_interest": i("open_interest_all"),
    }


def cot_index(values: list[int], current: int) -> int:
    """百分位：(current - min) / (max - min) × 100，结果取整。"""
    mn, mx = min(values), max(values)
    if mx == mn:
        return 50
    return round((current - mn) / (mx - mn) * 100)


def main():
    print("正在请求 CFTC Socrata API...")
    raw_rows = fetch_api(limit=52)
    print(f"  获取到 {len(raw_rows)} 条记录")

    weekly = []
    for row in raw_rows:
        parsed = parse_row(row)
        if parsed:
            weekly.append(parsed)

    # API 返回降序，转为升序
    weekly.sort(key=lambda r: r["date"])

    if not weekly:
        raise RuntimeError("未解析到任何有效数据，请检查 API 响应")

    # COT Index（用全部52周数据计算）
    mf_vals   = [r["mf_net"]   for r in weekly]
    comm_vals = [r["comm_net"] for r in weekly]

    latest = weekly[-1]
    prev   = weekly[-2] if len(weekly) >= 2 else {}

    mf_index   = cot_index(mf_vals,   latest["mf_net"])
    comm_index = cot_index(comm_vals, latest["comm_net"])

    output = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latest": {
            "date":         latest["date"],
            "mf_net":       latest["mf_net"],
            "comm_net":     latest["comm_net"],
            "open_interest": latest["open_interest"],
            "mf_net_chg":   latest["mf_net"]   - prev.get("mf_net", 0),
            "comm_net_chg": latest["comm_net"] - prev.get("comm_net", 0),
            "mf_index":     mf_index,
            "comm_index":   comm_index,
        },
        "weekly": weekly,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"已保存 {len(weekly)} 条数据到 {OUTPUT_PATH}")
    print(
        f"最新一期：{latest['date']}"
        f"  管理基金净多={latest['mf_net']:+,}"
        f"  商业套保净={latest['comm_net']:+,}"
        f"  COT Index={mf_index}%"
    )


if __name__ == "__main__":
    main()
