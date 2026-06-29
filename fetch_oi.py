#!/usr/bin/env python3
"""
从 CME Section 62 PDF 提取 COMEX GC 各交割月明细：月份、结算价、持仓，
追加到 data/oi.json（保留近90天）。

oi.json 格式（每天一条）：
[{"date":"2026-06-26","months":[{"month":"AUG26","settle":4096.30,"oi":272518},...]},...]
"""

import io, json, os, re, sys
from datetime import datetime

PDF_URL  = "https://www.cmegroup.com/daily_bulletin/current/Section62_Metals_Futures_Products.pdf"
OUT_PATH = os.path.join(os.path.dirname(__file__), "data", "oi.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.cmegroup.com/",
    "Accept": "application/pdf,*/*",
}

MONTH_RE = re.compile(r'^([A-Z]{3}\d{2})\b')


def download() -> bytes | None:
    try:
        from curl_cffi import requests as cffi
        print("  curl_cffi (Chrome TLS 指纹)")
        r = cffi.Session(impersonate="chrome124").get(PDF_URL, headers=HEADERS, timeout=30)
        print(f"  HTTP {r.status_code}  {len(r.content):,} bytes")
        return r.content if r.status_code == 200 else None
    except ImportError:
        pass
    import urllib.request
    print("  urllib 回退")
    req = urllib.request.Request(PDF_URL, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        print(f"  HTTP 200  {len(data):,} bytes")
        return data
    except Exception as e:
        print(f"  下载失败：{e}")
        return None


def _parse_month_row(line: str) -> dict | None:
    """
    PDF 行格式（空格分隔）：
      MONTH [OPEN HIGH /LOW] SETTLE + CHG  VOLUME VOL_CHG  OI (+/-/UNCH) OI_CHG
    返回 {"month":str, "settle":float, "oi":int} 或 None
    """
    tokens = line.split()
    if not tokens or not MONTH_RE.match(tokens[0]):
        return None
    month = tokens[0]

    # 结算价 = 第一个 +/- 号之前的最后一个含小数点的 token
    settle = None
    first_sign = next((i for i, t in enumerate(tokens) if t in ('+', '-')), None)
    if first_sign and first_sign > 0:
        clean = re.sub(r'[A-Za-z/]', '', tokens[first_sign - 1])
        if '.' in clean:
            try:
                settle = float(clean)
            except ValueError:
                pass

    # 持仓 = 最后一个 +/-/UNCH 号之前的纯整数 token
    oi = None
    for i in range(len(tokens) - 1, -1, -1):
        if tokens[i] in ('+', '-', 'UNCH') and i > 0:
            prev = tokens[i - 1].replace(',', '')
            if prev.isdigit():
                oi = int(prev)
            break

    if settle is None or oi is None:
        return None
    return {"month": month, "settle": settle, "oi": oi}


def parse(content: bytes) -> dict:
    import pdfplumber

    date_str = None
    month_rows: list[dict] = []

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            text  = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            lines = text.splitlines()

            if date_str is None:
                m = re.search(
                    r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+(\w{3})\s+(\d{1,2}),\s+(\d{4})\b',
                    text)
                if m:
                    date_str = datetime.strptime(
                        f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y"
                    ).strftime("%Y-%m-%d")

            # 只从包含各月明细的页面提取（通常 page 2）
            in_gc = False
            for line in lines:
                upper = line.upper()
                if not in_gc:
                    if (re.search(r'GOLD\s+FUTURES', upper)
                            and not re.search(r'\b(MGC|MINI|1OZ|QO)\b', upper)):
                        in_gc = True
                    continue
                if re.search(r'(SILVER|COPPER|PLATINUM|PALLADIUM|MINI|MGC|ALUMINUM|ZINC)\s+(FUTURES|OPTIONS)', upper):
                    break
                if re.search(r'TOTAL\s+\S+\s+FUT', upper):
                    break
                row = _parse_month_row(line.strip())
                if row and row not in month_rows:
                    month_rows.append(row)

    if not month_rows:
        raise ValueError("未找到 GC 各月明细行")
    if date_str is None:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        print(f"  ⚠ 未解析到日期，使用今日 UTC：{date_str}")

    return {"date": date_str, "months": month_rows}


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

    print("解析中...")
    entry = parse(content)
    months = entry["months"]
    total_oi = sum(r["oi"] for r in months)
    front    = max(months, key=lambda r: r["oi"])
    print(f"  日期：{entry['date']}  共 {len(months)} 个交割月  总持仓：{total_oi:,}")
    print(f"  主力月：{front['month']}  结算价：{front['settle']:.2f}  持仓：{front['oi']:,}")
    for r in months:
        print(f"    {r['month']:<6}  settle={r['settle']:>9.2f}  oi={r['oi']:>8,}")

    records = load_existing()
    existing = {r["date"]: i for i, r in enumerate(records)}

    if entry["date"] in existing:
        idx = existing[entry["date"]]
        if records[idx].get("months"):
            print(f"  {entry['date']} 已存在，跳过写入")
            return
        records[idx] = entry
        print(f"  {entry['date']} 已存在但无明细，已更新")
    else:
        records.append(entry)

    records = sorted(records, key=lambda r: r["date"])[-90:]

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"  已写入 {OUT_PATH}，共 {len(records)} 条记录")


if __name__ == "__main__":
    main()
