#!/usr/bin/env python3
"""
从 CFTC 下载最新 COT 报告，提取黄金期货（代码 088691）的
管理基金和商业套保者净持仓，保存到 data/cot.json。
"""

import csv
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from urllib.request import urlopen, Request

GOLD_CODE = "088691"
CURRENT_URL = "https://www.cftc.gov/dea/newcot/deahistfo.zip"
HISTORY_URL = "https://www.cftc.gov/files/dea/history/deahistfo_{year}.zip"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "data", "cot.json")
WEEKS_TO_KEEP = 52  # 保留最近 52 周


def fetch_zip(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 gold-dashboard/1.0"})
    with urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_cot_csv(raw: bytes) -> list[dict]:
    """解析 deahistfo.txt，返回黄金期货的所有行（按日期升序）。"""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
        content = zf.read(name).decode("utf-8", errors="replace")

    rows = []
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        code = row.get("CFTC_Contract_Market_Code", "").strip()
        if code != GOLD_CODE:
            continue
        date_str = row.get("Report_Date_as_YYYY-MM-DD", "").strip()
        if not date_str:
            continue
        try:
            mm_long  = int(row["M_Money_Positions_Long_All"])
            mm_short = int(row["M_Money_Positions_Short_All"])
            cm_long  = int(row["Comm_Positions_Long_All"])
            cm_short = int(row["Comm_Positions_Short_All"])
        except (KeyError, ValueError):
            continue
        rows.append({
            "date":    date_str,
            "mf_net":  mm_long - mm_short,
            "comm_net": cm_long - cm_short,
        })

    rows.sort(key=lambda r: r["date"])
    return rows


def load_existing() -> list[dict]:
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("weekly", [])
    return []


def merge(existing: list[dict], new_rows: list[dict]) -> list[dict]:
    """以 date 为键合并，新数据覆盖旧数据，保留最近 WEEKS_TO_KEEP 条。"""
    by_date = {r["date"]: r for r in existing}
    for r in new_rows:
        by_date[r["date"]] = r
    merged = sorted(by_date.values(), key=lambda r: r["date"])
    return merged[-WEEKS_TO_KEEP:]


def main():
    print("正在下载当前 COT 数据...")
    raw = fetch_zip(CURRENT_URL)
    new_rows = parse_cot_csv(raw)
    print(f"  解析到 {len(new_rows)} 条黄金期货记录")

    # 如果本地数据不足 WEEKS_TO_KEEP 条，补充历史年份
    existing = load_existing()
    if len(existing) < WEEKS_TO_KEEP:
        current_year = datetime.now().year
        for year in range(current_year - 1, current_year - 4, -1):
            if len(existing) + len(new_rows) >= WEEKS_TO_KEEP:
                break
            hist_url = HISTORY_URL.format(year=year)
            print(f"  补充历史数据：{hist_url}")
            try:
                hist_raw = fetch_zip(hist_url)
                hist_rows = parse_cot_csv(hist_raw)
                new_rows = hist_rows + new_rows
                print(f"    +{len(hist_rows)} 条")
            except Exception as e:
                print(f"    跳过 {year}：{e}")

    weekly = merge(existing, new_rows)

    latest = weekly[-1] if weekly else {}
    prev   = weekly[-2] if len(weekly) >= 2 else {}

    output = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latest": {
            "date":         latest.get("date", ""),
            "mf_net":       latest.get("mf_net", 0),
            "comm_net":     latest.get("comm_net", 0),
            "mf_net_chg":   latest.get("mf_net", 0) - prev.get("mf_net", 0),
            "comm_net_chg": latest.get("comm_net", 0) - prev.get("comm_net", 0),
        },
        "weekly": weekly,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"已保存 {len(weekly)} 条数据到 {OUTPUT_PATH}")
    print(f"最新一期：{latest.get('date')}  管理基金净多={latest.get('mf_net'):+,}  商业套保净={latest.get('comm_net'):+,}")


if __name__ == "__main__":
    main()
