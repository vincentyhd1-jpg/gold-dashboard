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

# 序列末端承接月的持仓下限（相对上一个主力月的历史峰值）。
#
# 主力月判定本身用「是否曾经当过持仓最大的月」这一序数信号（见 major_months），
# 不比较跨时间的持仓量级。这个比例只用于补上序列最末端那个还在积累持仓、
# 尚未当过主力月的承接月，用来把它和噪音级的存根月区分开。
MIN_NEXT_OI_RATIO = 0.01

# 到期月持仓跌到自身峰值的这个比例以下 → 视为移仓完毕，不再作为 roll_from。
# 否则窗口滚动后，序列会永远卡在最早那个早已到期的月份上。
ROLL_DONE_OI_RATIO = 0.02

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

def find_front(months: list[dict]) -> str | None:
    """展示用主力月 = 持仓最大的合约月。前端用它高亮柱子、填 KPI。"""
    if not months:
        return None
    return max(months, key=lambda m: m["oi"])["month"]


def major_months(peak_oi: dict[str, int], ever_front: set[str]) -> list[str]:
    """
    主力月集合（移仓周期上的各站），按日历序返回。

    判定用「是否曾经是持仓最大的月」这一纯序数信号，不比较跨时间的持仓量级。

    为什么不用峰值阈值：任何形如「峰值 >= 基准 * 比例」的规则都要跨时间比较
    量级，会被长期趋势打败 —— 实测持仓在窗口内增长 5 倍时最早一个周期的帧
    全部失效，下跌到 1/5 时则是最晚一个周期失效。改用全局基准或局部基准都
    一样，只是失效的位置不同。而「曾经当过主力月」是尺度无关的：存根月无论
    在哪个价格水平上都不会成为持仓最大的月。

    唯一需要额外处理的是序列末端：最新的承接月还在积累持仓、尚未当过主力月，
    但正是下一段移仓的目标。取其后峰值最大的那个合约补上（存根月峰值只有
    噪音级，不会被选中）。
    """
    if not peak_oi:
        return []
    core = sorted((m for m in ever_front if m in peak_oi), key=month_key)
    if not core:
        return []

    last_key = month_key(core[-1])
    after = [(m, pk) for m, pk in peak_oi.items()
             if month_key(m) > last_key and pk > 0]
    if after:
        cand, cand_pk = max(after, key=lambda kv: kv[1])
        if cand_pk >= peak_oi[core[-1]] * MIN_NEXT_OI_RATIO:
            core.append(cand)
    return core


def find_roll_pair(months: list[dict], peak_oi: dict[str, int],
                   ever_front: set[str]) -> tuple[str | None, str | None]:
    """
    移仓的两端：(roll_from, roll_to)。

    roll_from = 主力月中日历序最早的、且尚未移仓完毕的那个月（正在到期、持仓流出）
    roll_to   = 其后日历序最近的主力月（正在承接流入）

    两个必须分开取，且都不能用「持仓最大的月」（即展示用的 front）：

    1. front 的定义保证 next_oi <= front_oi，于是 roll = next/(front+next) 恒 <= 0.5，
       永远爬不到 1；两者持仓一交叉还会互换标签，曲线在移仓过半时跌回低位。
       分母必须锁定在到期月上，它才会随到期单调走向 1。
    2. 主力月判定不能基于当期持仓占比 —— 到期月持仓趋近 0，会在移仓末段掉出
       集合，恰好在数值该冲向 1 时丢掉数据。也不能基于跨时间的峰值量级比较
       （见 major_months）。用「曾经当过主力月」这一序数信号。

    实测（AUG26 -> DEC26 周期，分母全程锁在 AUG26）：
      06-26  AUG26=272518  DEC26= 48082  ->  0.150
      07-23  AUG26=173991  DEC26=157027  ->  0.474
      07-28  AUG26= 71430  DEC26=241826  ->  0.772   （front 已切到 DEC26）

    roll_to 取日历序上的下一个主力月，不再比一次持仓：OCT26 从未当过持仓最大的
    月（全程横盘 21~43k，未承接移仓），自然不在主力月集合里，AUG26 的下一站
    直接就是 DEC26。
    """
    if not months:
        return None, None
    oi_map = {m["month"]: m["oi"] for m in months}
    majors = [m for m in major_months(peak_oi, ever_front) if m in oi_map]
    if len(majors) < 2:
        return (majors[0] if majors else None), None

    # 移仓完毕的月份（持仓已跌到自身峰值的极小比例）不再作为 roll_from：
    # 否则序列会永远卡在最早那个早已到期的月份上。
    for i, cand in enumerate(majors[:-1]):
        pk = peak_oi.get(cand, 0)
        if pk > 0 and oi_map[cand] / pk >= ROLL_DONE_OI_RATIO:
            return cand, majors[i + 1]

    # 全部主力月都已移仓完毕（只可能出现在序列末尾的边缘情形）
    return majors[-2], majors[-1]


def roll_progress(months: list[dict],
                  roll_from: str | None, roll_to: str | None) -> float | None:
    """
    roll_to_OI / (roll_from_OI + roll_to_OI)，从 0 爬向 1。

    分母的 roll_from 必须是到期月（见 find_roll_pair），不是展示用的 front，
    否则数值恒 <= 0.5。
    """
    if not roll_from or not roll_to:
        return None
    oi_map = {m["month"]: m["oi"] for m in months}
    f_oi = oi_map.get(roll_from, 0)
    n_oi = oi_map.get(roll_to, 0)
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

    # X 轴合约列表：只按「存续状态」过滤，不看持仓大小。
    #
    # 只剔除在最后一帧已经不再挂牌的合约（已到期）。仍在挂牌的一律保留，
    # 无论持仓多小 —— 微小持仓由前端的最小柱高渲染成细线。
    #
    # 不用持仓阈值的原因：
    # 1. 那是把渲染问题当数据问题解。被剔除的合约仍有结算价，删列会把相邻点
    #    的横向间距从 1 个月变成 2 个月，扭曲价格曲线的几何形状。期限结构图
    #    的主体是那条线，柱子是辅助，不该为柱子的整洁去改线的形状。
    # 2. 非活跃月（黄金活跃月为 2/4/6/8/10/12）的持仓随合约变远连续衰减
    #    （实测 7906 → 3475 → 280 → 272 → 42 → 14 → 8），不存在稳定的分界。
    #    看似存在的「断层」只是这条衰减曲线当前最陡的一段，会随时间平移。
    #    相对阈值随 oi_max 漂移，绝对阈值则会在衰减到阈值以下时砍掉真实合约。
    last_listed = {m["month"] for m in window_months(records[-1].get("months") or [])} \
        if records else set()
    all_labels: set[str] = set()
    for r in records:
        for m in window_months(r.get("months") or []):
            all_labels.add(m["month"])
    expired = sorted(all_labels - last_listed, key=month_key)
    contracts = sorted(all_labels & last_listed, key=month_key)
    # 到期是合约的正常生命周期，不是异常 —— 记为 info，不进 warnings。
    # 不设条数上限：窗口保留 730 条，随时间推移这个列表本就会变长，
    # 截断只会在真正需要追溯时丢掉信息。
    info = [f"已到期合约已从 X 轴剔除：{', '.join(expired)}"] if expired else []

    dates = [r["date"] for r in records]

    # Per-record maps for fast lookup
    oi_maps: list[dict[str, int]] = []
    for r in records:
        wm = window_months(r.get("months") or [])
        oi_maps.append({m["month"]: m["oi"] for m in wm})

    # 全序列一次算好，不逐帧累计：到期月的峰值/主力地位都出现在移仓之前，
    # 逐帧累计会让早期帧看不到后来的信息，导致同一段历史在不同帧里配对不一致。
    #
    # peak_oi    —— 每个合约的历史峰值持仓（判定移仓完毕、承接月下限）
    # ever_front —— 曾经当过持仓最大月的合约集合（判定主力月，见 major_months）
    peak_oi: dict[str, int] = {}
    ever_front: set[str] = set()
    for om in oi_maps:
        for label, v in om.items():
            if v is not None and v > peak_oi.get(label, 0):
                peak_oi[label] = v
        if om:
            top = max(om.items(), key=lambda kv: kv[1] or 0)
            if top[1]:
                ever_front.add(top[0])

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

        # 展示用主力月与移仓两端分开取（见 find_roll_pair 的说明）
        front = find_front(wm)
        roll_from, roll_to = find_roll_pair(wm, peak_oi, ever_front)
        rp = roll_progress(wm, roll_from, roll_to)

        # in_roll_window: 到期月持仓已从自身峰值跌破一半 → 移仓正在进行
        # 用持仓相对峰值的比例判定，不用到期日 —— oi.json 里没有到期日字段，
        # 而持仓塌落本身就是移仓正在发生的直接证据。
        # 跟踪 roll_from（到期月）而非 front：front 会在移仓过半时切换到承接月，
        # 一切换其持仓就回到峰值附近，窗口判定会被复位。
        from_oi = oi_map.get(roll_from, 0) if roll_from else 0
        from_peak = peak_oi.get(roll_from, 0) if roll_from else 0
        in_roll_window = bool(from_peak > 0
                              and from_oi / from_peak < ROLL_WINDOW_OI_RATIO)

        frames.append({
            "date":           r["date"],
            "settle":         settle_arr,
            "oi":             oi_arr,
            "oi_chg":         oi_chg_arr,
            "front":          front,
            "roll_from":      roll_from,
            "roll_to":        roll_to,
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
        "info":      info,
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

    # ── 1b. 移仓进度 fixture ─────────────────────────────────────────────
    # 构造一次完整的 FEB26 -> APR26 移仓：到期月持仓流出、承接月流入，
    # 并让两者在中途交叉（day3 起 APR26 持仓超过 FEB26）。
    #
    # 交叉点是关键：分母若用「持仓最大的月」（展示用 front），一交叉就换标签，
    # roll_progress 恒 <= 0.5 且会跌回低位；分母锁在到期月才会单调走向 1。
    # 掺入 MAR26 存根月（持仓极薄），验证活跃月判定能跳过它。
    ROLL_FIXTURE = [
        # (date, FEB26, MAR26, APR26)
        ("2026-01-05", 200000, 300, 20000),   # 移仓未启动
        ("2026-01-06", 160000, 300, 60000),
        ("2026-01-07", 100000, 300, 120000),  # 交叉：APR26 超过 FEB26
        ("2026-01-08",  40000, 300, 190000),
        ("2026-01-09",  10000, 300, 230000),  # 移仓近尾声
    ]
    rf = derive([
        {"date": d, "months": [
            {"month": "FEB26", "settle": 4000.0, "oi": feb},
            {"month": "MAR26", "settle": 4010.0, "oi": mar},
            {"month": "APR26", "settle": 4020.0, "oi": apr},
        ]}
        for d, feb, mar, apr in ROLL_FIXTURE
    ])

    rolls  = [f["roll_progress"] for f in rf["frames"]]
    froms  = [f["roll_from"] for f in rf["frames"]]
    tos    = [f["roll_to"] for f in rf["frames"]]
    fronts = [f["front"] for f in rf["frames"]]

    if any(v is None for v in rolls):
        errors.append(f"ROLL fixture: roll_progress 不应有 None，得到 {rolls}")
    elif froms != ["FEB26"] * 5:
        # 分母必须全程锁在到期月，不随持仓交叉切换
        errors.append(f"ROLL fixture: roll_from 应恒为 FEB26，得到 {froms}")
    elif tos != ["APR26"] * 5:
        errors.append(f"ROLL fixture: roll_to 应恒为 APR26（跳过 MAR26 存根月），"
                      f"得到 {tos}")
    elif rolls != sorted(rolls):
        errors.append(f"ROLL fixture: roll_progress 应单调递增，得到 {rolls}")
    elif rolls[-1] <= 0.5:
        # 这条正是 0.5 天花板 bug 的回归断言
        errors.append(f"ROLL fixture: 末帧 roll_progress 应 > 0.5（移仓近尾声），"
                      f"得到 {rolls[-1]} —— 分母可能又用回了 front")
    else:
        print(f"  PASS  roll fixture: {rolls[0]} -> {rolls[-1]} 单调爬升且突破 0.5")

    # front 应在交叉点切换到承接月，而 roll_from 不跟着切 —— 两者确实解耦
    if fronts != ["FEB26", "FEB26", "APR26", "APR26", "APR26"]:
        errors.append(f"ROLL fixture: front 应在 day3 交叉点切到 APR26，得到 {fronts}")
    else:
        print("  PASS  roll fixture: front 随持仓交叉切换，roll_from 保持不动")

    # 无承接月时 roll_progress 应为 None，而不是 0 或异常
    solo = derive([{"date": "2026-01-05", "months": [
        {"month": "FEB26", "settle": 4000.0, "oi": 100000},
        {"month": "MAR26", "settle": 4010.0, "oi": 200},  # 存根月，低于 1% 下限
    ]}])
    s0 = solo["frames"][0]
    if s0["roll_progress"] is not None or s0["roll_to"] is not None:
        errors.append(f"ROLL fixture: 无承接月时应为 None，"
                      f"得到 roll_to={s0['roll_to']} roll={s0['roll_progress']}")
    else:
        print("  PASS  roll fixture: 无承接月 → roll_progress 为 None")

    # ── 1c. 跨周期趋势鲁棒性 ─────────────────────────────────────────────
    # 窗口保留 730 条（约两年），会跨越多个移仓周期。若主力月判定依赖跨时间的
    # 持仓量级比较，长期趋势会让一端的周期整段失效：
    #   全局峰值基准 → 持仓涨 5 倍时，最早一个周期的帧全部 roll_progress=null
    #   局部峰值基准 → 同样失效（半径按合约索引取，密度一变含义就变）
    # 现在改用「曾经当过持仓最大的月」这一序数信号，与量级无关。
    # 这批断言就是防止再退回量级比较。
    CYCLES = [("FEB26", "APR26"), ("APR26", "JUN26"),
              ("JUN26", "AUG26"), ("AUG26", "OCT26")]

    def build_trend(peaks: list[int]) -> list[dict]:
        recs, d = [], 1
        for (frm, to), pk in zip(CYCLES, peaks):
            for step in range(5):
                frac = step / 4
                recs.append({"date": f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}",
                             "months": [
                                 {"month": frm, "settle": 4000.0,
                                  "oi": int(pk * (1 - frac)) + 10},
                                 {"month": to, "settle": 4020.0,
                                  "oi": int(pk * frac) + 10},
                             ]})
                d += 1
        return recs

    TRENDS = [
        ("持仓涨 2.6 倍",  [100000, 140000, 190000, 260000]),
        ("持仓涨 5 倍",    [100000, 200000, 350000, 500000]),
        ("持仓涨 20 倍",   [100000, 400000, 900000, 2000000]),
        ("持仓跌到 1/5",   [500000, 350000, 200000, 100000]),
        ("持仓跌到 1/20",  [1000000, 500000, 200000, 50000]),
    ]
    trend_bad = []
    for label, peaks in TRENDS:
        tf = derive(build_trend(peaks))
        nulls = [f["date"] for f in tf["frames"] if f["roll_progress"] is None]
        if nulls:
            trend_bad.append(f"{label}: {len(nulls)}/{len(tf['frames'])} 帧失效")
    if trend_bad:
        errors.append("TREND fixture: 主力月判定受持仓量级影响 —— "
                      + "；".join(trend_bad))
    else:
        print(f"  PASS  trend fixture: {len(TRENDS)} 种趋势场景（涨 20x ~ 跌 1/20）"
              f"均无失效帧")

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

    for i in result.get("info") or []:
        print("INFO:", i)

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
