#!/usr/bin/env python3
"""
从 CME 官方每日报告获取 COMEX 黄金库存数据，
追加到 data/stocks.json（保留近90天）。

使用 requests + session cookie 预热绕过 WAF。
CME 返回的 .xls 实际上是 HTML 格式，用 html.parser 解析。
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser

import requests

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
    session = requests.Session()
    session.headers.update({
        "User-Agent": HEADERS["User-Agent"],
        "Accept-Language": HEADERS["Accept-Language"],
    })

    # Pre-warm: visit the gold page to acquire session cookies
    try:
        pre = session.get(
            GOLD_PAGE,
            headers={"Accept": "text/html,application/xhtml+xml,*/*"},
            timeout=20,
            allow_redirects=True,
        )
        print(f"  预热请求：{pre.status_code}，cookies：{dict(session.cookies)}")
    except Exception as e:
        print(f"  预热请求失败（继续）：{e}")

    # Now fetch the XLS
    resp = session.get(REPORT_URL, headers=HEADERS, timeout=30, allow_redirects=True)
    print(f"  XLS 请求状态码：{resp.status_code}")

    if resp.status_code == 403:
        print(f"  ⚠ HTTP 403 被拒绝。响应前200字节：{resp.text[:200]}")
        return None

    if resp.status_code != 200:
        print(f"  ⚠ HTTP {resp.status_code}，响应前200字节：{resp.text[:200]}")
        return None

    return resp.content


# ── HTML parser ───────────────────────────────────────────────────────────────

class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row  = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = ""

    def handle_endtag(self, tag):
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(self._cell.strip())
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell += data

    def handle_entityref(self, name):
        if self._cell is not None:
            self._cell += " "

    def handle_charref(self, name):
        if self._cell is not None:
            self._cell += " "


def _clean_num(s: str) -> int:
    s = re.sub(r"[,\s\xa0]", "", s)
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _find_col(rows: list[list[str]], keyword: str) -> int:
    for row in rows:
        for i, cell in enumerate(row):
            if keyword.upper() in cell.upper():
                return i
    return -1


def _find_date(text: str) -> str:
    m = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", text)
    if m:
        return datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def parse(content: bytes) -> dict:
    # CME XLS files are usually HTML in disguise
    if content[:1] in (b"<", b"\xef", b"\xff", b"\xfe"):
        text = content.decode("utf-8", errors="replace")
        print(f"  文件类型：HTML（CME 伪 XLS），前80字节：{text[:80]!r}")
        return _parse_html(text)

    # Try xlrd for real binary XLS
    try:
        import xlrd  # type: ignore
        wb = xlrd.open_workbook(file_contents=content)
        print("  文件类型：二进制 XLS，使用 xlrd 解析")
        return _parse_xlrd(wb)
    except Exception as e:
        # Last resort: try decoding as text anyway
        text = content.decode("utf-8", errors="replace")
        print(f"  xlrd 失败（{e}），尝试文本解析，前80字节：{text[:80]!r}")
        return _parse_html(text)


def _parse_html(text: str) -> dict:
    report_date = _find_date(text)

    p = _TableParser()
    p.feed(text)

    reg_col = _find_col(p.rows, "REGISTERED")
    eli_col = _find_col(p.rows, "ELIGIBLE")

    if reg_col < 0 or eli_col < 0:
        preview = "\n".join(str(r) for r in p.rows[:10])
        raise ValueError(f"未找到 REGISTERED/ELIGIBLE 列头。\n前10行：\n{preview}")

    # Find TOTAL row
    for row in p.rows:
        if row and "TOTAL" in row[0].upper():
            if len(row) > max(reg_col, eli_col):
                registered = _clean_num(row[reg_col])
                eligible   = _clean_num(row[eli_col])
                if registered > 0 or eligible > 0:
                    return {
                        "date":       report_date,
                        "registered": registered,
                        "eligible":   eligible,
                        "total":      registered + eligible,
                    }

    # Fallback: any row containing TOTAL anywhere
    for row in p.rows:
        if any("TOTAL" in cell.upper() for cell in row):
            nums = [_clean_num(c) for c in row
                    if re.search(r"\d{4,}", re.sub(r"[,\s]", "", c))]
            if len(nums) >= 2:
                registered, eligible = nums[0], nums[1]
                return {
                    "date":       report_date,
                    "registered": registered,
                    "eligible":   eligible,
                    "total":      registered + eligible,
                }

    preview = "\n".join(str(r) for r in p.rows)
    raise ValueError(f"未找到 TOTAL 行。全部行：\n{preview}")


def _parse_xlrd(wb) -> dict:
    ws  = wb.sheet_by_index(0)
    report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for ri in range(min(5, ws.nrows)):
        for ci in range(ws.ncols):
            m = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", str(ws.cell_value(ri, ci)))
            if m:
                report_date = datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")

    reg_col = eli_col = -1
    for ri in range(ws.nrows):
        for ci in range(ws.ncols):
            v = str(ws.cell_value(ri, ci)).upper()
            if "REGISTERED" in v and reg_col < 0:
                reg_col = ci
            if "ELIGIBLE" in v and eli_col < 0:
                eli_col = ci
        if reg_col >= 0 and eli_col >= 0:
            break

    if reg_col < 0 or eli_col < 0:
        raise ValueError("XLS：未找到 REGISTERED/ELIGIBLE 列头")

    for ri in range(ws.nrows):
        if "TOTAL" in str(ws.cell_value(ri, 0)).upper():
            registered = int(ws.cell_value(ri, reg_col) or 0)
            eligible   = int(ws.cell_value(ri, eli_col) or 0)
            return {
                "date":       report_date,
                "registered": registered,
                "eligible":   eligible,
                "total":      registered + eligible,
            }

    raise ValueError("XLS：未找到 TOTAL 行")


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
