#!/usr/bin/env python3
"""
从 CME Section 62 PDF 提取 COMEX GC 标准黄金期货总持仓（Open Interest），
追加到 data/oi.json（保留近90天）。

PDF 地址每日固定：
  https://www.cmegroup.com/daily_bulletin/current/Section62_Metals_Futures_Products.pdf

目标行：TOTAL GC FUT  132978  2969  362192 + 1632
  - 最大正整数(>10000) = 总持仓 OI
  - 符号后跟的数字（"+ 1632" 或 "- 500"）= 较前日变化
"""

import json
import os
import re
import sys
from datetime import datetime

PDF_URL  = "https://www.cmegroup.com/daily_bulletin/current/Section62_Metals_Futures_Products.pdf"
OUT_PATH = os.path.join(os.path.dirname(__file__), "data", "oi.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.cmegroup.com/",
    "Accept":  "application/pdf,*/*",
}


def download() -> bytes | None:
    # 优先使用 curl_cffi（绕过 WAF）
    try:
        from curl_cffi import requests as cffi_requests
        print("  使用 curl_cffi (Chrome TLS 指纹)")
        session = cffi_requests.Session(impersonate="chrome124")
        resp = session.get(PDF_URL, headers=HEADERS, timeout=30)
        print(f"  状态码：{resp.status_code}")
        if resp.status_code == 200:
            return resp.content
        print(f"  ⚠ HTTP {resp.status_code}")
        return None
    except ImportError:
        pass

    import urllib.request
    print("  回退到 urllib")
    req = urllib.request.Request(PDF_URL, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        print(f"  状态码：200，{len(data):,} 字节")
        return data
    except Exception as e:
        print(f"  下载失败：{e}")
        return None


def parse(content: bytes) -> dict:
    import pdfplumber, io

    date_str = None
    oi_total = None
    oi_chg   = None

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""

            # 公告日期："Fri, Jun 26, 2026"
            if date_str is None:
                m = re.search(
                    r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+'
                    r'(\w{3})\s+(\d{1,2}),\s+(\d{4})\b',
                    text
                )
                if m:
                    date_str = datetime.strptime(
                        f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y"
                    ).strftime("%Y-%m-%d")

            for line in text.splitlines():
                upper = line.upper()
                if "TOTAL" not in upper or not re.search(r'\bGC\b', upper):
                    continue
                if re.search(r'\b(MGC|QO|1OZ)\b', upper):
                    continue  # 跳过微型/衍生合约

                parts = line.split()
                # 总持仓 = 最大正整数 > 10000
                positives = [int(p.replace(',', '')) for p in parts
                             if p.replace(',', '').isdigit()
                             and int(p.replace(',', '')) > 10000]
                if positives:
                    oi_total = max(positives)

                # 变化：空格分隔的 "+ 1632" 或连写的 "+1632" / "-500"
                for j, tok in enumerate(parts):
                    if tok in ('+', '-') and j + 1 < len(parts):
                        num = parts[j + 1].replace(',', '')
                        if num.isdigit():
                            oi_chg = int(num) * (1 if tok == '+' else -1)
                            break
                    elif re.match(r'^[+-]\d', tok):
                        num = tok[1:].replace(',', '')
                        if num.isdigit():
                            oi_chg = int(num) * (1 if tok[0] == '+' else -1)
                            break

                if oi_total is not None:
                    print(f"  [p{page_num}] TOTAL GC FUT: OI={oi_total:,}  chg={oi_chg:+,}" if oi_chg is not None
                          else f"  [p{page_num}] TOTAL GC FUT: OI={oi_total:,}  chg=N/A")
                    break
            if oi_total is not None:
                break

    if oi_total is None:
        raise ValueError("未找到 TOTAL GC FUT 行")
    if date_str is None:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        print(f"  ⚠ 未解析到日期，使用今日 UTC：{date_str}")

    return {"date": date_str, "oi": oi_total, "chg": oi_chg}


def load_existing() -> list[dict]:
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    return []


def main():
    print("正在下载 CME Section 62 PDF...")
    content = download()
    if content is None:
        print("  下载失败，跳过更新")
        sys.exit(0)
    print(f"  已下载 {len(content):,} 字节")

    print("解析中...")
    entry = parse(content)
    print(f"  日期：{entry['date']}  OI：{entry['oi']:,}  变化：{entry['chg']:+,}" if entry['chg'] is not None
          else f"  日期：{entry['date']}  OI：{entry['oi']:,}  变化：N/A")

    records = load_existing()
    existing = {r["date"]: i for i, r in enumerate(records)}

    if entry["date"] in existing:
        idx = existing[entry["date"]]
        if records[idx].get("oi"):
            print(f"  {entry['date']} 已存在，跳过写入")
            return
        records[idx] = entry
        print(f"  {entry['date']} 已存在但缺 OI，已更新")
    else:
        records.append(entry)

    records = sorted(records, key=lambda r: r["date"])[-90:]

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"  已写入 {OUT_PATH}，共 {len(records)} 条记录")


if __name__ == "__main__":
    main()
