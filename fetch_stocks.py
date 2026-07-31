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

import io_utils
from io_utils import (
    atomic_write_json, read_json_or, sweep_stale_tmp,
    upsert_by_key, apply_retention, quarantine_write,
    KEEP_OLD, TAKE_NEW,
)
from data_envelope import envelope, unwrap

GOLD_PAGE  = "https://www.cmegroup.com/markets/metals/precious/gold.html"
REPORT_URL = "https://www.cmegroup.com/delivery_reports/Gold_Stocks.xls"
OUT_PATH   = os.path.join(os.path.dirname(__file__), "data", "stocks.json")
QUARANTINE_DIR = os.path.join(os.path.dirname(__file__), "data", "quarantine")

# 落盘保留窗口（条）。原逻辑是 sorted(...)[-90:]，语义不变。
RETENTION_ITEMS = 90

# 仓库数相对上一份的允许变动。实测恒为 10 个。
MAX_DEPOT_COUNT_DELTA = 3

# 明细求和与顶层 total 的允许差值（oz）。
#
# 两者独立解析，实测 30 期差值恒为 -8 ~ -10 oz，来源是每仓库 int() 截断的
# 累积残余（每仓库 < 1 oz，10 个仓库合成个位数），与库存量级无关 ——
# 所以用绝对阈而非相对阈。相对阈 total*0.01 = 27 万 oz，反而会放过
# 最小仓库 STONEX（17 万 oz）整仓静默归零。
#
# 2 * 仓库数 给每仓库 2 oz 余量（理论上界 1 oz），32 是仓库数偏少时的兜底。
def depot_sum_tolerance(n_depots: int) -> int:
    return max(2 * n_depots, 32)

# 总量相对上一份的允许变动比例。库存日变化实测在 1% 量级。
MAX_TOTAL_CHANGE_RATIO = 0.20

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
    """
    读已有记录。信封格式取 data，裸格式原样返回（过渡期两种都能读）。

    read_json_or 的 default 是 [] —— 文件不存在或解析失败时返回空列表，
    不抛。unwrap 会对坏信封（未知 schema_version 等）抛错，那是有意的：
    宁可拒绝也不静默按旧格式解析出错的业务数据。
    """
    raw = read_json_or(OUT_PATH, [])
    rows = unwrap(raw)
    return rows if isinstance(rows, list) else []


# ── 采集层校验 ────────────────────────────────────────────────────────────────
#
# 堵「解析失败静默归零」：_to_float() 对任何异常返回 0.0，parse() 又会跳过
# 全 0 仓库，两者叠加会让单仓库解析失败表现为「该仓库凭空消失」而非报错。
# d) 的明细与总量交叉核对是抓这类问题最强的一条 —— 两者独立解析。

def validate_stocks(entry: dict, records: list[dict]) -> list[str]:
    """返回失败原因列表；空列表表示通过。"""
    failures: list[str] = []

    reg   = entry.get("registered")
    eli   = entry.get("eligible")
    total = entry.get("total")

    # a) 总量同时为零 —— COMEX 库存不可能归零
    if (reg or 0) == 0 and (eli or 0) == 0:
        failures.append(
            f"a) registered 与 eligible 同时为 0（{reg!r} / {eli!r}）"
            f" —— 解析失败"
        )

    # b) registered 非正 —— 注册库存恒 > 0
    if reg is None or reg <= 0:
        failures.append(f"b) registered 为 {reg!r} —— 注册库存恒为正")

    # 上一份记录，供 c) e) 比对
    prev = None
    for r in sorted(records, key=lambda r: r["date"], reverse=True):
        if r["date"] < entry["date"]:
            prev = r
            break

    # c) 仓库数为零或骤变
    #    三态严格区分：字段缺失 ≠ 空列表 ≠ 正常
    has_depots = "depositories" in entry
    depots = entry.get("depositories")
    if has_depots and not isinstance(depots, list):
        failures.append(
            f"c) depositories 字段类型异常（{type(depots).__name__}）"
        )
    elif has_depots and len(depots) == 0:
        failures.append("c) depositories 为空列表 —— 所有仓库解析失败")
    elif has_depots and prev and isinstance(prev.get("depositories"), list):
        delta = len(depots) - len(prev["depositories"])
        if abs(delta) > MAX_DEPOT_COUNT_DELTA:
            failures.append(
                f"c) 仓库数 {len(prev['depositories'])} → {len(depots)}"
                f"（{delta:+d}），超过阈值 ±{MAX_DEPOT_COUNT_DELTA}"
            )

    # d) 明细与总量交叉核对。
    #    仅对「有明细」的记录做 —— 早期 8 条记录无 depositories 字段，
    #    缺字段时这条判据 SKIP：不是 pass 也不是 fail，是跳过。
    #    严禁把 sum([]) = 0 当成真实求和（会让 abs(0 - total) = total 全爆红），
    #    也严禁直接索引导致 KeyError 让闸自崩。
    if not has_depots:
        pass   # SKIP：无明细可核对
    elif isinstance(depots, list) and depots:
        detail_sum = sum(d.get("total") or 0 for d in depots)
        diff = abs(detail_sum - (total or 0))
        tol = depot_sum_tolerance(len(depots))
        if diff > tol:
            failures.append(
                f"d) 明细求和 {detail_sum:,} 与 total {(total or 0):,} 相差 "
                f"{diff:,} oz，超过容差 {tol} oz"
                f" —— 可能有仓库静默归零"
            )

    # e) 总量骤变
    if prev and prev.get("total"):
        change = abs((total or 0) - prev["total"]) / prev["total"]
        if change > MAX_TOTAL_CHANGE_RATIO:
            failures.append(
                f"e) total {prev['total']:,} → {(total or 0):,}"
                f"（{change:.1%}），超过阈值 {MAX_TOTAL_CHANGE_RATIO:.0%}"
            )

    return failures


def quarantine_stocks(entry: dict, xls_bytes: bytes,
                      failures: list[str]) -> str:
    """
    坏数据与原始 XLS 存入隔离区，data/stocks.json 保持上一份不被覆盖。

    触发条件由 validate_stocks() 判定（本源专属，5 条判据）；
    这里只调 io_utils 的机械写入，骨架不判断该不该隔离。
    """
    stamp = (entry.get("date")
             or datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    return quarantine_write(
        QUARANTINE_DIR, "stocks", stamp,
        reason=failures,
        payload={"entry": entry},
        raw=xls_bytes if xls_bytes else None,
        raw_ext="xls",
    )


def merge_stocks(old: dict, new: dict):
    """
    同一 date 已有记录时的取舍。由本脚本提供，io_utils 不内置内容比较。

    三种情形，info 如实区分 —— 补全不是修订，不能掺假：
      逐字段相同        → KEEP_OLD，幂等跳过，文件完全不变
      缺 depositories   → TAKE_NEW/backfilled，首次补全
      值变了            → TAKE_NEW/revised，CME 当日重发修订版报告

    原逻辑只在「缺 depositories」时补写，其余一律 return 跳过 ——
    那会让 CME 修订版的新值被静默丢弃。这里改为取新值并记 info。
    """
    FIELDS = ("registered", "eligible", "total")

    if old == new:
        return KEEP_OLD, None

    old_has = isinstance(old.get("depositories"), list)
    new_has = isinstance(new.get("depositories"), list)
    same_totals = all(old.get(k) == new.get(k) for k in FIELDS)

    if not old_has and new_has and same_totals:
        return TAKE_NEW, "backfilled"
    return TAKE_NEW, "revised"


def main():
    if "--test" in sys.argv:
        run_tests()
        return

    # 清理上次崩溃留下的临时文件（只清 stocks.json 自己的，见 basename 隔离）
    swept = sweep_stale_tmp(OUT_PATH)
    if swept:
        print(f"  清理上次残留的临时文件 {len(swept)} 个")

    print("正在下载 CME COMEX 黄金库存报告...")
    content = download()

    if content is None:
        # exit 2 = 上游未更新／抓不到，与「正常无新数据」区分。
        # 原先 exit 0 把被 WAF 封锁当成正常态，workflow 静默绿灯。
        print("  CME WAF 封锁，本次无法获取（stocks.json 保持不变）")
        print("::warning title=CME 库存抓取失败::"
              "WAF 封锁，stocks.json 未更新")
        sys.exit(2)

    print(f"  已下载 {len(content):,} 字节")

    entry = parse(content)

    print(f"  日期：{entry['date']}")
    print(f"  注册库存（Registered）：{entry['registered']:>12,} oz")
    print(f"  合格库存（Eligible）  ：{entry['eligible']:>12,} oz")
    print(f"  合计（Total）         ：{entry['total']:>12,} oz")
    print("  各仓库明细（depositories）：")
    print(json.dumps(entry.get("depositories", []), ensure_ascii=False, indent=4))

    records = load_existing()

    # ── 校验：坏数据不得写入 stocks.json ───────────────────────────────
    # 必须在幂等判断之前 —— 坏数据即使日期重复也该被隔离，
    # 不能让幂等 return 抢先吞掉
    print("校验中...")
    failures = validate_stocks(entry, records)
    if failures:
        path = quarantine_stocks(entry, content, failures)
        print("  校验失败，已隔离，data/stocks.json 保持上一份可用数据：")
        for f in failures:
            print(f"    - {f}")
        print(f"  隔离区：{path}")
        print(f"::error title=CME 库存采集校验失败::"
              f"{entry.get('date')} 未通过校验，已隔离；stocks.json 未更新。"
              f"原因：{'；'.join(failures)}")
        sys.exit(1)
    print("  5 项校验通过（总量归零 / registered 非正 / 仓库数 / "
          "明细核对 / 总量骤变）")

    # ── 去重追加：同 date 的取舍由 merge_stocks 决定 ───────────────────
    info: list[str] = []
    updated, action, reason = upsert_by_key(
        records, entry, key="date", merge=merge_stocks)

    if action == KEEP_OLD:
        # 数据逐字段相同 → 文件完全不变、git 无 diff。
        # generated_at 表示「数据这次真变了」，不是「脚本跑了」，所以不刷新。
        print(f"  {entry['date']} 与已有记录逐字段相同，跳过写入"
              f"（generated_at 不刷新）")
        return

    if action == TAKE_NEW:
        prev = next((r for r in records if r.get("date") == entry["date"]), None)
        if reason == "backfilled":
            msg = f"{entry['date']} 记录补全 depositories（原记录缺该字段）"
        elif reason == "revised":
            old_total = prev.get("total") if prev else None
            msg = (f"{entry['date']} 数据被源站修订：total "
                   f"{'?' if old_total is None else format(old_total, ',')}"
                   f" → {entry['total']:,}")
        else:
            msg = f"{entry['date']} 取新值（{reason}）"
        info.append(msg)
        print(f"  {msg}")

    # 保留窗口：先按 date 排序再截断（按数组位置截断会在乱序输入时删错记录）
    kept = apply_retention(updated, key="date", max_items=RETENTION_ITEMS)

    payload = envelope(
        source="cme_gold_stocks",
        freq="daily",
        data=kept,
        dates=[r["date"] for r in kept],
        info=info,
    )
    # 原子写：崩在中途只留临时文件，stocks.json 保持完整旧版
    atomic_write_json(OUT_PATH, payload, compact=False)

    print(f"  已写入 {OUT_PATH}，共 {len(kept)} 条记录（信封格式）")


# ── 校验逻辑单元测试 ──────────────────────────────────────────────────────────

def run_tests():
    """python fetch_stocks.py --test（不联网）"""
    passed = failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}  {detail}")

    def depot(name, reg, eli):
        return {"name": name, "registered": reg, "eligible": eli,
                "total": reg + eli, "reg_adj": 0}

    # 10 个仓库，仿真实分布（含 STONEX 这类小仓库）
    DEPOTS = [
        depot("ASAHI", 1725473, 113286),
        depot("BRINKS", 5621485, 1219574),
        depot("DELAWARE", 63924, 118089),
        depot("HSBC", 1534977, 3967924),
        depot("IDS", 65748, 1850),
        depot("JPM", 2551327, 5929640),
        depot("LOOMIS", 532058, 132656),
        depot("MALCA", 1223887, 747),
        depot("MTB", 1310041, 754838),
        depot("STONEX", 118757, 51312),
    ]
    SUM = sum(d["total"] for d in DEPOTS)

    def entry_at(date_s, **kw):
        e = {"date": date_s, "registered": 14747681, "eligible": 12289920,
             "total": SUM + 9,   # 实测残余 +9 oz
             "depositories": [dict(d) for d in DEPOTS]}
        e.update(kw)
        return e

    prev = entry_at("2026-07-28", total=SUM + 9)
    records = [prev]

    print("\n[validate_stocks]")
    check("正常数据（残余 9 oz）→ 无失败",
          validate_stocks(entry_at("2026-07-29"), records) == [],
          validate_stocks(entry_at("2026-07-29"), records))

    # a) 总量同时为零
    f = validate_stocks(entry_at("2026-07-29", registered=0, eligible=0), records)
    check("a) registered 与 eligible 同时为 0 → 命中",
          any(x.startswith("a)") for x in f), f)

    # b) registered 非正
    for bad in (0, -100):
        f = validate_stocks(entry_at("2026-07-29", registered=bad), records)
        check(f"b) registered = {bad} → 命中",
              any(x.startswith("b)") for x in f), f)

    # c) 仓库数为空列表
    f = validate_stocks(entry_at("2026-07-29", depositories=[]), records)
    check("c) depositories 为空列表 → 命中",
          any(x.startswith("c)") for x in f), f)

    # c) 仓库数骤变 10 → 3
    few = [dict(d) for d in DEPOTS[:3]]
    f = validate_stocks(entry_at("2026-07-29", depositories=few,
                                 total=sum(d["total"] for d in few)), records)
    check("c) 仓库数 10 → 3 → 命中",
          any(x.startswith("c)") for x in f), f)

    # d) 最小仓库 STONEX（170,069 oz）静默归零
    #    这是原计划 total*0.01 阈值会放过的情形
    zeroed = [dict(d) for d in DEPOTS]
    zeroed[-1] = depot("STONEX", 0, 0)
    f = validate_stocks(entry_at("2026-07-29", depositories=zeroed), records)
    check("d) 最小仓库 STONEX 归零（17 万 oz 差）→ 命中",
          any(x.startswith("d)") for x in f), f)

    # d) 容差边界：残余 9 / 20 oz 不该命中，33 oz 该命中
    for resid, want_hit in ((9, False), (20, False), (33, True)):
        f = validate_stocks(entry_at("2026-07-29", total=SUM + resid), records)
        hit = any(x.startswith("d)") for x in f)
        check(f"d) 残余 {resid} oz → {'命中' if want_hit else '不命中'}"
              f"（容差 {depot_sum_tolerance(10)}）", hit == want_hit, f)

    # d) 三态：字段缺失 → SKIP，不是 fail
    no_depots = entry_at("2026-07-29")
    del no_depots["depositories"]
    f = validate_stocks(no_depots, records)
    check("d) 无 depositories 字段 → 跳过，不误判损坏",
          f == [], f)
    check("d) 无 depositories 字段时不抛 KeyError",
          isinstance(f, list))

    # d) 三态对照：空列表命中 c) 而非 d)（值为 0 ≠ 字段缺失）
    f = validate_stocks(entry_at("2026-07-29", depositories=[]), records)
    check("d) 空列表 ≠ 字段缺失：命中 c) 但不命中 d)",
          any(x.startswith("c)") for x in f)
          and not any(x.startswith("d)") for x in f), f)

    # e) 总量骤变
    f = validate_stocks(entry_at("2026-07-29", total=int(prev["total"] * 2),
                                 depositories=[]), records)
    check("e) total 翻倍 → 命中",
          any(x.startswith("e)") for x in f), f)

    # 首次运行（库为空）→ c) e) 无从比对，但不应失败
    f = validate_stocks(entry_at("2026-07-29"), [])
    check("首次运行（库为空）→ 无失败", f == [], f)

    print("\n[depot_sum_tolerance]")
    check("10 个仓库 → 32（max(20, 32)）", depot_sum_tolerance(10) == 32)
    check("20 个仓库 → 40（max(40, 32)）", depot_sum_tolerance(20) == 40)
    check("0 个仓库 → 32（兜底）", depot_sum_tolerance(0) == 32)

    # ── merge_stocks：同 date 的取舍（B 表语义，io_utils 不内置）─────────
    print("\n[merge_stocks]")
    base = entry_at("2026-07-29")

    act, why = merge_stocks(base, dict(base))
    check("逐字段相同 → KEEP_OLD（幂等，文件不变）",
          (act, why) == (KEEP_OLD, None), (act, why))

    revised = dict(base, total=base["total"] + 1000)
    act, why = merge_stocks(base, revised)
    check("值变了 → TAKE_NEW/revised（源站修订，取新值不丢弃）",
          (act, why) == (TAKE_NEW, "revised"), (act, why))

    old_no_dep = {k: v for k, v in base.items() if k != "depositories"}
    act, why = merge_stocks(old_no_dep, base)
    check("缺 depositories 变有 → TAKE_NEW/backfilled（补全，不是修订）",
          (act, why) == (TAKE_NEW, "backfilled"), (act, why))

    # 补全与修订必须分得开：既补了明细又改了总量 → 算修订，不算补全
    both = dict(base, total=base["total"] + 500)
    act, why = merge_stocks(old_no_dep, both)
    check("既补明细又改总量 → revised（不掺假成 backfilled）",
          (act, why) == (TAKE_NEW, "revised"), (act, why))

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
