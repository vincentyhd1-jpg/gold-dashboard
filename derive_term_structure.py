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
ROLL_CANDIDATES = 3          # look at top-N OI contracts to find front/next


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

        # roll_window: 5 trading days before expiry of front month
        # (simple heuristic: front month OI < 20% of its recent peak → in roll window)
        front_oi = oi_map.get(front, 0) if front else 0
        front_oi_max = max((oi_maps[j].get(front, 0) for j in range(idx + 1)), default=0)
        in_roll_window = bool(front_oi_max > 0 and front_oi / front_oi_max < 0.5)

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
    Validates diff-computed oi_chg against CME-stored oi_chg.

    Test cases:
    - 2026-06-26: first frame, no predecessor → all oi_chg must be None
    - 2026-07-24: mid-sequence frame, diff == stored (CME values confirmed good)
    - 2026-07-27: CME revised prior-day OI → diff ≠ stored; values must be tagged unreliable
    """
    print("Running unit tests...")
    errors = []
    result = derive(records)
    contract_idx = {c: i for i, c in enumerate(result["contracts"])}

    def get_frame(d: str):
        return next((f for f in result["frames"] if f["date"] == d), None)

    # Test 1: first frame → all oi_chg null (no predecessor to diff against)
    f0 = get_frame("2026-06-26")
    if f0 is None:
        errors.append("FAIL 2026-06-26: frame missing")
    else:
        non_null = [v for v in f0["oi_chg"] if v is not None]
        if non_null:
            errors.append(f"FAIL 2026-06-26: expected all None, got {len(non_null)} non-null")
        else:
            print("  PASS  2026-06-26: first frame all oi_chg=None")

    # Test 2: 2026-07-24 — no CME revision; diff must exactly match stored values
    f24 = get_frame("2026-07-24")
    r24 = next((r for r in records if r["date"] == "2026-07-24"), None)
    if f24 is None or r24 is None:
        errors.append("FAIL 2026-07-24: frame or record missing")
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

    # Test 3: 2026-07-27 — CME revised prior-day OI; known diverging contracts must be
    # tagged unreliable=True; their diff values may differ from stored
    f27 = get_frame("2026-07-27")
    r27 = next((r for r in records if r["date"] == "2026-07-27"), None)
    if f27 is None or r27 is None:
        errors.append("FAIL 2026-07-27: frame or record missing")
    else:
        known_revised = {"AUG26", "DEC26", "OCT26", "JUL26"}
        unreliable = set(f27.get("unreliable_chg") or [])
        missing = known_revised - unreliable
        if missing:
            errors.append(
                f"FAIL 2026-07-27: revised contracts not tagged unreliable: {missing}"
            )
        else:
            print(f"  PASS  2026-07-27: revised contracts correctly tagged unreliable")

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
