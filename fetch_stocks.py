#!/usr/bin/env python3
"""
从 CME 官方每日报告获取 COMEX 黄金库存数据，
追加到 data/stocks.json（保留近90天）。

使用 curl_cffi 模拟 Chrome TLS/HTTP2 指纹绕过 Akamai WAF。
文件为真实二进制 XLS（OLE2），用 xlrd 解析。
总计行结构：TOTAL REGISTERED / TOTAL ELIGIBLE / COMBINED TOTAL（直接按行名取 TOTAL TODAY 列）。
depositories：各仓库明细，ENHANCED 子库并入主库累加。
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
# XLS structure (confirmed from debug dump):
#   Row 7:  ['GOLD', ..., 'Report Date: M/D/YYYY', ...]
#   Row 8:  ['Troy Ounce', ..., 'Activity Date: M/D/YYYY', ...]
#   Row 10: ['DEPOSITORY', ..., 'ADJUSTMENT', 'PREV TOTAL', ..., 'TOTAL TODAY', '']  ← col header
#           col 6 = ADJUSTMENT, col 7 = TOTAL TODAY
#
#   Depot block pattern (col 0 indented with leading spaces):
#     '  Brink's Inc'          ← depot name row (no numeric data)
#     '    Registered'         ← registered row
#     '    Eligible'           ← eligible row
#     '    Total'              ← total row  (we skip, compute ourselves)
#     '  Brink's Inc - ENHANCED'  ← optional sub-depot (merge into parent)
#     ...
#
#   Grand totals (col 0, NO leading spaces):
#     'TOTAL REGISTERED'
#     'TOTAL PLEDGED'
#     'TOTAL ELIGIBLE'
#     'COMBINED TOTAL'
#
# Col 6 = ADJUSTMENT, Col 7 = TOTAL TODAY (both confirmed).

# Normalise a depot name: strip leading/trailing space, collapse internal
# whitespace, remove " - ENHANCED" suffix so it merges into the parent.
_ENHANCED = re.compile(r"\s*-\s*ENHANCED\b.*$", re.IGNORECASE)

def _norm_depot(raw: str) -> str:
    return _ENHANCED.sub("", raw.strip()).strip()


def _to_float(v) -> float:
    try:
        return float(str(v).replace(",", "").strip() or "0")
    except (ValueError, TypeError):
        return 0.0


def parse(content: bytes) -> dict:
    import xlrd
    wb = xlrd.open_workbook(file_contents=content)
    ws = wb.sheet_by_index(0)

    # ── date ──────────────────────────────────────────────────────────────────
    report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for ri in range(min(12, ws.nrows)):
        for ci in range(ws.ncols):
            cell = str(ws.cell_value(ri, ci))
            m = re.search(r"(?:Report|Activity)\s+Date:\s*(\d{1,2}/\d{1,2}/\d{4})", cell)
            if m:
                report_date = datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
                break

    # ── locate ADJUSTMENT and TOTAL TODAY columns from header row ─────────────
    adj_col   = 6   # default
    total_col = 7   # default
    for ri in range(ws.nrows):
        row = [str(ws.cell_value(ri, ci)).strip().upper() for ci in range(ws.ncols)]
        if "TOTAL TODAY" in row:
            total_col = row.index("TOTAL TODAY")
            # ADJUSTMENT is typically one column to the left of TOTAL TODAY
            if "ADJUSTMENT" in row:
                adj_col = row.index("ADJUSTMENT")
            else:
                adj_col = total_col - 1
            print(f"  列头行 {ri}：ADJUSTMENT=col{adj_col}, TOTAL TODAY=col{total_col}")
            break

    # ── grand totals ──────────────────────────────────────────────────────────
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
        for ri in range(ws.nrows):
            print(f"  行{ri}: {[str(ws.cell_value(ri, ci)) for ci in range(ws.ncols)]}")
        raise ValueError(
            f"未找到 TOTAL REGISTERED/ELIGIBLE 行（registered={registered}, eligible={eligible}）"
        )

    # ── per-depot detail ──────────────────────────────────────────────────────
    # Walk rows between the header row (~10) and the grand-total block.
    # A depot NAME row has leading spaces in col 0 and no numeric value in total_col.
    # Its sub-rows ('    Registered', '    Eligible') are indented further.
    #
    # We accumulate into a dict keyed by normalised depot name so ENHANCED
    # sub-depots fold into their parent automatically.

    depots: dict[str, dict] = {}   # name -> {registered, eligible, reg_adj}
    current_depot: str | None = None

    for ri in range(ws.nrows):
        raw_label = str(ws.cell_value(ri, 0))
        label_stripped = raw_label.strip()
        label_upper    = label_stripped.upper()

        # Stop at grand-total block
        if label_upper in ("TOTAL REGISTERED", "TOTAL ELIGIBLE", "COMBINED TOTAL",
                           "TOTAL PLEDGED"):
            break

        # Skip header rows and blank rows
        if not label_stripped or label_upper in ("DEPOSITORY", "GOLD", "TROY OUNCE"):
            continue

        total_val = _to_float(ws.cell_value(ri, total_col))
        adj_val   = _to_float(ws.cell_value(ri, adj_col))

        # Depot name rows: indented once (e.g. '  Brink\'s Inc')
        # Sub-rows: indented twice (e.g. '    Registered')
        leading = len(raw_label) - len(raw_label.lstrip(" "))

        if leading <= 2 and label_upper not in ("REGISTERED", "ELIGIBLE", "TOTAL",
                                                 "PLEDGED", "NET CHANGE"):
            # This is a depot name (or ENHANCED variant)
            current_depot = _norm_depot(label_stripped)
            if current_depot not in depots:
                depots[current_depot] = {"registered": 0, "eligible": 0, "reg_adj": 0}

        elif current_depot is not None:
            if label_upper == "REGISTERED":
                depots[current_depot]["registered"] += int(total_val)
                depots[current_depot]["reg_adj"]    += int(adj_val)
            elif label_upper == "ELIGIBLE":
                depots[current_depot]["eligible"]   += int(total_val)

    # Build output list, skip any depot with all zeros (artefact rows)
    depositories = []
    for name, vals in depots.items():
        reg = vals["registered"]
        eli = vals["eligible"]
        if reg == 0 and eli == 0:
            continue
        depositories.append({
            "name":       name,
            "registered": reg,
            "eligible":   eli,
            "total":      reg + eli,
            "reg_adj":    vals["reg_adj"],
        })

    print(f"  解析到 {len(depositories)} 个仓库")

    return {
        "date":         report_date,
        "registered":   int(registered),
        "eligible":     int(eligible),
        "total":        int(registered + eligible),
        "depositories": depositories,
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
    print("  各仓库明细（depositories）：")
    print(json.dumps(entry.get("depositories", []), ensure_ascii=False, indent=4))

    records = load_existing()
    existing_dates = {r["date"] for r in records}

    if entry["date"] in existing_dates:
        # Update existing record if it's missing depositories
        for r in records:
            if r["date"] == entry["date"] and "depositories" not in r:
                r["depositories"] = entry["depositories"]
                print(f"  {entry['date']} 已存在但缺少明细，已补写 depositories")
                break
        else:
            print(f"  {entry['date']} 已存在且有明细，跳过写入")
            return
    else:
        records.append(entry)
    records = sorted(records, key=lambda r: r["date"])[-90:]

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"  已追加到 {OUT_PATH}，共 {len(records)} 条记录")


if __name__ == "__main__":
    main()
