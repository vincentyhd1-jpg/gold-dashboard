#!/usr/bin/env python3
"""
从 CME 官方每日报告获取 COMEX 黄金库存数据，
追加到 data/stocks.json（保留近90天）。

使用 curl_cffi 模拟 Chrome TLS/HTTP2 指纹绕过 Akamai WAF。
文件为真实二进制 XLS（OLE2），用 xlrd 解析。
总计行结构：TOTAL REGISTERED / TOTAL ELIGIBLE / COMBINED TOTAL（无统一列，直接按行名取 col 7）。
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

GOLD_PAGE  = "https://www.cmegroup.com/markets/metals/precious/gold.html"
REPORT_URL = "https://www.cmegroup.com/delivery_reports/Gold_Stocks.xls"
OUT_PATH   = os.path.join(os.path.dirname(__file__), "data", "stocks.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer":         "https://www.cmegroup.com/markets/metals/precious/gold.html",
    "Accept":          "application/vnd.ms-excel,application/octet-stream,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Downloader ────────────────────────────────────────────────────────────────

def download() -> bytes | None:
    try:
        from curl_cffi import requests as cffi_requests
        print("  使用 curl_cffi (Chrome TLS 指纹)")
        session = cffi_requests.Session(impersonate="chrome124")

        try:
            pre = session.get(GOLD_PAGE, timeout=20)
            print(f"  预热请求：{pre.status_code}")
        except Exception as e:
            print(f"  预热失败（继续）：{e}")

        resp = session.get(REPORT_URL, headers=HEADERS, timeout=30)
        print(f"  XLS 请求状态码：{resp.status_code}")

        if resp.status_code == 200:
            return resp.content
        print(f"  ⚠ HTTP {resp.status_code}。响应前200字节：{resp.text[:200]}")
        return None

    except ImportError:
        print("  curl_cffi 未安装，回退到 requests")

    import requests
    session = requests.Session()
    session.headers.update({
        "User-Agent":      HEADERS["User-Agent"],
        "Accept-Language": HEADERS["Accept-Language"],
    })
    try:
        pre = session.get(GOLD_PAGE, headers={"Accept": "text/html,*/*"}, timeout=20)
        print(f"  预热请求：{pre.status_code}")
    except Exception as e:
        print(f"  预热失败（继续）：{e}")

    resp = session.get(REPORT_URL, headers=HEADERS, timeout=30)
    print(f"  XLS 请求状态码：{resp.status_code}")
    if resp.status_code == 200:
        return resp.content
    print(f"  ⚠ HTTP {resp.status_code}。响应前200字节：{resp.text[:200]}")
    return None


# ── Parser ────────────────────────────────────────────────────────────────────
# The XLS has this structure (no explicit REGISTERED/ELIGIBLE column header row):
#   Row 7:  ['GOLD', ..., 'Report Date: M/D/YYYY', ...]
#   Row 8:  ['Troy Ounce', ..., 'Activity Date: M/D/YYYY', ...]
#   Row 10: ['DEPOSITORY', ..., 'PREV TOTAL', ..., 'TOTAL TODAY', '']   ← headers
#   ...  individual depository rows (Registered / Eligible / Total per depot)
#   Row 131: ['TOTAL REGISTERED', '', prevTotal, ..., totalToday, '']
#   Row 132: ['TOTAL PLEDGED',    '', ...]
#   Row 133: ['TOTAL ELIGIBLE',   '', prevTotal, ..., totalToday, '']
#   Row 134: ['COMBINED TOTAL',   '', ...]
#
# Col 7 = "TOTAL TODAY" — that's what we want.

def _to_float(v) -> float:
    try:
        return float(str(v).replace(",", "").strip() or "0")
    except (ValueError, TypeError):
        return 0.0


def parse(content: bytes) -> dict:
    import xlrd
    wb = xlrd.open_workbook(file_contents=content)
    ws = wb.sheet_by_index(0)

    # Extract date from rows 7/8
    report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for ri in range(min(12, ws.nrows)):
        for ci in range(ws.ncols):
            cell = str(ws.cell_value(ri, ci))
            m = re.search(r"(?:Report|Activity)\s+Date:\s*(\d{1,2}/\d{1,2}/\d{4})", cell)
            if m:
                report_date = datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
                break

    # Find TOTAL TODAY column index from the header row
    total_col = 7   # default — confirmed from debug dump above
    for ri in range(ws.nrows):
        row = [str(ws.cell_value(ri, ci)).strip().upper() for ci in range(ws.ncols)]
        if "TOTAL TODAY" in row:
            total_col = row.index("TOTAL TODAY")
            print(f"  列头行 {ri}：TOTAL TODAY = col {total_col}")
            break

    registered = eligible = None
    for ri in range(ws.nrows):
        label = str(ws.cell_value(ri, 0)).strip().upper()
        if label == "TOTAL REGISTERED":
            registered = _to_float(ws.cell_value(ri, total_col))
            print(f"  行{ri} TOTAL REGISTERED = {registered:,.0f}")
        elif label == "TOTAL ELIGIBLE":
            eligible = _to_float(ws.cell_value(ri, total_col))
            print(f"  行{ri} TOTAL ELIGIBLE = {eligible:,.0f}")

    if registered is None or eligible is None:
        # debug dump
        for ri in range(ws.nrows):
            row = [str(ws.cell_value(ri, ci)) for ci in range(ws.ncols)]
            print(f"  行{ri}: {row}")
        raise ValueError(f"未找到 TOTAL REGISTERED/ELIGIBLE 行（registered={registered}, eligible={eligible}）")

    return {
        "date":       report_date,
        "registered": int(registered),
        "eligible":   int(eligible),
        "total":      int(registered + eligible),
    }


# ── Storage ───────────────────────────────────────────────────────────────────

def load_existing() -> list[dict]:
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    return []


def main():
    print("正在下载 CME COMEX 黄金库存报告...")
    content = download()

    if content is None:
        print("  CME WAF 封锁，跳过本次更新（stocks.json 保持不变）")
        sys.exit(0)

    print(f"  已下载 {len(content):,} 字节")

    entry = parse(content)

    print(f"  日期：{entry['date']}")
    print(f"  注册库存（Registered）：{entry['registered']:>12,} oz")
    print(f"  合格库存（Eligible）  ：{entry['eligible']:>12,} oz")
    print(f"  合计（Total）         ：{entry['total']:>12,} oz")

    records = load_existing()
    existing = {r["date"] for r in records}

    if entry["date"] in existing:
        print(f"  {entry['date']} 已存在，跳过写入")
        return

    records.append(entry)
    records = sorted(records, key=lambda r: r["date"])[-90:]

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"  已追加到 {OUT_PATH}，共 {len(records)} 条记录")


if __name__ == "__main__":
    main()
