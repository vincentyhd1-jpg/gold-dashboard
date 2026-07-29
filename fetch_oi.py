#!/usr/bin/env python3
"""
从 CME Section 62 PDF 提取 COMEX GC 各交割月明细：月份、结算价、持仓，
追加到 data/oi.json（保留近 730 条）。

oi.json 格式（每天一条）：
[{"date":"2026-06-26","months":[{"month":"AUG26","settle":4096.30,"oi":272518},...]},...]

写入前有四条校验（见 validate()），任一命中则：
  - 坏数据 + 原始 PDF 存入 data/quarantine/
  - 不覆盖 data/oi.json（保留上一份可用数据）
  - exit 1

拦截点放在采集层而非派生层：坏数据一旦进 oi.json，既会显示在页面上，也会成为
下一交易日差分（oi[t] - oi[t-1]）的输入，把错误传播到后续所有帧。

Run:  python fetch_oi.py
Test: python fetch_oi.py --test   （只测校验逻辑，不联网）
"""

import io, json, os, re, sys
from datetime import datetime, timezone, date as date_cls

from trading_calendar import (
    is_trading_day, is_calendar_covered,
    prev_trading_day, latest_trading_day_on_or_before,
    trading_days_between,
)

PDF_URL  = "https://www.cmegroup.com/daily_bulletin/current/Section62_Metals_Futures_Products.pdf"
OUT_PATH = os.path.join(os.path.dirname(__file__), "data", "oi.json")
QUARANTINE_DIR = os.path.join(os.path.dirname(__file__), "data", "quarantine")

# CME 在 T+1 美东早间发布 T 日的 Section 62 公报，所以 "current" 文件里
# 永远是「上一个交易日」的数据，不是当天的。
#
# 这一点由 oi.json 的 20 次历史运行实测确认，无一例外：
#   运行 2026-07-28 Tue 23:09 UTC → Trade Date 2026-07-27 Mon
#   运行 2026-07-24 Fri 23:09 UTC → Trade Date 2026-07-23 Thu
#   运行 2026-07-25 Sat 19:12 UTC → Trade Date 2026-07-24 Fri
#   运行 2026-07-03（假日）23:13 UTC → Trade Date 2026-07-02 Thu
# 若把预期值定为「当天」，工作日会 4/5 误判，每天隔离好数据 —— 比原问题更糟。
PUBLISH_HOUR_UTC = 14  # ≈ 8am ET，含夏令时波动留出余量

# 允许的滞后交易日数。发布时刻本身已由 expected_trade_date 建模，这里再放余量
# 就是双重宽容，所以默认 0：Trade Date 必须精确等于预期交易日。
#
# 代价是 CME 发布延迟或假日表缺项会造成误隔离 —— 但那正是要的行为：显式报错
# 并指向具体原因，而不是静默让陈旧数据顶上。
MAX_STALE_TRADING_DAYS = 0

# 交割月数量相对上一交易日的允许波动。超出即视为 PDF 结构性改版或解析错位。
# 实测正常波动为 ±1（远月挂牌/到期，27↔28）。
MAX_CONTRACT_COUNT_DELTA = 3

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

    # 持仓 & 持仓变化 = 最后一个 +/-/UNCH 两侧的整数
    oi = None
    oi_chg = None
    for i in range(len(tokens) - 1, -1, -1):
        if tokens[i] in ('+', '-', 'UNCH') and i > 0:
            prev = tokens[i - 1].replace(',', '')
            if prev.isdigit():
                oi = int(prev)
            if tokens[i] == 'UNCH':
                oi_chg = 0
            elif i + 1 < len(tokens):
                chg_tok = tokens[i + 1].replace(',', '')
                if chg_tok.isdigit():
                    oi_chg = int(chg_tok) if tokens[i] == '+' else -int(chg_tok)
                else:
                    oi_chg = 0
            else:
                oi_chg = 0
            break

    if settle is None or oi is None:
        return None
    return {"month": month, "settle": settle, "oi": oi, "oi_chg": oi_chg or 0}


def parse(content: bytes) -> dict:
    import pdfplumber

    date_str = None
    month_rows: list[dict] = []

    in_gc = False
    done  = False

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            if done:
                break
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

            # in_gc 跨页保持，捕获跨页的远月行
            for line in lines:
                upper = line.upper()
                if not in_gc:
                    if (re.search(r'GOLD\s+FUTURES', upper)
                            and not re.search(r'\b(MGC|MINI|1OZ|QO)\b', upper)):
                        in_gc = True
                    continue
                if re.search(r'(SILVER|COPPER|PLATINUM|PALLADIUM|MINI|MGC|ALUMINUM|ZINC)\s+(FUTURES|OPTIONS)', upper):
                    done = True
                    break
                if re.search(r'TOTAL\s+\S+\s+FUT', upper):
                    done = True
                    break
                row = _parse_month_row(line.strip())
                if row and row not in month_rows:
                    month_rows.append(row)

    if not month_rows:
        raise ValueError("未找到 GC 各月明细行")

    # 解析不到 Trade Date 时不能用当天日期兜底：校验 a) 正是拿 Trade Date 与
    # 预期交易日比对，兜底值几乎必然等于预期值，等于用伪造数据骗过最强的校验。
    # 这里只给隔离区文件名一个占位日期，并打上标记让 validate() 直接判失败。
    date_unparsed = date_str is None
    if date_unparsed:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        print(f"  未解析到 Trade Date，占位日期：{date_str}（将判为校验失败）")

    entry = {"date": date_str, "months": month_rows}
    if date_unparsed:
        entry["date_unparsed"] = True
    return entry


def load_existing() -> list[dict]:
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    return []


# ── 采集层数据校验 ────────────────────────────────────────────────────────────
#
# 坏数据必须在写入 oi.json 之前拦下。一旦落盘，它既会显示在页面上，也会成为
# 下一交易日差分（oi[t] - oi[t-1]）的输入，把错误传播到后续所有帧。
#
# 校验按强度排序，a) 最强：Trade Date 直接来自 PDF 自身。

def expected_trade_date(now_utc: datetime | None = None) -> date_cls:
    """
    "current" 公报应当对应的交易日 —— 即「上一个交易日」，不是当天。

    CME 在 T+1 早间发布 T 日公报（详见 PUBLISH_HOUR_UTC 处的实测记录）。
    发布时刻之前运行时还要再往前退一个交易日。
    """
    now = now_utc or datetime.now(timezone.utc)
    today = now.date()
    if is_trading_day(today):
        # 交易日：当天发布的是上一个交易日的公报
        expected = prev_trading_day(today)
    else:
        # 周末/假日：最近一个交易日的公报已在当天早间发布
        expected = latest_trading_day_on_or_before(today)
    if now.hour < PUBLISH_HOUR_UTC:
        # 早于发布时刻（例如手动触发），"current" 还停在更早一个交易日
        expected = prev_trading_day(expected)
    return expected


def _months_identical(a: list[dict], b: list[dict]) -> bool:
    """逐字段比对两份交割月明细（顺序无关）。"""
    if len(a) != len(b):
        return False
    key = lambda r: r["month"]
    for x, y in zip(sorted(a, key=key), sorted(b, key=key)):
        if (x.get("month") != y.get("month")
                or x.get("settle") != y.get("settle")
                or x.get("oi") != y.get("oi")):
            return False
    return True


def validate(entry: dict, records: list[dict],
             now_utc: datetime | None = None) -> list[str]:
    """
    返回失败原因列表；空列表表示通过。
    四条校验全部执行，便于一次看清所有问题，而不是只报第一条。
    """
    failures: list[str] = []
    months = entry.get("months") or []
    parsed = date_cls.fromisoformat(entry["date"])

    # a0) Trade Date 根本没解析出来 —— 日期是占位值，不可信
    if entry.get("date_unparsed"):
        failures.append(
            f"a) PDF 内未解析到 Trade Date，{entry['date']} 为占位日期"
            f" —— PDF 版式可能已变更"
        )
    # a) Trade Date 是否等于预期交易日 —— 最强校验
    elif not is_trading_day(parsed):
        failures.append(
            f"a) PDF 内 Trade Date {entry['date']} 不是交易日"
            f"（周末或 CME 假日）"
        )
    elif not is_calendar_covered(parsed):
        failures.append(
            f"a) {entry['date']} 超出假日表覆盖范围 "
            f"—— 请在 trading_calendar.py 中补充该年份"
        )
    else:
        want = expected_trade_date(now_utc)
        if parsed > want:
            # 比预期更新：不该发生，说明发布模型或假日表有误
            failures.append(
                f"a) Trade Date {entry['date']} 晚于预期 {want.isoformat()}"
                f" —— 发布时刻模型或假日表可能需要修正"
            )
        elif parsed < want:
            # 比预期旧：滞后若干个交易日，即 CME 'current' 文件未更新
            lag = trading_days_between(parsed, want) + 1
            if lag > MAX_STALE_TRADING_DAYS:
                failures.append(
                    f"a) Trade Date 陈旧：PDF 为 {entry['date']}，预期 "
                    f"{want.isoformat()}，滞后 {lag} 个交易日"
                    f"（容许 {MAX_STALE_TRADING_DAYS}）"
                    f" —— CME 'current' 文件未更新"
                )

    # 上一交易日的记录，供 b) c) 比对
    prev_rec = None
    for r in sorted(records, key=lambda r: r["date"], reverse=True):
        if r["date"] < entry["date"] and (r.get("months") or []):
            prev_rec = r
            break

    # b) 是否与上一份完全相同（真实市场不可能全月份存量与结算价分毫不变）
    if prev_rec and _months_identical(months, prev_rec["months"]):
        failures.append(
            f"b) 全部 {len(months)} 个交割月的结算价与持仓与 {prev_rec['date']} "
            f"完全相同 —— 重复/陈旧数据"
        )

    # c) 合约数量突变（PDF 改版或解析错位到了别的品种）
    if prev_rec:
        delta = len(months) - len(prev_rec["months"])
        if abs(delta) > MAX_CONTRACT_COUNT_DELTA:
            failures.append(
                f"c) 交割月数量 {len(prev_rec['months'])} → {len(months)}"
                f"（{delta:+d}），超出阈值 ±{MAX_CONTRACT_COUNT_DELTA}"
                f" —— PDF 结构可能已改版"
            )

    # d) 主力合约 OI 缺失或为 0
    if not months:
        failures.append("d) 未解析到任何交割月")
    else:
        front = max(months, key=lambda r: r.get("oi") or 0)
        if not front.get("oi"):
            failures.append(
                f"d) 主力月 {front.get('month')} 持仓为 "
                f"{front.get('oi')!r} —— 解析失败"
            )

    return failures


def quarantine(entry: dict, pdf_bytes: bytes, failures: list[str]) -> str:
    """
    把坏数据与原始 PDF 存入隔离区。

    CME 只提供当日文件、无历史归档，坏数据错过就永久拿不回来，
    事后排查完全依赖这份快照 —— 所以隔离区要提交进仓库。
    """
    os.makedirs(QUARANTINE_DIR, exist_ok=True)
    stamp = entry.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    json_path = os.path.join(QUARANTINE_DIR, f"oi-{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
            "reason": failures,
            "entry": entry,
        }, f, ensure_ascii=False, indent=2)

    if pdf_bytes:
        pdf_path = os.path.join(QUARANTINE_DIR, f"section62-{stamp}.pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

    return json_path


def main():
    if "--test" in sys.argv:
        run_tests()
        return

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

    # ── 校验：坏数据不得写入 oi.json ──────────────────────────────────────
    print("校验中...")
    failures = validate(entry, records)
    if failures:
        # 文件名以 PDF 自身日期为键：同一份坏文件反复抓到时覆盖而非堆积
        path = quarantine(entry, content, failures)
        print("  校验失败，已隔离，data/oi.json 保持上一份可用数据：")
        for f in failures:
            print(f"    - {f}")
        print(f"  隔离区：{path}")
        # GitHub Actions 注释：红灯时无需翻日志即可看清是哪一层挂了
        print(f"::error title=OI 采集校验失败::"
              f"{entry['date']} 数据未通过校验，已隔离；oi.json 未更新。"
              f"原因：{'；'.join(failures)}")
        sys.exit(1)
    print("  4 项校验通过（Trade Date / 重复比对 / 合约数量 / 主力月持仓）")

    existing = {r["date"]: i for i, r in enumerate(records)}
    if entry["date"] in existing:
        idx = existing[entry["date"]]
        existing_months = records[idx].get("months") or []
        if existing_months:
            print(f"  {entry['date']} 已存在，跳过写入")
            return
        records[idx] = entry
        print(f"  {entry['date']} 已存在但无明细，已更新")
    else:
        records.append(entry)

    records = sorted(records, key=lambda r: r["date"])[-730:]

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"  已写入 {OUT_PATH}，共 {len(records)} 条记录")


# ── 校验逻辑单元测试 ──────────────────────────────────────────────────────────

def run_tests():
    """python fetch_oi.py --test"""
    passed = failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}  {detail}")

    # 回归基准取自 git log -- data/oi.json：流水线的真实行为，不是假设。
    # 第一版按"当天"建模，会让这 20 次里 16 次误判为陈旧，每天隔离好数据。
    print("\n[expected_trade_date] 回归：20 次真实运行")
    OBSERVED = [
        ("2026-07-28T23:09", "2026-07-27"),  # Tue
        ("2026-07-25T19:12", "2026-07-24"),  # Sat → 周五公报
        ("2026-07-24T23:09", "2026-07-23"),  # Fri
        ("2026-07-23T23:04", "2026-07-22"),  # Thu
        ("2026-07-22T23:10", "2026-07-21"),  # Wed
        ("2026-07-21T23:04", "2026-07-20"),  # Tue
        ("2026-07-18T19:10", "2026-07-17"),  # Sat
        ("2026-07-17T22:56", "2026-07-16"),  # Fri
        ("2026-07-16T23:04", "2026-07-15"),  # Thu
        ("2026-07-15T23:04", "2026-07-14"),  # Wed
        ("2026-07-14T23:03", "2026-07-13"),  # Tue
        ("2026-07-11T19:09", "2026-07-10"),  # Sat
        ("2026-07-10T23:06", "2026-07-09"),  # Fri
        ("2026-07-09T23:19", "2026-07-08"),  # Thu
        ("2026-07-08T23:17", "2026-07-07"),  # Wed
        ("2026-07-07T23:08", "2026-07-06"),  # Tue
        ("2026-07-03T23:13", "2026-07-02"),  # Fri，CME 假日 → 周四公报
        ("2026-07-02T23:16", "2026-07-01"),  # Thu
        ("2026-07-01T23:22", "2026-06-30"),  # Wed
        ("2026-06-30T23:20", "2026-06-29"),  # Tue
    ]
    mism = []
    for run_s, want_s in OBSERVED:
        run = datetime.fromisoformat(run_s).replace(tzinfo=timezone.utc)
        got = expected_trade_date(run).isoformat()
        if got != want_s:
            mism.append(f"{run_s} 应为 {want_s}，得到 {got}")
    check(f"{len(OBSERVED)} 次历史运行全部复现", not mism,
          ("\n        " + "\n        ".join(mism[:6])) if mism else "")

    # 早于发布时刻（手动触发）：在上一交易日基础上再退一个
    check("周一 12:00 UTC（未到发布时刻）→ 再退一个交易日",
          expected_trade_date(datetime(2026, 7, 27, 12, tzinfo=timezone.utc))
          == date_cls(2026, 7, 23))

    # 运行于 2026-07-27 Mon 22:00 UTC（已过发布时刻）→ 预期 Trade Date 为
    # 上一个交易日 07-24 Fri。库里已有的上一份则是 07-23 Thu。
    now = datetime(2026, 7, 27, 22, tzinfo=timezone.utc)
    GOOD_DATE = "2026-07-24"

    prev = {"date": "2026-07-23", "months": [
        {"month": "AUG26", "settle": 4100.0, "oi": 200000},
        {"month": "DEC26", "settle": 4180.0, "oi": 150000},
        {"month": "FEB27", "settle": 4200.0, "oi": 20000},
    ]}
    records = [prev]

    def entry_at(date_s, months):
        return {"date": date_s, "months": months}

    good_months = [
        {"month": "AUG26", "settle": 4090.0, "oi": 170000},
        {"month": "DEC26", "settle": 4175.0, "oi": 176000},
        {"month": "FEB27", "settle": 4195.0, "oi": 20500},
    ]

    print("\n[validate]")
    check("正常数据 → 无失败",
          validate(entry_at(GOOD_DATE, good_months), records, now) == [],
          validate(entry_at(GOOD_DATE, good_months), records, now))

    # a) Trade Date 未解析出来。占位日期恰好等于预期值时，仅靠日期比对无法
    #    发现，必须由 date_unparsed 标记拦住 —— 这是最容易漏的一条。
    unparsed = entry_at(GOOD_DATE, good_months)
    unparsed["date_unparsed"] = True
    f = validate(unparsed, records, now)
    check("a) Trade Date 未解析（占位日期=预期值）→ 命中",
          any("未解析到 Trade Date" in x for x in f), f)

    # a) 陈旧：CME current 文件仍停在更早一个交易日
    f = validate(entry_at("2026-07-23", good_months), records, now)
    check("a) Trade Date 滞后 1 个交易日 → 命中",
          any("陈旧" in x for x in f), f)

    # a) 比预期更新（不该发生，说明发布模型或假日表有误）
    f = validate(entry_at("2026-07-27", good_months), records, now)
    check("a) Trade Date 晚于预期 → 命中",
          any("晚于预期" in x for x in f), f)

    # a) 非交易日
    f = validate(entry_at("2026-07-26", good_months), records, now)  # 周日
    check("a) 非交易日 → 命中",
          any("不是交易日" in x for x in f), f)

    # b) 与上一份完全相同（日期正确但内容原封不动）
    #    注：这一条防的是「CME current 文件没换、拿到重复快照」。它并不能
    #    覆盖 06-29..07-23 那次故障 —— 那次 OI 存量本身每天都在变，坏的只有
    #    oi_chg 字段（见该窗口实测：18 天里 0 天逐字段全同）。针对那类故障的
    #    防线是 oi_chg 一律由存量差分重算，不再读 CME 的 oi_chg 字段。
    f = validate(entry_at(GOOD_DATE, prev["months"]), records, now)
    check("b) 逐字段完全相同 → 命中",
          any(x.startswith("b)") for x in f), f)

    # b) 仅一个合约的 oi 变动 1 手 → 不算重复（避免过度敏感）
    almost = [dict(m) for m in prev["months"]]
    almost[0]["oi"] += 1
    f = validate(entry_at(GOOD_DATE, almost), records, now)
    check("b) 单合约变动 1 手 → 不命中",
          not any(x.startswith("b)") for x in f), f)

    # b) 仅结算价变动、持仓全同 → 不算重复
    price_only = [dict(m, settle=m["settle"] + 0.1) for m in prev["months"]]
    f = validate(entry_at(GOOD_DATE, price_only), records, now)
    check("b) 仅结算价变动 → 不命中",
          not any(x.startswith("b)") for x in f), f)

    # c) 合约数量突变（PDF 改版或解析错位到别的品种）
    many = good_months + [
        {"month": f"X{i:02d}", "settle": 1.0, "oi": 1} for i in range(5)
    ]
    f = validate(entry_at(GOOD_DATE, many), records, now)
    check("c) 合约数 3 → 8 → 命中",
          any(x.startswith("c)") for x in f), f)

    # c) 数量变化在阈值内 → 不命中（远月自然增减）
    f = validate(entry_at(GOOD_DATE, good_months[:2]), records, now)
    check("c) 合约数 3 → 2（阈值内）→ 不命中",
          not any(x.startswith("c)") for x in f), f)

    # d) 主力月 OI 为 0
    zeroed = [dict(m, oi=0) for m in good_months]
    f = validate(entry_at(GOOD_DATE, zeroed), records, now)
    check("d) 主力月 OI = 0 → 命中",
          any(x.startswith("d)") for x in f), f)

    # d) 主力月 OI 为 None
    nulled = [dict(m, oi=None) for m in good_months]
    f = validate(entry_at(GOOD_DATE, nulled), records, now)
    check("d) 主力月 OI = None → 命中",
          any(x.startswith("d)") for x in f), f)

    # d) 空明细
    f = validate(entry_at(GOOD_DATE, []), records, now)
    check("d) 无交割月 → 命中",
          any(x.startswith("d)") for x in f), f)

    # 空库（首次运行）：b) c) 无从比对，但不应因此失败
    f = validate(entry_at(GOOD_DATE, good_months), [], now)
    check("首次运行（库为空）→ 无失败", f == [], f)

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
