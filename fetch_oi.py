#!/usr/bin/env python3
"""
从 CME Section 62 PDF 提取 COMEX GC 各交割月明细：月份、结算价、持仓，
追加到 data/oi.json（保留近 730 条）。

oi.json 为信封格式，业务数据在 data 键下（每天一条）：
{"schema_version":0,"source":"cme_section62",...,
 "data":[{"date":"2026-06-26","months":[{"month":"AUG26","settle":4096.30,"oi":272518},...]},...]}

落盘走 io_utils 骨架：sweep_stale_tmp → 校验 → 幂等比对 → atomic_write_json。

退出码与 oi.json 的关系分两类，这条区分是有意的：

  a) 下载失败       → exit 2，**oi.json 完全不动**
  b) 响应非 PDF     → exit 2，**oi.json 完全不动**
  c) PDF 解析失败   → exit 1，**oi.json 完全不动**
  d) 四条校验不过   → exit 1，落 data:null + coverage:null + warnings 记原因

a/b/c 是「没拿到东西」—— 手上没有任何本次的观测，写 data:null 等于用「这次没
数据」覆盖掉上一份好数据，而事实是我们根本没看到今天的盘。保持不动才对。

d 是「拿到了但不可用」—— 确实看到了今天的 PDF，只是内容不可信。此时若沿用旧
行为（拒绝落盘、停在上一份好数据），下游无从区分「今天持仓确实没变」与「今天
采集坏了」，页面照常画出完整的期限结构，陈旧多久都看不出来。落显式 data:null
之后，读取端拿到的是「这次没有数据」这个事实本身。

两类都仍然存隔离区产物，quarantine 行为不变。

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
from data_envelope import envelope, unwrap
from io_utils import atomic_write_json, read_json_or, sweep_stale_tmp

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
#
# 发布时刻只用来放宽下界，不用来收紧上界：早于此刻运行时，"current" 可能
# 还停在更早一个交易日，也可能已经换新，两者都接受。
# （取 14:00 UTC = 10am EDT 而非 8am，是为了把夏令时与发布延迟都包进窗口。）
PUBLISH_HOUR_UTC = 14

# 允许的滞后交易日数。0 表示不接受任何超出发布窗口的陈旧数据。
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

# quarantine raw 段保留的响应体上限（与 fetch_gold.RAW_BODY_LIMIT 同值）。
# 留够看清 WAF 挑战页到底说了什么，又不至于把整个 PDF 塞进 JSON。
RAW_BODY_LIMIT = 20000

# PDF 魔数。CME 的 WAF 挑战页是 200 + text/html，靠 status 分不出来，
# 只能看内容首字节 —— 少了这道检查，HTML 会一路走到 pdfplumber.open()
# 抛未捕获异常，以 exit 1 + traceback 收场：既没有隔离产物，也把上游侧
# 故障（给错东西）记成了我方处理失败。
PDF_MAGIC = b"%PDF-"


def download() -> tuple[bytes | None, dict]:
    """
    下载 Section 62 PDF，返回 (content, meta)。

    content=None 表示没拿到东西（非 200 或连接异常），此时 meta["failure"]
    说明是哪一种 —— 原先只返回 None，main() 无从区分「403 被 WAF 拦」与
    「DNS 挂了」，日志里都只有一句「下载失败，跳过更新」。

    meta 的形状与 fetch_gold 的 raw_meta 一致（source/requested_url/
    final_url/status/body），非 PDF 时直接进 quarantine 的 raw 段。
    """
    meta = {"source": "cmegroup.com", "requested_url": PDF_URL,
            "final_url": None, "status": None, "failure": None}
    try:
        from curl_cffi import requests as cffi
        print("  curl_cffi (Chrome TLS 指纹)")
        try:
            r = cffi.Session(impersonate="chrome124").get(
                PDF_URL, headers=HEADERS, timeout=30)
        except Exception as e:
            # curl_cffi 装了但请求本身炸了（超时/TLS/DNS）—— 不能让它冒泡成
            # 未捕获异常 exit 1，那会把上游侧故障记成我方处理失败。
            print(f"  连接异常：{type(e).__name__}: {e}")
            meta["failure"] = f"连接异常（curl_cffi）：{type(e).__name__}: {e}"
            return None, meta
        meta["status"] = r.status_code
        meta["final_url"] = str(getattr(r, "url", None) or PDF_URL)
        print(f"  HTTP {r.status_code}  {len(r.content):,} bytes")
        if r.status_code != 200:
            meta["failure"] = f"HTTP {r.status_code}"
            meta["body"] = r.content[:RAW_BODY_LIMIT].decode(
                "utf-8", errors="replace")
            return None, meta
        return r.content, meta
    except ImportError:
        pass
    import urllib.request
    print("  urllib 回退")
    req = urllib.request.Request(PDF_URL, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            meta["status"] = r.status
            meta["final_url"] = r.geturl()
        print(f"  HTTP {meta['status']}  {len(data):,} bytes")
        return data, meta
    except urllib.error.HTTPError as e:
        # HTTPError 有 status 与 body，比裸异常信息多得多 —— 403 挑战页的
        # 正文往往就写着为什么被拦。
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        meta["status"] = e.code
        meta["final_url"] = getattr(e, "url", None) or PDF_URL
        meta["body"] = body[:RAW_BODY_LIMIT].decode("utf-8", errors="replace")
        meta["failure"] = f"HTTP {e.code}"
        print(f"  下载失败：HTTP {e.code}")
        return None, meta
    except Exception as e:
        meta["failure"] = f"连接异常：{type(e).__name__}: {e}"
        print(f"  下载失败：{e}")
        return None, meta


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
    """
    读上一份帧数组。裸格式与信封格式都能读，data:null 与文件不存在同归 []。

    这是**追加的基底**，不只是幂等比对的对象：读不出来就会把 730 天历史
    悄悄截成今天一条 —— 且写盘照常成功、退出码为 0，没有任何一层会红。
    """
    raw = read_json_or(OUT_PATH, None)
    if raw is None:
        return []
    return unwrap(raw) or []


# ── 采集层数据校验 ────────────────────────────────────────────────────────────
#
# 坏数据必须在写入 oi.json 之前拦下。一旦落盘，它既会显示在页面上，也会成为
# 下一交易日差分（oi[t] - oi[t-1]）的输入，把错误传播到后续所有帧。
#
# 校验按强度排序，a) 最强：Trade Date 直接来自 PDF 自身。

def expected_trade_date(now_utc: datetime | None = None) -> date_cls:
    """
    "current" 里物理上可能出现的最新 Trade Date —— 即「上一个交易日」。

    CME 在 T+1 早间发布 T 日公报（详见 PUBLISH_HOUR_UTC 处的实测记录），
    所以当天的数据当天不可能拿到。这是可接受区间的上界。
    """
    now = now_utc or datetime.now(timezone.utc)
    today = now.date()
    if is_trading_day(today):
        # 交易日：当天发布的是上一个交易日的公报
        return prev_trading_day(today)
    # 周末/假日：最近一个交易日的公报已在当天早间发布
    return latest_trading_day_on_or_before(today)


def oldest_acceptable_trade_date(now_utc: datetime | None = None) -> date_cls:
    """
    可接受区间的下界。

    早于发布时刻运行时（例如手动触发），"current" 可能还停在更早一个交易日，
    这是正常的，不该判失败。
    """
    now = now_utc or datetime.now(timezone.utc)
    oldest = expected_trade_date(now)
    if now.hour < PUBLISH_HOUR_UTC:
        oldest = prev_trading_day(oldest)
    for _ in range(MAX_STALE_TRADING_DAYS):
        oldest = prev_trading_day(oldest)
    return oldest


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
        # Trade Date 必须落在 [oldest, newest] 区间内。
        # 上界是物理可能的最新交易日（当天数据当天拿不到）；下界受发布时刻影响，
        # 早于发布时刻运行时 "current" 可能还没换新。
        # 注意：数据「比预期新」不算错误 —— 那只说明发布时刻估得保守，是模型
        # 的问题不是数据的问题。只有超出上界（未来数据）才判失败。
        newest = expected_trade_date(now_utc)
        oldest = oldest_acceptable_trade_date(now_utc)
        if parsed > newest:
            failures.append(
                f"a) Trade Date {entry['date']} 晚于物理可能的最新交易日 "
                f"{newest.isoformat()} —— 假日表可能缺项，或 PDF 日期解析有误"
            )
        elif parsed < oldest:
            lag = trading_days_between(parsed, newest) + 1
            failures.append(
                f"a) Trade Date 陈旧：PDF 为 {entry['date']}，可接受区间 "
                f"{oldest.isoformat()}～{newest.isoformat()}，"
                f"滞后最新交易日 {lag} 个交易日"
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


def quarantine_raw(*, meta: dict, content: bytes | None,
                   failures: list[str], kind: str) -> str:
    """
    下载/解析阶段的隔离：还没有 entry 可存，留的是原始响应本身。

    与 quarantine() 分开是因为两者证据不同 —— 那个存「解析出来但校验不过
    的结构化数据」，这个存「压根没解析成的原始字节」。归因看的东西也不同：
    raw 段的 status / final_url / body 前若干字节能直接看出返回的是不是
    PDF、是不是挑战页，不必靠 exit code 反推。

    kind 进文件名（not-pdf / parse-error），同一天两种故障不互相覆盖。
    """
    os.makedirs(QUARANTINE_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    head = (content or b"")[:RAW_BODY_LIMIT]
    json_path = os.path.join(QUARANTINE_DIR, f"oi-{kind}-{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
            "reason": failures,
            "raw": {
                **{k: v for k, v in meta.items() if k != "body"},
                "bytes": len(content or b""),
                # 首字节单独列出：一眼看出是 %PDF- 还是 <!DOCTYPE html>
                "head_hex": head[:16].hex(),
                "head_text": head[:400].decode("utf-8", errors="replace"),
                "is_pdf": bool(content) and content.startswith(PDF_MAGIC),
                "body": meta.get("body") or head.decode("utf-8",
                                                        errors="replace"),
            },
        }, f, ensure_ascii=False, indent=2)

    # 拿到的确是 PDF 但解析失败时，原始 PDF 也要留 —— CME 无历史归档，
    # 这份字节是事后唯一能复现 parse() 为什么挂的凭据。
    if content and content.startswith(PDF_MAGIC):
        pdf_path = os.path.join(QUARANTINE_DIR, f"section62-{kind}-{stamp}.pdf")
        with open(pdf_path, "wb") as f:
            f.write(content)

    return json_path


def build_envelope(records: list[dict] | None,
                   warnings: list[str] | None = None) -> dict:
    """
    包信封。records 为 None → data:null + coverage:null。

    derived_from=[]：oi 从 CME PDF 直抓，仓库内没有上游文件。空列表是「确实
    没有仓库内上游」，与 gold 的 [upstream_ref(cot)] 是两种事实，不是省略。

    coverage 必须与 data 同步失效：data 为 null 时留着 coverage_of([]) 给的
    {first:None,last:None,count:0} 等于宣称「空但有效」，而这里要表达的是
    「无效」。显式覆盖成 None。
    """
    payload = envelope(
        source="cme_section62",
        freq="daily",
        data=records,
        dates=[r["date"] for r in (records or [])],
        date_field="date",
        derived_from=[],
        warnings=warnings or [],
        info=[f"CME Daily Bulletin Section 62 (Metals Futures Products): {PDF_URL}"],
    )
    if records is None:
        payload["coverage"] = None
    return payload


def write_null(failures: list[str]) -> None:
    """d) 校验不过：落 data:null + coverage:null，原因进 warnings。"""
    atomic_write_json(OUT_PATH, build_envelope(None, warnings=failures),
                      compact=False)


def main():
    if "--test" in sys.argv:
        run_tests()
        return

    # 清理上次崩溃留下的临时文件（只清 oi.json 自己的，见 basename 隔离）
    swept = sweep_stale_tmp(OUT_PATH)
    if swept:
        print(f"  清理上次残留的临时文件 {len(swept)} 个")

    print("正在下载 CME Section 62 PDF...")
    content, meta = download()

    # ── a) 没拿到东西 → exit 2（上游未更新）────────────────────────────────
    # 原先 exit 0，把「被 WAF 拦」当成正常态，workflow 静默绿灯 —— 与
    # fetch_stocks 的处理对齐（那边同样的洞已在 :419-425 补过）。
    # oi.json 保持不动的现有行为不变。
    if content is None:
        reason = meta.get("failure") or "未知原因"
        print(f"  下载失败，跳过更新（oi.json 保持不变）：{reason}")
        print(f"::warning title=OI PDF 下载失败::"
              f"{reason}；oi.json 未更新", file=sys.stderr)
        print(f"下载失败：{reason}", file=sys.stderr)
        sys.exit(2)

    # ── b) 拿到了但不是 PDF → exit 2 + 隔离原始响应 ────────────────────────
    # WAF 挑战页是 200 + text/html，status 分不出来，只能看魔数。
    # 归上游侧（exit 2）：源站给错了东西，不是我方解析能力问题。
    if not content.startswith(PDF_MAGIC):
        head = content[:16].hex()
        reason = (f"响应非 PDF（首字节 {head}，"
                  f"HTTP {meta.get('status')}，{len(content):,} bytes）")
        path = quarantine_raw(meta=meta, content=content,
                              failures=[reason], kind="not-pdf")
        print(f"  {reason}")
        print(f"  隔离区：{path}")
        print(f"::warning title=OI PDF 响应非 PDF::"
              f"{reason}；已隔离，oi.json 未更新", file=sys.stderr)
        print(f"响应非 PDF：{reason}", file=sys.stderr)
        sys.exit(2)

    print("解析中...")
    # ── c) 确是 PDF 但解析失败 → exit 1 + 隔离原始 PDF ─────────────────────
    # 归我方侧（exit 1）：类型对了却处理不了 —— PDF 版式变更、pdfplumber
    # 行为变化、正则失配都落在这里，需人工介入改 parse()。
    # 原先无捕获，以 traceback + exit 1 收场，没有隔离产物可供排查。
    try:
        entry = parse(content)
    except Exception as e:
        reason = f"PDF 解析失败：{type(e).__name__}: {e}"
        path = quarantine_raw(meta=meta, content=content,
                              failures=[reason], kind="parse-error")
        print(f"  {reason}")
        print(f"  隔离区：{path}（含原始 PDF）")
        print(f"::error title=OI PDF 解析失败::"
              f"{reason}；已隔离原始 PDF，oi.json 未更新", file=sys.stderr)
        print(reason, file=sys.stderr)
        sys.exit(1)

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
        # 文件名以 PDF 自身日期为键：同一份坏文件反复抓到时覆盖而非堆积
        path = quarantine(entry, content, failures)
        # d)「拿到了但不可用」→ 落 data:null。与 a/b/c 的「没拿到东西」不同，
        # 那三条仍然完全不动 oi.json（详见模块 docstring）。
        write_null(failures)
        print("  校验失败，已隔离，data/oi.json 落 data:null + coverage:null：")
        for f in failures:
            print(f"    - {f}")
        print(f"  隔离区：{path}")
        # GitHub Actions 注释：红灯时无需翻日志即可看清是哪一层挂了
        print(f"::error title=OI 采集校验失败::"
              f"{entry['date']} 数据未通过校验，已隔离；oi.json 已落 data:null。"
              f"原因：{'；'.join(failures)}")
        sys.exit(1)
    print("  4 项校验通过（Trade Date / 重复比对 / 合约数量 / 主力月持仓）")

    existing = {r["date"]: i for i, r in enumerate(records)}
    if entry["date"] in existing:
        idx = existing[entry["date"]]
        existing_months = records[idx].get("months") or []
        if existing_months:
            # 附注（本轮不改）：这条按**日期**早退，不比内容。CME 重发同一
            # Trade Date 的修订公报时，新数字会在这里被静默丢弃 —— 实测预置
            # 最新帧 oi=7,908、抓到真实值 oi=2,908，落盘仍是 7,908，exit 0。
            # 校验 b) 的重复比对管的是「与**前一交易日**相同」，不覆盖这条。
            # 要改成按内容判断属于行为变更，需独立一轮 + 先补断言。
            print(f"  {entry['date']} 已存在，跳过写入")
            return
        records[idx] = entry
        print(f"  {entry['date']} 已存在但无明细，已更新")
    else:
        records.append(entry)

    records = sorted(records, key=lambda r: r["date"])[-730:]

    # ── 幂等：业务数据与磁盘上完全相同则不写盘 ─────────────────────────────
    #
    # 只比 data 数组，信封元数据全部排除：generated_at 自比自永远不等；
    # coverage/derived_from 由 data 派生，比它等于重复比 data；warnings/info
    # 是本次运行的旁注。
    #
    # 比**整个 records**（全量 730 条）而非最新一帧，且 dict/list 的 == 是
    # 结构化递归比较，逐字段严格相等，无容差。
    #
    # 注意实际到达这里的只有「records 相对磁盘确有增减」的情形：上面那条
    # date 早退分支（已存在且有明细 → return）先把同日重复运行拦掉了，而
    # 走到这里的两条路（append 新日期 / 补一条无明细记录）都必然改变 records。
    # 所以本判断当前是一道**冗余闸**，不是唯一的幂等来源 —— 留着是因为它
    # 才是「内容没变就不写盘」这件事的正确口径：日期相同不等于内容相同，
    # 而 date 早退分支恰恰把同日修订也一并跳过了（见下方附注）。
    if load_existing() == records:
        # generated_at 表示「数据这次真变了」，不是「脚本跑了」，所以不刷新。
        print(f"  {entry['date']} 业务数据与磁盘上逐字段相同，跳过写入"
              f"（generated_at 不刷新）")
        return

    # 原子写：崩在中途只留临时文件，oi.json 保持完整旧版
    atomic_write_json(OUT_PATH, build_envelope(records), compact=False)
    print(f"  已写入 {OUT_PATH}，共 {len(records)} 条记录（信封格式）")


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

    # 上界不随发布时刻变化：物理上当天数据当天拿不到，仅此而已
    check("上界与运行时刻无关（12:00 与 22:00 一致）",
          expected_trade_date(datetime(2026, 7, 27, 12, tzinfo=timezone.utc))
          == expected_trade_date(datetime(2026, 7, 27, 22, tzinfo=timezone.utc))
          == date_cls(2026, 7, 24))

    print("\n[oldest_acceptable_trade_date] 下界")
    # 已过发布时刻：下界 = 上界，要求就是最新那一份
    check("周一 22:00 UTC（已过发布时刻）→ 下界 = 上界",
          oldest_acceptable_trade_date(datetime(2026, 7, 27, 22, tzinfo=timezone.utc))
          == date_cls(2026, 7, 24))
    # 未到发布时刻：允许还停在更早一个交易日
    check("周一 12:00 UTC（未到发布时刻）→ 下界再退一个交易日",
          oldest_acceptable_trade_date(datetime(2026, 7, 27, 12, tzinfo=timezone.utc))
          == date_cls(2026, 7, 23))

    # 会误杀新鲜数据的那个窗口：2026-07-29 Wed 12:00~14:00 UTC。
    # CME 此时已发布 07-28，旧实现（预期=严格相等且 12:00 时预期为 07-27）
    # 会把新鲜的 07-28 判为"晚于预期"并隔离。
    print("\n[区间判定] 发布时刻前后的窗口")
    for hour in (11, 13, 15, 22):
        now_w = datetime(2026, 7, 29, hour, tzinfo=timezone.utc)
        fs = validate({"date": "2026-07-28", "months": [
            {"month": "AUG26", "settle": 4100.0, "oi": 200000}]}, [], now_w)
        check(f"2026-07-29 {hour:02d}:00 UTC 拿到 07-28（新鲜）→ 不判失败",
              not fs, fs)
    # 未来数据仍必须拦住
    fs = validate({"date": "2026-07-29", "months": [
        {"month": "AUG26", "settle": 4100.0, "oi": 200000}]}, [],
        datetime(2026, 7, 29, 22, tzinfo=timezone.utc))
    check("2026-07-29 22:00 UTC 拿到 07-29（未来）→ 命中",
          any("晚于物理可能" in x for x in fs), fs)

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

    # a) 超出上界（未来数据 —— 假日表缺项或日期解析有误）
    f = validate(entry_at("2026-07-27", good_months), records, now)
    check("a) Trade Date 晚于物理可能的最新交易日 → 命中",
          any("晚于物理可能" in x for x in f), f)

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

    # ── 下载/解析阶段的失败分支 ───────────────────────────────────────────
    #
    # 这几条守的是 exit code 归属，不是校验逻辑。区分原则：
    #   上游给的东西对不对 → exit 2；我方处理不了 → exit 1。
    # 原先 a) 是 exit 0（把被 WAF 拦当正常态），b)/c) 是未捕获异常
    # exit 1 + traceback、无隔离产物 —— 三种上游侧故障混在一个颜色里。
    print("\n[下载/解析失败分支] 魔数判别与 raw 隔离")

    # PDF 魔数：WAF 挑战页是 200 + text/html，status 分不出来，只能看首字节
    check("魔数: %PDF- 开头 → 认作 PDF",
          b"%PDF-1.4\n...".startswith(PDF_MAGIC))
    check("魔数: HTML 挑战页 → 判非 PDF",
          not b"<!DOCTYPE html><html>".startswith(PDF_MAGIC))
    check("魔数: 空响应 → 判非 PDF",
          not b"".startswith(PDF_MAGIC))
    # 前导空白不能宽容：真 PDF 必须严格以 %PDF- 起头，容了就等于放 HTML 过关
    check("魔数: 前导空格 + %PDF- → 仍判非 PDF（不宽容）",
          not b"  %PDF-1.4".startswith(PDF_MAGIC))

    # quarantine_raw 的 raw 段必须能独立看出"拿到的是什么"，
    # 不必靠 exit code 反推。这几个字段是归因的最小集。
    import shutil as _sh
    import tempfile as _tf
    _real_quar = QUARANTINE_DIR
    try:
        for _kind, _body, _want_pdf in [
            ("not-pdf", b"<!DOCTYPE html><html>Access Denied</html>", False),
            ("parse-error", b"%PDF-1.4\nbroken\x00garbage", True),
        ]:
            _d = _tf.mkdtemp(prefix="oitest_")
            globals()["QUARANTINE_DIR"] = _d
            _meta = {"source": "cmegroup.com", "requested_url": PDF_URL,
                     "final_url": PDF_URL, "status": 200, "failure": None}
            _p = quarantine_raw(meta=_meta, content=_body,
                                failures=[f"test {_kind}"], kind=_kind)
            with open(_p, encoding="utf-8") as _f:
                _q = json.load(_f)
            _raw = _q.get("raw", {})
            check(f"quarantine_raw({_kind}): is_pdf={_want_pdf}",
                  _raw.get("is_pdf") is _want_pdf, _raw.get("is_pdf"))
            check(f"quarantine_raw({_kind}): raw 段字段齐全",
                  {"status", "final_url", "bytes", "head_hex", "head_text",
                   "is_pdf", "body"} <= set(_raw), sorted(_raw))
            check(f"quarantine_raw({_kind}): head_hex 对得上首字节",
                  _raw.get("head_hex") == _body[:16].hex(),
                  _raw.get("head_hex"))
            # 原始 PDF 只在确是 PDF 时留 —— HTML 挑战页没必要存成 .pdf
            _pdfs = [x for x in os.listdir(_d) if x.endswith(".pdf")]
            check(f"quarantine_raw({_kind}): "
                  f"{'留' if _want_pdf else '不留'}原始 PDF",
                  bool(_pdfs) is _want_pdf, _pdfs)
            _sh.rmtree(_d, ignore_errors=True)
    finally:
        globals()["QUARANTINE_DIR"] = _real_quar

    # download() 的返回契约：必须是二元组，且失败时 meta 带得动归因信息。
    # 单返回 None 的旧签名下，main() 无从区分 403 与 DNS 失败。
    import inspect as _ins
    _sig = _ins.signature(download)
    check("download() 返回类型标注为 tuple",
          "tuple" in str(_sig.return_annotation), _sig.return_annotation)

    # ── 信封形状 ──────────────────────────────────────────────────────────
    print("\n[信封形状]")
    from data_envelope import assert_envelope as _assert_env

    _recs = [{"date": "2026-07-30", "months": [{"month": "DEC26",
                                                "settle": 4100.0, "oi": 250000}]},
             {"date": "2026-07-31", "months": [{"month": "DEC26",
                                                "settle": 4110.0, "oi": 251000}]}]
    _env = build_envelope(_recs)
    _assert_env(_env)
    check("source=cme_section62", _env["source"] == "cme_section62", _env["source"])
    check("freq=daily", _env["freq"] == "daily", _env["freq"])
    # derived_from=[] 是「确实没有仓库内上游」这一事实，不是忘了填
    check("derived_from 为空列表（PDF 直抓，无仓库内上游）",
          _env["derived_from"] == [], _env["derived_from"])
    check("info 记 PDF URL", any(PDF_URL in s for s in _env["info"]), _env["info"])
    check("info 记 Section 62",
          any("Section 62" in s for s in _env["info"]), _env["info"])
    check("coverage 首末为帧日期范围",
          (_env["coverage"]["first"], _env["coverage"]["last"],
           _env["coverage"]["count"]) == ("2026-07-30", "2026-07-31", 2),
          _env["coverage"])
    check("unwrap 取回原帧数组", unwrap(_env) == _recs)

    # data:null 路径：coverage 必须同步失效，否则宣称「覆盖 N 天」而 data 里
    # 一条都没有。空但有效（count:0）与无效（null）是两种语义。
    print("\n[data:null 落盘形状]")
    _null = build_envelope(None, warnings=["Trade Date 不在可接受区间"])
    _assert_env(_null)
    check("data 为 None", _null["data"] is None, _null["data"])
    check("coverage 为 None（不是 count:0）", _null["coverage"] is None,
          _null["coverage"])
    check("warnings 非空且记录原因", bool(_null["warnings"]), _null["warnings"])
    check("unwrap(data:null) 得 None（读取端可辨）", unwrap(_null) is None)

    # ── 幂等比对口径 ──────────────────────────────────────────────────────
    # 只比 data，不含信封元数据。比全量而非最新帧：CME 重发修订公报时，老帧
    # 变了而最新帧没动，只比最新帧会把真实修订静默跳过。
    print("\n[幂等比对口径]")
    check("同一份 data → 判定相同", unwrap(build_envelope(_recs)) == _recs)

    _touched = json.loads(json.dumps(_recs))
    _touched[0]["months"][0]["oi"] += 1          # 改**老帧**，最新帧不动
    check("仅老帧 oi 改 1 手 → 判定不同（修订不被跳过）",
          unwrap(build_envelope(_touched)) != _recs)

    _resettled = json.loads(json.dumps(_recs))
    _resettled[0]["months"][0]["settle"] += 0.01  # 无容差：0.01 也算不同
    check("仅老帧 settle 改 0.01 → 判定不同（无容差）",
          unwrap(build_envelope(_resettled)) != _recs)

    # load_existing 是**追加的基底**，读不出来会把 730 天历史截成今天一条，
    # 且写盘照常成功、exit 0 —— 没有任何一层会红。
    print("\n[load_existing 容双形状]")
    _real_out = OUT_PATH
    _tmpd = _tf.mkdtemp()
    try:
        globals()["OUT_PATH"] = os.path.join(_tmpd, "oi.json")
        with open(OUT_PATH, "w", encoding="utf-8") as _f:
            json.dump(_recs, _f)
        check("读裸数组", load_existing() == _recs)
        with open(OUT_PATH, "w", encoding="utf-8") as _f:
            json.dump(build_envelope(_recs), _f)
        check("读信封", load_existing() == _recs)
        with open(OUT_PATH, "w", encoding="utf-8") as _f:
            json.dump(build_envelope(None, warnings=["x"]), _f)
        check("读 data:null → []（与文件不存在同归）", load_existing() == [])
        os.remove(OUT_PATH)
        check("文件不存在 → []", load_existing() == [])
    finally:
        globals()["OUT_PATH"] = _real_out
        _sh.rmtree(_tmpd, ignore_errors=True)

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
