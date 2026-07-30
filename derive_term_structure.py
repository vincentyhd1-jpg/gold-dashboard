#!/usr/bin/env python3
"""
Derive data/derived/term-structure-series.json from data/oi.json.

Key logic:
- oi_chg is computed from OI diffs (oi[t] - oi[t-1] per contract), NOT stored values,
  because stored values are unreliable for 06-29..07-23.
- Trading-day continuity is validated; gaps > 1 trading day set oi_chg=null for that frame.
- roll_progress = next_active_OI / (front_OI + next_active_OI), where "active" = OI > 5% of total.
- Global scale extremes span the full dataset for locked Y-axes during playback.

陈旧/重复快照的拦截点在采集层（fetch_oi.py 的 validate()），坏数据不会写进
oi.json。本脚本不再因数据可疑而中断 —— 派生数据可随时从原始数据重算，不该
让可重算的计算失败连累不可再生的采集数据。可疑之处记入 warnings。

Run:  python derive_term_structure.py
Test: python derive_term_structure.py --test
"""

import json, os, sys, re
from datetime import date

from trading_calendar import trading_days_between

IN_PATH  = os.path.join(os.path.dirname(__file__), "data", "oi.json")
OUT_DIR  = os.path.join(os.path.dirname(__file__), "data", "derived")
OUT_PATH = os.path.join(OUT_DIR, "term-structure-series.json")

ACTIVE_OI_THRESHOLD = 0.05  # contract must hold >5% of total OI to be "active"

# 主力月持仓跌破自身峰值的这个比例 → 判定为正在移仓
ROLL_WINDOW_OI_RATIO = 0.5


# 交易日历统一由 trading_calendar 提供（采集层与派生层共用同一份假日表）


# ── Month ordering ────────────────────────────────────────────────────────────

MON_ORDER = dict(JAN=1, FEB=2, MAR=3, APR=4, MAY=5, JUN=6,
                 JUL=7, AUG=8, SEP=9, OCT=10, NOV=11, DEC=12)

MONTH_RE = re.compile(r'^([A-Z]{3})(\d{2})$')


def month_key(label: str) -> int:
    m = MONTH_RE.match(label)
    if not m:
        return 0
    yr = int(m.group(2))
    return (2000 + yr) * 12 + MON_ORDER.get(m.group(1), 0)


# ── Roll-progress helpers ────────────────────────────────────────────────────

def find_front_next(months: list[dict]) -> tuple[str | None, str | None]:
    """
    Front = highest-OI contract.
    Next  = highest-OI contract *after* front with OI > ACTIVE_OI_THRESHOLD * total.
    Uses OI threshold to skip thin stub months (SEP26, NOV26, etc.).
    """
    if not months:
        return None, None
    total_oi = sum(m["oi"] for m in months)
    if total_oi == 0:
        return None, None
    threshold = total_oi * ACTIVE_OI_THRESHOLD
    sorted_m = sorted(months, key=lambda m: m["oi"], reverse=True)
    front = sorted_m[0]["month"]
    front_key = month_key(front)
    next_active = None
    for m in sorted(months, key=lambda m: month_key(m["month"])):
        if month_key(m["month"]) > front_key and m["oi"] >= threshold:
            next_active = m["month"]
            break
    return front, next_active


def roll_progress(months: list[dict], front: str, nxt: str) -> float | None:
    if not front or not nxt:
        return None
    oi_map = {m["month"]: m["oi"] for m in months}
    f_oi = oi_map.get(front, 0)
    n_oi = oi_map.get(nxt, 0)
    if f_oi + n_oi == 0:
        return None
    return round(n_oi / (f_oi + n_oi), 4)


# ── Integrity checks ─────────────────────────────────────────────────────────

def check_stored_oi_chg(record: dict) -> list[str]:
    """
    Returns list of warning strings if stored oi_chg values look suspicious.
    An all-same non-null set (e.g., all 0) is flagged as likely parser failure.

    注意：这里只记录警告，不再中断。"全部合约 oi_chg 相同" 的实际含义是
    原始快照陈旧/重复，属采集层问题 —— 拦截点在 fetch_oi.py 的 validate()，
    坏数据不会写进 oi.json。派生层不该因可重算的计算而拒绝出数据；
    stored 值本身也已不参与计算（oi_chg 一律由存量差分得出）。
    """
    months = record.get("months") or []
    chg_vals = [m.get("oi_chg") for m in months if "oi_chg" in m]
    if not chg_vals:
        return []
    non_null = [v for v in chg_vals if v is not None]
    if not non_null:
        return []
    if len(set(non_null)) == 1:
        return [
            f"{record['date']}: stored oi_chg all equal {non_null[0]} "
            f"({len(non_null)} contracts) — likely parser failure; values discarded"
        ]
    return []


# ── One-year window (same logic as frontend) ─────────────────────────────────

def window_months(months: list[dict]) -> list[dict]:
    if not months:
        return []
    near = months[0]["month"]
    m = MONTH_RE.match(near)
    if not m:
        return months
    cutoff = m.group(1) + str(int(m.group(2)) + 1).zfill(2)
    idx = next((i for i, mo in enumerate(months) if mo["month"] == cutoff), -1)
    return months[:idx + 1] if idx >= 0 else months


# ── Main derivation ──────────────────────────────────────────────────────────

def derive(records: list[dict]) -> dict:
    """
    Build term-structure-series from raw oi.json records.

    不再抛异常中断：陈旧/重复快照的拦截点已上移到 fetch_oi.py 的 validate()，
    坏数据根本不会进入 oi.json。派生数据可从原始数据随时重算，不应让可重算的
    计算失败连累不可再生的采集数据。可疑之处一律记入 warnings 供排查。
    """
    records = sorted(records, key=lambda r: r["date"])

    warnings = []
    for r in records:
        warnings.extend(check_stored_oi_chg(r))

    # Build sorted union of all contract labels across all records (one-year window)
    all_labels: set[str] = set()
    for r in records:
        for m in window_months(r.get("months") or []):
            all_labels.add(m["month"])
    contracts = sorted(all_labels, key=month_key)

    dates = [r["date"] for r in records]

    # Per-record maps for fast lookup
    oi_maps: list[dict[str, int]] = []
    for r in records:
        wm = window_months(r.get("months") or [])
        oi_maps.append({m["month"]: m["oi"] for m in wm})

    # Build frames
    frames = []
    scale = dict(oi_max=0, delta_abs_max=0, settle_min=float("inf"), settle_max=float("-inf"))

    for idx, r in enumerate(records):
        wm = window_months(r.get("months") or [])
        settle_map = {m["month"]: m["settle"] for m in wm}
        oi_map     = oi_maps[idx]

        # Update settle scale
        for v in settle_map.values():
            if v < scale["settle_min"]: scale["settle_min"] = v
            if v > scale["settle_max"]: scale["settle_max"] = v

        # Determine if this frame has a valid trading-day predecessor
        gap_too_large = False
        if idx > 0:
            prev_date = date.fromisoformat(records[idx - 1]["date"])
            curr_date = date.fromisoformat(r["date"])
            gap = trading_days_between(prev_date, curr_date)
            if gap > 0:
                gap_too_large = True
                warnings.append(
                    f"{r['date']}: gap of {gap} trading day(s) after {records[idx-1]['date']}; "
                    f"oi_chg set to null for this frame"
                )

        # Compute diff-based oi_chg (per contract)
        # Also detect CME revision: stored oi_chg exists AND differs from diff → unreliable
        stored_chg_map: dict[str, int | None] = {}
        raw_months = r.get("months") or []
        raw_chg_vals = [m.get("oi_chg") for m in raw_months if "oi_chg" in m]
        raw_non_null = [v for v in raw_chg_vals if v is not None]
        has_reliable_stored = len(set(raw_non_null)) > 1  # not all-same
        if has_reliable_stored:
            stored_chg_map = {m["month"]: m.get("oi_chg") for m in raw_months if "oi_chg" in m}

        settle_arr   = []
        oi_arr       = []
        oi_chg_arr   = []
        unreliable   = []

        for label in contracts:
            s = settle_map.get(label)
            oi = oi_map.get(label)
            settle_arr.append(s)
            oi_arr.append(oi)

            if idx == 0 or gap_too_large or oi is None:
                oi_chg_arr.append(None)
            else:
                prev_oi = oi_maps[idx - 1].get(label)
                if prev_oi is None:
                    oi_chg_arr.append(None)
                else:
                    diff = oi - prev_oi
                    oi_chg_arr.append(diff)
                    # Check for CME revision: stored ≠ diff
                    if has_reliable_stored and label in stored_chg_map:
                        stored_val = stored_chg_map[label]
                        if stored_val is not None and stored_val != diff:
                            unreliable.append(label)

            if oi is not None and oi > scale["oi_max"]:
                scale["oi_max"] = oi

        # Update delta scale (after computing chg)
        for v in oi_chg_arr:
            if v is not None and abs(v) > scale["delta_abs_max"]:
                scale["delta_abs_max"] = abs(v)

        # Roll logic
        front, nxt = find_front_next(wm)
        rp = roll_progress(wm, front, nxt) if (front and nxt) else None

        # in_roll_window: 主力月持仓已从自身峰值跌破一半 → 正在移仓
        # 用持仓相对峰值的比例判定，不用到期日 —— oi.json 里没有到期日字段，
        # 而持仓塌落本身就是移仓正在发生的直接证据。
        front_oi = oi_map.get(front, 0) if front else 0
        front_oi_max = max((oi_maps[j].get(front, 0) for j in range(idx + 1)), default=0)
        in_roll_window = bool(front_oi_max > 0
                              and front_oi / front_oi_max < ROLL_WINDOW_OI_RATIO)

        frames.append({
            "date":           r["date"],
            "settle":         settle_arr,
            "oi":             oi_arr,
            "oi_chg":         oi_chg_arr,
            "front":          front,
            "next":           nxt,
            "roll_progress":  rp,
            "in_roll_window": in_roll_window,
            "unreliable_chg": unreliable or None,
        })

    if scale["settle_min"] == float("inf"):
        scale["settle_min"] = 0
    if scale["settle_max"] == float("-inf"):
        scale["settle_max"] = 0

    return {
        "dates":     dates,
        "contracts": contracts,
        "frames":    frames,
        "scale":     scale,
        "warnings":  warnings,
    }


# ── Unit tests ───────────────────────────────────────────────────────────────

def run_tests(records: list[dict]) -> None:
    """
    两部分：

    1. 冻结 fixture —— 手写的三日数据，覆盖差分逻辑的三种情形。不依赖 oi.json，
       断言恒定有效。
    2. 真实数据抽查 —— 拿 oi.json 里的已知日期交叉验证。这些日期终会滑出
       730 条窗口，届时跳过而非报错：那是数据老化，不是代码回归，
       每天在 CI 里跑的测试不该因此变红。
    """
    print("Running unit tests...")
    errors = []

    # ── 1. 冻结 fixture ──────────────────────────────────────────────────
    # 2026-01-05/06/07 为连续交易日。三个合约，构造出：
    #   day1 首帧无前驱          → oi_chg 全 None
    #   day2 stored == diff      → 正常，unreliable_chg 为空
    #   day3 FEB26 stored ≠ diff → 该合约进 unreliable_chg
    FIXTURE = [
        {"date": "2026-01-05", "months": [
            {"month": "FEB26", "settle": 4000.0, "oi": 100000, "oi_chg": 500},
            {"month": "APR26", "settle": 4020.0, "oi": 50000,  "oi_chg": -200},
            {"month": "JUN26", "settle": 4040.0, "oi": 20000,  "oi_chg": 0},
        ]},
        {"date": "2026-01-06", "months": [
            {"month": "FEB26", "settle": 4010.0, "oi": 101000, "oi_chg": 1000},
            {"month": "APR26", "settle": 4030.0, "oi": 49000,  "oi_chg": -1000},
            {"month": "JUN26", "settle": 4050.0, "oi": 20500,  "oi_chg": 500},
        ]},
        {"date": "2026-01-07", "months": [
            # FEB26 实际 diff = +1500，但 CME 报 +1200（盘后修订前一日存量）
            {"month": "FEB26", "settle": 4015.0, "oi": 102500, "oi_chg": 1200},
            {"month": "APR26", "settle": 4035.0, "oi": 48000,  "oi_chg": -1000},
            {"month": "JUN26", "settle": 4055.0, "oi": 21000,  "oi_chg": 500},
        ]},
    ]
    fx = derive(FIXTURE)
    fx_idx = {c: i for i, c in enumerate(fx["contracts"])}
    f1, f2, f3 = fx["frames"]

    if any(v is not None for v in f1["oi_chg"]):
        errors.append(f"FIXTURE day1: 首帧应全 None，得到 {f1['oi_chg']}")
    else:
        print("  PASS  fixture day1: 首帧 oi_chg 全 None")

    want2 = {"FEB26": 1000, "APR26": -1000, "JUN26": 500}
    bad2 = {c: f2["oi_chg"][fx_idx[c]] for c, w in want2.items()
            if f2["oi_chg"][fx_idx[c]] != w}
    if bad2 or f2["unreliable_chg"]:
        errors.append(f"FIXTURE day2: 差分不符 {bad2}，unreliable={f2['unreliable_chg']}")
    else:
        print("  PASS  fixture day2: 差分与 CME 值一致，无 unreliable 标记")

    want3 = {"FEB26": 1500, "APR26": -1000, "JUN26": 500}
    bad3 = {c: f3["oi_chg"][fx_idx[c]] for c, w in want3.items()
            if f3["oi_chg"][fx_idx[c]] != w}
    if bad3:
        errors.append(f"FIXTURE day3: 差分不符 {bad3}（应以 diff 为准，非 CME stored）")
    elif set(f3["unreliable_chg"] or []) != {"FEB26"}:
        errors.append(f"FIXTURE day3: unreliable_chg 应为 ['FEB26']，"
                      f"得到 {f3['unreliable_chg']}")
    else:
        print("  PASS  fixture day3: 修订合约取 diff 并标记 unreliable_chg")

    # ── 2. 真实数据抽查 ──────────────────────────────────────────────────
    result = derive(records)
    contract_idx = {c: i for i, c in enumerate(result["contracts"])}

    def get_frame(d: str):
        return next((f for f in result["frames"] if f["date"] == d), None)

    # 抽查 1：首帧无前驱 → oi_chg 全 None
    # 注意断言的是「序列首帧」而非某个固定日期，窗口滚动后依然成立
    f0 = result["frames"][0] if result["frames"] else None
    if f0 is None:
        print("  SKIP  真实数据抽查 1：oi.json 为空")
    else:
        non_null = [v for v in f0["oi_chg"] if v is not None]
        if non_null:
            errors.append(
                f"FAIL {f0['date']}: 首帧应全 None，得到 {len(non_null)} 个非 None"
            )
        else:
            print(f"  PASS  {f0['date']}: 首帧 oi_chg 全 None")

    # 抽查 2：2026-07-24 —— CME 未修订，diff 必须与 stored 完全一致
    # 前提是它仍是中段帧：窗口滚动到它成为首帧时无前驱可差分，oi_chg 全 None
    # 是正确行为，此时比对 stored 会得到一堆假失败。
    f24 = get_frame("2026-07-24")
    r24 = next((r for r in records if r["date"] == "2026-07-24"), None)
    first_date = result["frames"][0]["date"] if result["frames"] else None
    if f24 is None or r24 is None:
        print("  SKIP  真实数据抽查 2：2026-07-24 已滑出窗口")
    elif f24["date"] == first_date:
        print("  SKIP  真实数据抽查 2：2026-07-24 已成为序列首帧（无前驱）")
    else:
        stored = {m["month"]: m.get("oi_chg") for m in r24.get("months", []) if "oi_chg" in m}
        mismatches = []
        for contract, stored_val in stored.items():
            if stored_val is None:
                continue
            ci = contract_idx.get(contract)
            if ci is None:
                continue
            computed = f24["oi_chg"][ci]
            if computed != stored_val:
                mismatches.append(f"    {contract}: stored={stored_val:+d}  computed={computed}")
        if mismatches:
            errors.append("MISMATCH 2026-07-24:\n" + "\n".join(mismatches))
        else:
            print(f"  PASS  2026-07-24: {len(stored)} oi_chg values match CME stored")

    # 抽查 3：2026-07-27 —— CME 盘后修订了前一日存量，这 4 个合约必须被标记
    # 同抽查 2：它成为首帧时无前驱可差分，unreliable_chg 为空是正确行为
    f27 = get_frame("2026-07-27")
    r27 = next((r for r in records if r["date"] == "2026-07-27"), None)
    if f27 is None or r27 is None:
        print("  SKIP  真实数据抽查 3：2026-07-27 已滑出窗口")
    elif f27["date"] == first_date:
        print("  SKIP  真实数据抽查 3：2026-07-27 已成为序列首帧（无前驱）")
    else:
        known_revised = {"AUG26", "DEC26", "OCT26", "JUL26"}
        unreliable = set(f27.get("unreliable_chg") or [])
        missing = known_revised - unreliable
        if missing:
            errors.append(
                f"FAIL 2026-07-27: 修订合约未被标记 unreliable: {missing}"
            )
        else:
            print("  PASS  2026-07-27: 修订合约已正确标记 unreliable_chg")

    if errors:
        print("\nFAILURES:")
        for e in errors:
            print(" ", e)
        sys.exit(1)
    else:
        print("All tests passed.\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    run_test_mode = "--test" in sys.argv

    with open(IN_PATH, encoding="utf-8") as f:
        records = json.load(f)
    print(f"Loaded {len(records)} records from {IN_PATH}")

    if run_test_mode:
        run_tests(records)
        return

    result = derive(records)

    if result["warnings"]:
        print("Warnings:")
        for w in result["warnings"]:
            print(" ", w)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))

    n_frames = len(result["frames"])
    n_contracts = len(result["contracts"])
    sc = result["scale"]
    print(f"Wrote {OUT_PATH}")
    print(f"  {n_frames} frames × {n_contracts} contracts")
    print(f"  settle: ${sc['settle_min']:.2f}–${sc['settle_max']:.2f}")
    print(f"  oi_max: {sc['oi_max']:,}")
    print(f"  delta_abs_max: {sc['delta_abs_max']:,}")


if __name__ == "__main__":
    main()
