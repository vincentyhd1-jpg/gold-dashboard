#!/usr/bin/env python3
"""
从 CFTC Socrata API 获取黄金期货 COT 数据（合约代码 088691），
计算管理基金净多持仓、商业套保净持仓、COT Index，
以信封格式保存到 data/cot.json（业务数据在 data 键里）。

落盘走 io_utils 骨架：sweep_stale_tmp → 校验 → 幂等比对 → atomic_write_json。
七个骨架函数用五个 —— upsert_by_key / apply_retention 不用，因为 cot 是周频
全量重写：API 每次返回完整 52 周，整体替换，没有「单条追加」语义；窗口在
请求端由 $limit=52 决定，落盘端再截一次就是两处窗口定义，早晚打架。

时间戳从业务数据里删掉了（原 updated_at）—— 它属信封元数据 generated_at，
且只在业务数据真变时刷新，见 main() 的幂等比对。
"""

import json
import os
import sys
from datetime import datetime, timezone, date as date_cls
from urllib.request import urlopen, Request
from urllib.parse import urlencode

from io_utils import (
    atomic_write_json, read_json_or, sweep_stale_tmp, quarantine_write,
)
from data_envelope import envelope, unwrap

API_URL   = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
GOLD_CODE = "088691"
OUT_PATH  = os.path.join(os.path.dirname(__file__), "data", "cot.json")
QUARANTINE_DIR = os.path.join(os.path.dirname(__file__), "data", "quarantine")

# 有效周数下限。API 请求 52 条，允许少数期缺失。
# TODO(待历史数据回看)：45 是拍的，等积累足够历史后按实际缺失率校准。
MIN_WEEKS = 45

# 最新一期距今的天数上限。CFTC 每周五发布上周二数据，正常滞后 3~10 天。
MAX_LATEST_AGE_DAYS = 21

# 相邻期间隔上限（天）。实测正常为 6~8 天，节假日会顺延。
MAX_WEEK_GAP_DAYS = 14


def fetch_api(limit: int = 52) -> list[dict]:
    params = urlencode({
        "cftc_contract_market_code": GOLD_CODE,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": limit,
    })
    req = Request(f"{API_URL}?{params}", headers={
        "User-Agent": "gold-dashboard/1.0",
        "Accept":     "application/json",
    })
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_row(row: dict) -> dict | None:
    date = row.get("report_date_as_yyyy_mm_dd", "")
    if not date:
        return None
    date = date[:10]   # strip optional time component

    def i(key: str) -> int:
        return int(float(row.get(key) or 0))

    mf_long  = i("m_money_positions_long_all")
    mf_short = i("m_money_positions_short_all")

    # prod_merc fields have no _all suffix in this dataset
    prod_long  = i("prod_merc_positions_long")
    prod_short = i("prod_merc_positions_short")

    swap_long  = i("swap_positions_long_all")
    swap_short = i("swap__positions_short_all")   # two underscores after swap

    return {
        "date":          date,
        "mf_net":        mf_long - mf_short,
        "comm_net":      (prod_long - prod_short) + (swap_long - swap_short),
        "open_interest": i("open_interest_all"),
    }


def cot_index(values: list[int], current: int) -> int | None:
    """
    净持仓在 52 周区间内的百分位。

    退化输入（mx == mn）返回 None，不返回 50 —— 原先返 50 会把「52 周净持仓
    分毫不变」这种解析失败归一化成看似正常的中性值，形成第二层掩盖：
    上游全 0 被 i() 静默吞掉，再被这里粉饰成 50%，页面上完全看不出异常。

    真实市场 52 周区间退化为一点不可能，所以 None 恒等于「数据有问题」。
    落盘 null 语义正确（不可知），与 0（确实为零）严格区分。
    """
    if not values:
        return None
    mn, mx = min(values), max(values)
    if mx == mn:
        return None
    return round((current - mn) / (mx - mn) * 100)


# ── 采集层校验 ────────────────────────────────────────────────────────────────
#
# 堵「解析失败静默归零」：i() 里 float(row.get(key) or 0) 会把字段改名/缺失
# 变成 0，一路落盘、经 cot_index 粉饰成 50%，页面完全看不出异常。
# 闸必须在 cot_index() 之前 —— 放后面看到的是被归一化过的值。

def validate_cot(weekly: list[dict],
                 today: date_cls | None = None) -> list[str]:
    """返回失败原因列表；空列表表示通过。五条判据全跑，一次看清所有问题。"""
    failures: list[str] = []
    if not weekly:
        return ["a) weekly 为空 —— 未解析到任何有效数据"]

    KEYS = ("mf_net", "comm_net", "open_interest")

    # a) 关键字段在所有期都为 0 —— 真实市场不可能
    if all(all((r.get(k) or 0) == 0 for k in KEYS) for r in weekly):
        failures.append(
            f"a) 全部 {len(weekly)} 期的 mf_net/comm_net/open_interest 均为 0"
            f" —— 字段改名或 API 结构变更导致静默归零"
        )

    latest = weekly[-1]

    # b) 最新一期三项同时为 0 —— 该行解析失败，不是市场状态
    if all((latest.get(k) or 0) == 0 for k in KEYS):
        failures.append(
            f"b) 最新一期 {latest['date']} 的 mf_net/comm_net/open_interest "
            f"同时为 0 —— 该行解析失败"
        )

    # c) open_interest 非正 —— 总持仓恒 > 0
    oi = latest.get("open_interest")
    if oi is None or oi <= 0:
        failures.append(
            f"c) 最新一期 {latest['date']} 的 open_interest 为 {oi!r}"
            f" —— 总持仓恒为正，非正即解析失败"
        )

    # d) 有效期数骤降 —— 字段改名会让 parse_row 大批返回 None
    if len(weekly) < MIN_WEEKS:
        failures.append(
            f"d) 有效期数仅 {len(weekly)}，低于下限 {MIN_WEEKS}"
            f" —— 大量行解析失败"
        )

    # e) 日期分布异常：中间缺期，或整体停更
    dates = [date_cls.fromisoformat(r["date"]) for r in weekly]
    for prev_d, cur_d in zip(dates, dates[1:]):
        gap = (cur_d - prev_d).days
        if gap > MAX_WEEK_GAP_DAYS:
            failures.append(
                f"e) {prev_d} → {cur_d} 间隔 {gap} 天，超过上限 "
                f"{MAX_WEEK_GAP_DAYS} —— 中间期可能解析失败"
            )
            break
    age = ((today or datetime.now(timezone.utc).date()) - dates[-1]).days
    if age > MAX_LATEST_AGE_DAYS:
        failures.append(
            f"e) 最新一期 {dates[-1]} 距今 {age} 天，超过上限 "
            f"{MAX_LATEST_AGE_DAYS} —— 上游可能已停更"
        )

    return failures


def quarantine_cot(weekly: list[dict], raw, failures: list[str]) -> str:
    """
    坏数据与原始 API 响应存入隔离区，data/cot.json 保持上一份不被覆盖。

    这里只调 io_utils 的机械写入，骨架不判断该不该隔离（硬约束 3）——
    触发条件在 validate_cot() 的五条判据里。

    stamp 用数据自身的最新期日期而非运行时刻：同一份坏数据反复抓到时覆盖
    而非堆积。weekly 为空（连一行都没解析出来）时无日期可用，退回当日。

    raw 是 Socrata 的 JSON 响应，序列化成字节另存一份（`raw_ext="raw.json"`，
    不能用 `"json"` —— 会与 payload 撞名）—— 与 parsed_weekly 分开保存，
    便于事后区分「API 返回就是坏的」与「parse_row 解析错了」。
    """
    stamp = (weekly[-1]["date"] if weekly
             else datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    return quarantine_write(
        QUARANTINE_DIR, "cot", stamp,
        reason=failures,
        payload={"parsed_weekly": weekly},
        raw=json.dumps(raw, ensure_ascii=False, indent=2).encode("utf-8"),
        # 不能用 "json" —— 会与 payload 的 cot-<stamp>.json 撞同一路径，
        # raw 覆盖掉 parsed_weekly，隔离区只剩一半证据（已由 quarantine_write
        # 的撞名断言拦住，这里显式错开）。
        raw_ext="raw.json",
    )


def build_payload(weekly: list[dict]) -> dict:
    """
    业务数据 {latest, weekly}。不含时间戳 —— generated_at 属信封元数据。

    单独成函数是为了让幂等比对能拿到「本次算出的业务数据」这一个对象，
    与磁盘上的旧 data 直接 == 比较。
    """
    mf_vals   = [r["mf_net"]   for r in weekly]
    comm_vals = [r["comm_net"] for r in weekly]

    latest = weekly[-1]
    prev   = weekly[-2] if len(weekly) >= 2 else {}

    return {
        "latest": {
            "date":          latest["date"],
            "mf_net":        latest["mf_net"],
            "comm_net":      latest["comm_net"],
            "open_interest": latest["open_interest"],
            "mf_net_chg":    latest["mf_net"]   - prev.get("mf_net",   0),
            "comm_net_chg":  latest["comm_net"] - prev.get("comm_net", 0),
            "mf_index":      cot_index(mf_vals,   latest["mf_net"]),
            "comm_index":    cot_index(comm_vals, latest["comm_net"]),
        },
        "weekly": weekly,
    }


def main():
    if "--test" in sys.argv:
        run_tests()
        return

    # 清理上次崩溃留下的临时文件（只清 cot.json 自己的，见 basename 隔离）
    swept = sweep_stale_tmp(OUT_PATH)
    if swept:
        print(f"  清理上次残留的临时文件 {len(swept)} 个")

    print("正在请求 CFTC Socrata API...")
    raw = fetch_api(limit=52)
    print(f"  获取到 {len(raw)} 条原始记录")

    weekly = sorted(
        filter(None, (parse_row(r) for r in raw)),
        key=lambda r: r["date"],
    )

    # ── 校验：坏数据不得写入 cot.json ──────────────────────────────────
    # 必须在 cot_index() 之前 —— 它会把退化输入粉饰掉
    print("校验中...")
    failures = validate_cot(weekly)
    if failures:
        path = quarantine_cot(weekly, raw, failures)
        print("  校验失败，已隔离，data/cot.json 保持上一份可用数据：")
        for f in failures:
            print(f"    - {f}")
        print(f"  隔离区：{path}")
        print(f"::error title=COT 采集校验失败::"
              f"未通过校验，已隔离；cot.json 未更新。"
              f"原因：{'；'.join(failures)}")
        sys.exit(1)
    print(f"  5 项校验通过（全期归零 / 单期归零 / OI 非正 / 期数 / 日期分布）")

    data = build_payload(weekly)
    latest     = data["latest"]
    mf_index   = latest["mf_index"]
    comm_index = latest["comm_index"]

    # ── 幂等：业务数据与磁盘上完全相同则不写盘 ─────────────────────────
    #
    # cot 是周频全量重写、无 upsert 路径，所以判断点在这里显式比对。
    #
    # 比**整个 data**（latest + weekly 全量），不是只比 latest：CFTC 会修订
    # 历史期，老周数字变了而最新一期没动时，只比 latest 会把真实修订静默跳过。
    # dict/list 的 == 是结构化递归比较，够用。
    #
    # 信封元数据全部排除：generated_at 自比自永远不等；coverage/derived_from
    # 由 data 派生，比它等于重复比 data；warnings/info 是本次运行的旁注。
    old = unwrap(read_json_or(OUT_PATH, None)) if os.path.exists(OUT_PATH) else None
    if old == data:
        # generated_at 表示「数据这次真变了」，不是「脚本跑了」，所以不刷新。
        print(f"  {latest['date']} 业务数据与磁盘上逐字段相同，跳过写入"
              f"（generated_at 不刷新）")
        return

    payload = envelope(
        source="cftc_cot",
        freq="weekly",
        data=data,
        dates=[r["date"] for r in weekly],
    )
    # 原子写：崩在中途只留临时文件，cot.json 保持完整旧版
    atomic_write_json(OUT_PATH, payload, compact=False)

    print(f"已保存 {len(weekly)} 条数据到 {OUT_PATH}（信封格式）")
    print(
        f"最新一期：{latest['date']}"
        f"  管理基金净多={latest['mf_net']:+,}"
        f"  商业套保净={latest['comm_net']:+,}"
        f"  COT Index={'--' if mf_index is None else str(mf_index) + '%'}"
        f"  商业 Index={'--' if comm_index is None else str(comm_index) + '%'}"
    )


# ── 校验逻辑单元测试 ──────────────────────────────────────────────────────────

def run_tests():
    """python fetch_cot.py --test（不联网）"""
    passed = failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}  {detail}")

    TODAY = date_cls(2026, 7, 30)

    def week(i, mf=10000, comm=-8000, oi=500000):
        """第 i 周（0 = 最早），按 7 天递推。52 周覆盖到 2026-07-21。"""
        d = date_cls(2025, 7, 29) + __import__("datetime").timedelta(days=7 * i)
        return {"date": d.isoformat(), "mf_net": mf + i * 100,
                "comm_net": comm - i * 100, "open_interest": oi + i * 10}

    good = [week(i) for i in range(52)]

    print("\n[validate_cot]")
    check("正常 52 周 → 无失败",
          validate_cot(good, TODAY) == [], validate_cot(good, TODAY))

    # a) 全期三项归零
    f = validate_cot([dict(w, mf_net=0, comm_net=0, open_interest=0)
                      for w in good], TODAY)
    check("a) 全期 mf/comm/oi 均为 0 → 命中",
          any(x.startswith("a)") for x in f), f)

    # b) 仅最新一期三项归零
    z = good[:-1] + [dict(good[-1], mf_net=0, comm_net=0, open_interest=0)]
    f = validate_cot(z, TODAY)
    check("b) 最新一期三项同时为 0 → 命中",
          any(x.startswith("b)") for x in f), f)

    # c) open_interest 非正
    for bad_oi in (0, -5):
        f = validate_cot(good[:-1] + [dict(good[-1], open_interest=bad_oi)], TODAY)
        check(f"c) open_interest = {bad_oi} → 命中",
              any(x.startswith("c)") for x in f), f)

    # d) 期数骤降
    f = validate_cot(good[-30:], TODAY)
    check("d) 仅 30 期 → 命中", any(x.startswith("d)") for x in f), f)

    # e) 中间缺期（间隔 21 天）
    gapped = good[:20] + good[23:]
    f = validate_cot(gapped, TODAY)
    check("e) 中间间隔 21 天 → 命中", any(x.startswith("e)") for x in f), f)

    # e) 停更：最新一期距今 30 天
    f = validate_cot(good, date_cls(2026, 8, 20))
    check("e) 最新一期距今 30 天 → 命中",
          any("停更" in x for x in f), f)

    # 正常边界：间隔 8 天不该命中
    check("正常间隔 6~8 天 → 不命中 e)",
          not any(x.startswith("e)") for x in validate_cot(good, TODAY)))

    print("\n[cot_index]")
    check("退化输入（52 周全 0）→ None，不是 50",
          cot_index([0] * 52, 0) is None, cot_index([0] * 52, 0))
    check("退化输入（全为同一非零值）→ None",
          cot_index([5000] * 52, 5000) is None)
    check("空序列 → None", cot_index([], 0) is None)
    check("正常序列中位 → 50（与退化情形区分）",
          cot_index([100, 200, 150], 150) == 50, cot_index([100, 200, 150], 150))
    check("正常序列极值 → 0 / 100",
          cot_index([100, 200], 100) == 0 and cot_index([100, 200], 200) == 100)

    print("\n[parse_row]")
    check("缺 date → None", parse_row({}) is None)
    check("字段全缺 → 三项为 0（由 validate_cot 拦截，非此处职责）",
          parse_row({"report_date_as_yyyy_mm_dd": "2026-07-21"})
          == {"date": "2026-07-21", "mf_net": 0, "comm_net": 0,
              "open_interest": 0})

    print("\n[build_payload]")
    p = build_payload(good)
    check("不含时间戳字段（updated_at 已删，generated_at 归信封）",
          "updated_at" not in p and "generated_at" not in p, sorted(p.keys()))
    check("顶层只有 latest / weekly",
          sorted(p.keys()) == ["latest", "weekly"], sorted(p.keys()))
    check("latest 八个字段齐全",
          sorted(p["latest"].keys()) == sorted([
              "date", "mf_net", "comm_net", "open_interest",
              "mf_net_chg", "comm_net_chg", "mf_index", "comm_index"]),
          sorted(p["latest"].keys()))
    check("latest.date = 最后一期", p["latest"]["date"] == good[-1]["date"])
    check("mf_net_chg = 末期 − 前期",
          p["latest"]["mf_net_chg"] == good[-1]["mf_net"] - good[-2]["mf_net"],
          p["latest"]["mf_net_chg"])
    check("weekly 原样透传（不排序不改写）", p["weekly"] == good)
    check("单期输入 → chg 以 0 为基准（prev 缺失）",
          build_payload([good[0]])["latest"]["mf_net_chg"] == good[0]["mf_net"])

    # 幂等的相等判断：build_payload 必须是纯函数，同输入产出可 == 的对象。
    # 若将来有人往 data 里塞时间戳，这条会立刻红 —— 那正是幂等失效的形态。
    check("同输入两次调用结果相等（纯函数，幂等比对的前提）",
          build_payload(good) == build_payload(good))
    check("weekly 里历史期变了 → payload 不相等（防「只比 latest」退化）",
          build_payload(good)
          != build_payload([dict(good[0], mf_net=999999)] + good[1:]))
    check("只有 latest 变了 → payload 不相等",
          build_payload(good)
          != build_payload(good[:-1] + [dict(good[-1], mf_net=777777)]))

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
