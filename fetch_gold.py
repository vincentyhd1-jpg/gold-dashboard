#!/usr/bin/env python3
"""
获取黄金期货（GC=F）周线收盘价，与 COT 报告日期（每周二）对齐，
保存到 data/gold_price.json（信封格式，业务数据在 data 里）：

    {"schema_version":0, "source":"gold_price", "freq":"weekly",
     "date_field":"date", "coverage":{...}, "derived_from":[cot.json],
     "info":["price_source=..."], "data":[{"date":..., "price":...}, ...]}

数据源优先级：
1. Stooq https://stooq.com/q/d/l/?s=xauusd&i=w （CSV，无需 API key）
2. Yahoo Finance GC=F 周线 JSON（无需 API key）

exit code 两态：

    0  正常落盘，含幂等跳过（data 与上一份逐条相同 → 不写盘、不刷 generated_at）
    1  需人工介入 —— 五项校验命中（已落 data:null）或两源皆不可达或上游无日期

**gold 不产生 exit 2，不留该分支。** 理由：exit 2 的语义是「上游未更新，
gold_price.json 保持上一份，重跑可能自愈」。这个语义依赖「保持上一份」——
而本脚本改为校验失败时落 data:null 后，「保持上一份」这条路已经不存在了：
文件一定会被写成某种确定状态，要么是好数据、要么是显式的 data:null。
既然不再有「什么都不做地放过」这一档，2 就没有可对应的行为。

两条原 exit 2 路径的归属：
  a) 两源皆不可达      → 1。无数据可对齐，落 data:null 让下游看见「这次没有」，
                          而不是继续显示上一份陈旧价格却无从察觉。
  b) 上游 cot.json 无日期 → 1。同上；且这是真需要人看的状态（cot 采集也坏了）。

把它们压到 1 不会丢失可诊断性：warnings 里记的是具体原因，
三段 quarantine 留有 raw 响应体与实际请求 URL，归因不靠 exit code 区分。

**exit code 改动必须在 workflow 侧同步**：yml 里若还留着 gold 的 exit 2
分支，那个分支从此永不命中，读 yml 的人会误以为该状态仍可能出现。
"""

import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from data_envelope import envelope, unwrap, upstream_ref, coverage_of, is_envelope
from io_utils import atomic_write_json, read_json_or, quarantine_write, sweep_stale_tmp

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

# quarantine raw 段保留的响应体上限。留够看清源站到底返回了什么
# （HTML 错误页 / 限流提示 / 半截 CSV），又不至于把隔离区撑爆。
RAW_BODY_LIMIT = 20000


class GoldSourceError(Exception):
    """
    取数失败，且带回已拿到的响应证据。

    与裸 Exception 的区别：两源皆失败要落 data:null + 三段 quarantine，
    raw 段必须能看出**实际请求的是哪个源、返回了什么**。
    只抛字符串的话这部分证据在栈里就丢了。
    """
    def __init__(self, message: str, raw_meta: dict | None = None):
        super().__init__(message)
        self.raw_meta = raw_meta


def fetch_stooq() -> tuple[dict[str, float], dict]:
    """
    Return ({date_str: close_price}, raw_meta) from Stooq CSV (xauusd, weekly).

    raw_meta 带回响应体 / status / **实际请求到的 URL**（resp.geturl()，
    经重定向后可能与 STOOQ_URL 不同）供 quarantine raw 段留证 ——
    「解析失败」与「源站返回的就是这个」必须能事后区分开。
    """
    req = urllib.request.Request(STOOQ_URL, headers={
        "User-Agent": "gold-dashboard/1.0",
        "Accept":     "text/csv,text/plain,*/*",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        body   = resp.read()
        status = resp.status
        final_url = resp.geturl()
    text = body.decode("utf-8").strip()
    raw_meta = {"source": "stooq.com", "requested_url": STOOQ_URL,
                "final_url": final_url, "status": status,
                "body": text, "bytes": len(body)}

    if text.startswith("Get your apikey") or "<html" in text.lower():
        raise GoldSourceError(
            f"Stooq returned non-CSV response: {text[:120]}", raw_meta)

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
        raise GoldSourceError("Stooq returned empty price data", raw_meta)
    return prices, raw_meta


def fetch_yahoo() -> tuple[dict[str, float], dict]:
    """Return ({date_str: close_price}, raw_meta) from Yahoo chart API (GC=F, weekly).

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
        body   = resp.read()
        status = resp.status
        final_url = resp.geturl()
    text = body.decode("utf-8", errors="replace")
    raw_meta = {"source": "Yahoo Finance (GC=F)", "requested_url": YAHOO_URL,
                "final_url": final_url, "status": status,
                "body": text[:RAW_BODY_LIMIT], "bytes": len(body)}

    try:
        data = json.loads(body)
        result_obj = data["chart"]["result"][0]
        timestamps  = result_obj["timestamp"]
        closes      = result_obj["indicators"]["quote"][0]["close"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        raise GoldSourceError(f"Yahoo response shape unexpected: {e}", raw_meta)

    prices: dict[str, float] = {}
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        prices[date_str] = round(float(close), 2)
    if not prices:
        raise GoldSourceError("Yahoo returned empty price data", raw_meta)
    return prices, raw_meta


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


def _raw_segment(attempts: list[dict], effective: str) -> dict:
    """
    raw 段：逐次尝试的响应证据 + 哪个源实际生效。

    effective 为空串时写 None —— 「全都失败了」和「生效源恰好叫空串」
    必须能区分开。
    """
    return {
        "effective": effective or None,
        "attempts":  attempts,
    }


def quarantine_gold(*, raw_meta: dict | None,
                    parsed: dict[str, float] | None,
                    aligned: list[dict] | None,
                    failures: list[str],
                    stamp: str | None = None) -> str:
    """
    三段留证。data/gold_price.json 会被写成 data:null，隔离区留还原现场的材料。

    三段各回答一个不同的问题，缺一段就有一类归因做不出来：

      raw     源站到底返回了什么      —— 含 body / status / **实际请求 URL** /
                                        生效源。区分「源站返回就是坏的」与
                                        「我们解析错了」
      parse   解析出了什么但还没对齐  —— price_map（{日期: 价格}）。区分
                                        「CSV 解析错列」与「对齐窗口没命中」
      align   对齐后的最终序列        —— [{date, price}]，price 可能为 None。
                                        这才是被 validate_gold 判死的那份

    触发条件由 validate_gold() 或取数失败判定；这里只调 io_utils 的机械写入，
    骨架不判断该不该隔离。
    """
    if stamp is None:
        last_dated = [r["date"] for r in (aligned or []) if r.get("date")]
        stamp = (last_dated[-1] if last_dated
                 else datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    parse_seg = None
    if parsed is not None:
        keys = sorted(parsed)
        parse_seg = {
            "count": len(parsed),
            "first": keys[0] if keys else None,
            "last":  keys[-1] if keys else None,
            "price_map": parsed,
        }

    align_seg = None
    if aligned is not None:
        prices = [r.get("price") for r in aligned]
        align_seg = {
            "count":    len(aligned),
            "missing":  sum(1 for p in prices if p is None),
            "coverage": coverage_of([r["date"] for r in aligned if r.get("date")]),
            "series":   aligned,
        }

    return quarantine_write(
        QUARANTINE_DIR, "gold", stamp,
        reason=failures,
        payload={
            "raw":   raw_meta,
            "parse": parse_seg,
            "align": align_seg,
        },
    )


def write_null(failures: list[str], info: list[str]) -> None:
    """
    落 data:null + coverage:null，并把原因记进 warnings。

    为什么不再「保持上一份」：旧行为是校验失败就拒绝落盘，文件停在上一次的
    好数据上。那让下游无从区分「这周价格确实没变」与「这周采集坏了」——
    页面照常画出一条完整金价线，陈旧多久都看不出来。落显式的 null 之后，
    读取端拿到的是「这次没有数据」这个事实本身。

    coverage 必须同时为 null：data 为 null 时留着上一轮的 coverage
    等于宣称「覆盖 N 周」而 data 里一条都没有，两者自相矛盾。
    envelope() 里 coverage_of([]) 会给 {first:None,last:None,count:0}，
    那是「空但有效」的语义；这里要的是「无效」，所以显式覆盖成 None。
    """
    payload = envelope(
        source="gold_price",
        freq="weekly",
        data=None,
        dates=[],
        derived_from=[upstream_ref(COT_PATH, "cftc_cot")],
        warnings=failures,
        info=info,
    )
    payload["coverage"] = None
    atomic_write_json(OUT_PATH, payload, compact=False)


def load_existing_data() -> tuple[list | None, bool]:
    """
    读上一份的业务数据供幂等比对。裸格式与信封格式都能读。

    返回 (业务数据, 是否信封格式)。

    default=None：文件不存在与「文件存在但 data:null」在幂等语义上同类 ——
    都表示「上一份没有可比的数据」，一律视为不同，照常写盘。
    """
    raw = read_json_or(OUT_PATH, None)
    if raw is None:
        return None, False
    return unwrap(raw), is_envelope(raw)


def main():
    if "--test" in sys.argv:
        run_tests()
        return

    # 清理上次崩溃留下的临时文件（只清本文件自己的，见 basename 隔离）
    swept = sweep_stale_tmp(OUT_PATH)
    if swept:
        print(f"清理上次残留的临时文件 {len(swept)} 个")

    print("读取 COT 日期列表...")
    # unwrap 容双形状：cot.json 信封化前后都能读。strict=False 是过渡期默认，
    # 四源全迁完后统一收紧（见 CLAUDE.md TODO）。
    with open(COT_PATH, encoding="utf-8") as f:
        cot = unwrap(json.load(f))

    # 用 .get 而非 ["weekly"]：缺键与空列表是同一类上游状态，都不该崩。
    cot_dates = [r["date"] for r in ((cot or {}).get("weekly") or [])]
    if not cot_dates:
        failures = ["f) 上游 cot.json 无可用日期（weekly 缺失或为空）"
                    " —— 无 COT 日期可对齐"]
        path = quarantine_gold(raw_meta=None, parsed=None, aligned=None,
                               failures=failures)
        write_null(failures, info=[])
        print("上游 cot.json 没有可用日期（weekly 缺失或为空）。")
        print(f"  已落 data:null，隔离区：{path}")
        print("::error title=金价上游无日期::"
              "cot.json 无 weekly 数据，已落 data:null")
        sys.exit(1)

    print(f"  COT 包含 {len(cot_dates)} 周，范围 {cot_dates[0]} ~ {cot_dates[-1]}")

    # ── 取数：Stooq → Yahoo 级联。source 记**实际生效**的那个源，不硬编码 ──
    #
    # raw_attempts 逐次累积，不是只留最后一次：两源都失败时只留一份 raw
    # 会让隔离区显示「请求的是 Stooq」而实际上 Yahoo 也试过且是它最后失败的
    # —— 归因会指错源。每次尝试都留一条，raw 段自带 effective 字段标明
    # 哪个真正生效（全失败时为 null）。
    price_map: dict[str, float] = {}
    source = ""
    raw_attempts: list[dict] = []
    attempts: list[str] = []

    for label, fn in (("Stooq (xauusd)", fetch_stooq),
                      ("Yahoo Finance (GC=F)", fetch_yahoo)):
        try:
            print(f"尝试 {label} ...")
            price_map, meta = fn()
            source = meta["source"]
            raw_attempts.append({**meta, "outcome": "ok"})
            print(f"  成功，获取 {len(price_map)} 条记录")
            break
        except (GoldSourceError, urllib.error.URLError, OSError,
                ValueError) as e:
            attempts.append(f"{label}: {e}")
            print(f"  {label} 失败: {e}")
            meta = getattr(e, "raw_meta", None)
            if meta is None:
                # HTTPError 之类在读到 body 之前就抛了 —— 没有响应体可留，
                # 但「试过这个源、这样失败的」本身就是证据，不能整条丢掉。
                meta = {"source": label,
                        "requested_url": STOOQ_URL if fn is fetch_stooq else YAHOO_URL,
                        "final_url": None,
                        "status": getattr(e, "code", None),
                        "body": None, "bytes": None}
            raw_attempts.append({**meta, "outcome": "failed", "error": str(e)})
    else:
        # 两源皆不可达 → exit 1 并落 data:null。不再是 exit 2「保持上一份」：
        # 保持上一份会让页面继续显示陈旧价格且无从察觉（见 write_null 说明）。
        failures = ["g) Stooq 与 Yahoo 两源皆不可达"] + [f"   {a}" for a in attempts]
        path = quarantine_gold(raw_meta=_raw_segment(raw_attempts, source),
                               parsed=None, aligned=None, failures=failures)
        write_null(failures, info=[])
        print("所有数据源均不可达，已落 data:null。")
        print(f"  隔离区：{path}")
        print("::error title=金价源不可达::"
              "Stooq 与 Yahoo 均失败，已落 data:null")
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

    # info 记**实际生效**的源。级联到 Yahoo 就记 Yahoo —— 硬编码 stooq
    # 会让「哪个源出的数」这个问题在事后查不出来，而两源的 bar 日期约定不同
    # （Stooq 收在周五、Yahoo 开在周一），对齐结果的解释依赖它。
    #
    # 价格源不进 derived_from：那里记的是**本仓库自己产出**的上游文件
    # （此处只有 cot.json），外部 HTTP 源不是。
    info = [f"price_source={source}"]
    if attempts:
        info.append(f"fallback_after={len(attempts)} 次失败：{'; '.join(attempts)}")

    # ── 校验：不过则落 data:null（不再是拒绝落盘）───────────────────────
    # 必须在幂等判断之前：坏数据即使与上一份相同也该被隔离，
    # 不能让幂等 return 抢先吞掉。
    print("校验中...")
    failures = validate_gold(result)
    if failures:
        path = quarantine_gold(raw_meta=_raw_segment(raw_attempts, source),
                               parsed=price_map,
                               aligned=result, failures=failures)
        write_null(failures, info)
        print("  校验失败，已落 data:null + coverage:null：")
        for f in failures:
            print(f"    - {f}")
        print(f"  隔离区（三段：raw / parse / align）：{path}")
        print(f"::error title=金价采集校验失败::"
              f"未通过校验，已落 data:null。"
              f"原因：{'；'.join(failures)}")
        sys.exit(1)
    print("  5 项校验通过（全 null / 缺失比例 / 最新一期 / 价格区间 / 序列退化）")

    # ── 幂等：只比 data 数组（含历史值修正）────────────────────────────
    # 逐条比对而非只看最新一期：源站会修订历史周的收盘价，只比末条会漏掉
    # 中间某周被改的情形。相同则不写盘、不刷 generated_at ——
    # generated_at 表示「数据这次真变了」，不是「脚本跑了」。
    old_data, old_is_envelope = load_existing_data()

    if old_data == result:
        if old_is_envelope:
            print(f"  {len(result)} 条数据与已有记录逐条相同，跳过写入"
                  f"（generated_at 不刷新，git 无 diff）")
            return
        else:
            # 格式迁移：旧文件是裸格式，业务数据相同但仍需写一次以升级到信封
            migration_info = "Format migration: upgraded from bare list to envelope (business data unchanged)"
            info.append(migration_info)
            print(f"  {len(result)} 条数据格式迁移：bare list → envelope（业务数据未变）")

    payload = envelope(
        source="gold_price",
        freq="weekly",
        data=result,
        dates=[r["date"] for r in result],
        derived_from=[upstream_ref(COT_PATH, "cftc_cot")],
        info=info,
    )
    # 原子写：崩在中途只留临时文件，gold_price.json 保持完整旧版
    atomic_write_json(OUT_PATH, payload, compact=False)
    print(f"已保存至 {OUT_PATH}，共 {len(result)} 条（信封格式，{info[0]}）")


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

    # ── 信封 / data:null / 幂等 / 三段 quarantine ──────────────────────
    # 这些是 commit3 的新代码路径，若无断言则「12 条全绿」不覆盖它们。
    import tempfile

    print("\n[信封形状]")
    env = envelope(
        source="gold_price", freq="weekly", data=good,
        dates=[r["date"] for r in good],
        derived_from=[], info=["price_source=stooq.com"],
    )
    check("envelope 十字段齐全",
          set(env) == {"schema_version", "source", "freq", "generated_at",
                       "date_field", "coverage", "derived_from",
                       "warnings", "info", "data"}, sorted(env))
    check("source=gold_price / freq=weekly / date_field=date",
          env["source"] == "gold_price" and env["freq"] == "weekly"
          and env["date_field"] == "date",
          (env["source"], env["freq"], env["date_field"]))
    check("data 原样放入（未被改写）", env["data"] == good)
    check("coverage 为对齐后日期范围",
          env["coverage"] == {"first": good[0]["date"],
                              "last": good[-1]["date"], "count": 52},
          env["coverage"])

    print("\n[data:null 落盘形状]")
    # 真落盘再读回：只查内存对象会漏掉序列化环节（coverage 覆盖是在
    # envelope() 返回之后做的，若顺序写错则内存对的、落盘的错）
    global OUT_PATH, QUARANTINE_DIR
    _saved_out, _saved_quar = OUT_PATH, QUARANTINE_DIR
    with tempfile.TemporaryDirectory() as td:
        OUT_PATH = os.path.join(td, "gold_price.json")
        QUARANTINE_DIR = os.path.join(td, "quarantine")
        reasons = ["a) 全部 52 周对齐失败（price 全为 null）"]
        write_null(reasons, info=["price_source=Yahoo Finance (GC=F)"])
        with open(OUT_PATH, encoding="utf-8") as f:
            got = json.load(f)
        check("data 为 null", got["data"] is None, got["data"])
        check("coverage 为 null（不是 count:0 的空壳）",
              got["coverage"] is None, got["coverage"])
        check("warnings 记录失败原因（非空）",
              got["warnings"] == reasons, got["warnings"])
        check("info 保留实际生效源",
              got["info"] == ["price_source=Yahoo Finance (GC=F)"], got["info"])
        check("data:null 仍是合法信封（assert_envelope 不抛）",
              _assert_ok(got))
        check("unwrap(data:null) 得 None（读取端可辨）",
              unwrap(got) is None)

        # 幂等比对：上一份是 data:null 时视为「无可比数据」，不该判相同
        check("load_existing_data() 读 data:null 得 None",
              load_existing_data()[0] is None)
        check("data:null 与真实序列不相等（不会误判幂等跳过）",
              load_existing_data()[0] != good)

        print("\n[幂等比对]")
        atomic_write_json(OUT_PATH, envelope(
            source="gold_price", freq="weekly", data=good,
            dates=[r["date"] for r in good]), compact=False)
        check("逐条相同 → 判定相同（跳过写入）",
              load_existing_data()[0] == good)
        revised = [dict(r) for r in good]
        revised[10]["price"] = revised[10]["price"] + 0.01
        check("历史第 11 周被修订 → 判定不同（只比末条会漏）",
              load_existing_data()[0] != revised)
        check("末条被修订 → 判定不同",
              load_existing_data()[0] != good[:-1] + [
                  {**good[-1], "price": good[-1]["price"] + 1}])
        check("裸数组格式的旧文件也能读回比对",
              _bare_roundtrip(OUT_PATH, good))

        print("\n[三段 quarantine]")
        # raw 段用 _raw_segment 构造：两次尝试（Stooq 失败 → Yahoo 成功），
        # 断言它能同时留下「试过谁」与「谁生效」
        raw_seg = _raw_segment([
            {"source": "stooq.com", "requested_url": STOOQ_URL,
             "final_url": STOOQ_URL, "status": 200, "body": "<!DOCTYPE html>",
             "bytes": 796, "outcome": "failed", "error": "non-CSV response"},
            {"source": "Yahoo Finance (GC=F)", "requested_url": YAHOO_URL,
             "final_url": YAHOO_URL + "&crumb=x", "status": 200,
             "body": '{"chart":...}', "bytes": 13, "outcome": "ok"},
        ], "Yahoo Finance (GC=F)")
        pmap = {"2026-07-27": 5230.5, "2026-07-20": 5100.0}
        qpath = quarantine_gold(raw_meta=raw_seg, parsed=pmap,
                                aligned=good, failures=reasons)
        with open(qpath, encoding="utf-8") as f:
            q = json.load(f)
        check("三段齐全（raw / parse / align）",
              all(k in q for k in ("raw", "parse", "align")), sorted(q))
        check("raw 段能看出实际生效源（级联到 Yahoo 就记 Yahoo）",
              q["raw"]["effective"] == "Yahoo Finance (GC=F)",
              q["raw"].get("effective"))
        check("raw 段保留全部尝试（失败的那次不被最后一次覆盖）",
              [a["source"] for a in q["raw"]["attempts"]]
              == ["stooq.com", "Yahoo Finance (GC=F)"]
              and [a["outcome"] for a in q["raw"]["attempts"]]
              == ["failed", "ok"],
              q["raw"]["attempts"])
        check("raw 段含实际请求 URL 与 status",
              q["raw"]["attempts"][1]["final_url"].startswith(
                  "https://query1.finance.yahoo.com")
              and q["raw"]["attempts"][0]["status"] == 200,
              [(a.get("final_url"), a.get("status")) for a in q["raw"]["attempts"]])
        check("parse 段是未对齐的 price_map",
              q["parse"]["price_map"] == pmap and q["parse"]["count"] == 2,
              q["parse"])
        check("align 段是对齐后序列且含 missing 计数",
              q["align"]["series"] == good and q["align"]["missing"] == 0,
              {k: q["align"][k] for k in ("count", "missing")})
        check("reason 记录判据", q["reason"] == reasons, q.get("reason"))

        # 全失败：effective 为 None，但 attempts 仍留两条
        allfail = _raw_segment([
            {"source": "stooq.com", "outcome": "failed", "error": "x"},
            {"source": "Yahoo Finance (GC=F)", "outcome": "failed",
             "status": 403, "error": "HTTP Error 403: Forbidden"},
        ], "")
        check("两源皆失败 → effective 为 None 且 attempts 留两条",
              allfail["effective"] is None and len(allfail["attempts"]) == 2,
              allfail)

        # 取数失败时 parsed/aligned 为 None —— 三段仍在，缺的段显式 null，
        # 不能整段消失（消失会让人误以为忘了留证）
        qpath2 = quarantine_gold(raw_meta=allfail, parsed=None, aligned=None,
                                 failures=["g) 两源皆不可达"], stamp="2026-01-01")
        with open(qpath2, encoding="utf-8") as f:
            q2 = json.load(f)
        check("取数失败时三段键仍在，parse/align 显式 null",
              q2["raw"] is not None and q2["parse"] is None
              and q2["align"] is None,
              {k: q2[k] for k in ("parse", "align")})
    OUT_PATH, QUARANTINE_DIR = _saved_out, _saved_quar

    print("\n[exit code 约定]")
    # 只扫**生产代码**（run_tests 之前的部分）。不能全文件扫：
    # `sys.exit(2)` 这个字面量出现在本断言自己的消息串里，会自我匹配恒红。
    # 上一版按引号排除自身，仍被 check() 那行的中文消息串命中 ——
    # 按区段切比按模式排除可靠：生产路径才是被测对象，测试代码不是。
    import re
    src_lines = open(__file__, encoding="utf-8").read().splitlines()
    prod_end = next((i for i, l in enumerate(src_lines)
                     if l.startswith("def run_tests(")), len(src_lines))
    exit2 = [(i + 1, l.strip()) for i, l in enumerate(src_lines[:prod_end])
             if re.search(r"sys\.exit\(\s*2\s*\)", l)
             and not l.strip().startswith("#")]
    check(f"生产代码（前 {prod_end} 行）无真实 sys.exit(2) —— gold 不产生 exit 2",
          not exit2, exit2)
    check("文档串写明不留 exit 2 分支的理由",
          "gold 不产生 exit 2" in "\n".join(src_lines))

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


def _assert_ok(payload) -> bool:
    from data_envelope import assert_envelope
    try:
        assert_envelope(payload)
        return True
    except ValueError:
        return False


def _bare_roundtrip(path: str, rows: list[dict]) -> bool:
    """裸数组格式（信封化之前的形状）写进去，load_existing_data 仍能读回。"""
    atomic_write_json(path, rows, compact=False)
    return load_existing_data()[0] == rows


if __name__ == "__main__":
    main()
