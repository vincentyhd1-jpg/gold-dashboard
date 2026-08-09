#!/usr/bin/env python3
"""
Derive data/derived/term-structure-series.json from data/oi.json.

Key logic:
- oi_chg is computed from OI diffs (oi[t] - oi[t-1] per contract), NOT stored values,
  because stored values are unreliable for 06-29..07-23.
- Trading-day continuity is validated; gaps > 1 trading day set oi_chg=null for that frame.
- front_remaining = 到期月当前 OI / 到期月历史峰值 OI，从 1 降到 0。
- roll_noise = |到期月 OI 变化| / Σ|全部合约 OI 变化|，另存 3 日均值 roll_noise_ma。
  暂无阈值：当前数据全在移仓加剧的上升段，无平静期可供定位拐点。
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
from data_envelope import envelope, upstream_ref, unwrap
from io_utils import atomic_write_json, read_json_or, sweep_stale_tmp

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

# roll_noise 的移动平均窗口（交易日）。
# 取 3 而非 5：22 帧的序列上 5 日均值平滑过度，把 06-30 的真实低谷
# （raw 0.0182）抹成 0.1430 且滞后一帧。等数据攒够再调回。
ROLL_NOISE_MA_DAYS = 3


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


def month_gap_days(a: str | None, b: str | None) -> int:
    """
    两个交割月之间的日历天数差。

    年化价差需要真实天数，不能按「月数 × 30」估算 —— AUG26→DEC26 是 122 天，
    按 4×30=120 算会让年化率偏高 1.7%。

    以每月 1 日为基准：合约实际到期日在月中，但两端同样偏移，差值不受影响。
    """
    ma, mb = MONTH_RE.match(a or ""), MONTH_RE.match(b or "")
    if not ma or not mb:
        return 0
    da = date(2000 + int(ma.group(2)), MON_ORDER.get(ma.group(1), 1), 1)
    db = date(2000 + int(mb.group(2)), MON_ORDER.get(mb.group(1), 1), 1)
    return (db - da).days


# ── KPI 指标 ─────────────────────────────────────────────────────────────────
#
# 这些算术原先散在前端 _renderFrame 里，下沉到派生层：前端只读字段不算数，
# 计算逻辑进 --test 有回归保护，将来别的页面（term-3d 等）要用也不必重写。
# 角色锚定不在这里 —— roll_from / roll_to 由 find_roll_pair() 决定。

def total_oi_of(months: list[dict],
                established: set[str] | None = None) -> int:
    """
    已确立主角总持仓 = Σ OI over month ∈ ever_front。
    即只累加「已当过持仓最大月」的月份，只计有结算价的合约。**零阈值。**

    传 ever_front 而非 major_months() 的返回值：后者会用
    MIN_NEXT_OI_RATIO（未跑分布的拍值）给序列末端那个尚未坐正的承接月补位
    （当前数据下是 FEB27），那会让 total_oi 依赖一个没锚定的阈值。
    ever_front 由逐帧取持仓最大月累积得出，纯序数、尺度无关、无阈值。

    不含三类：
      到期清算残余         首帧 JUN26 的 7 手
      从未当过主角的名义月  OCT26 全程横盘 21~43k，从未成为持仓最大月
      未坐正的末端承接月    当前 FEB27（还在积累，尚未当过最大月）

    与价差/主力月共享 major_months 家族但**口径有意略窄**：
      价差含承接月     问「往哪移」→ 用 major_months（含 FEB27）
      total_oi 只含主角 问「盘子多大」→ 用 ever_front
    两者不同口径是有意的，不是不一致。

    旧口径「全部挂牌月求和」已作废：它把到期残余与名义月一并计入。

    established 为 None 时退化为「全部传入月份求和」，仅供无 ever_front
    上下文的调用方（单帧场景）使用。
    """
    rows = [m for m in months if m.get("settle") is not None]
    if established is not None:
        rows = [m for m in rows if m["month"] in established]
    return sum(m["oi"] or 0 for m in rows)


def spread_metrics(months: list[dict],
                   roll_from: str | None,
                   roll_to: str | None) -> dict:
    """
    到期月 → 承接月的价差与年化率。

    annualized_pct = spread / near_settle × (365 / 日历天数差) × 100

    锚点用合约角色而非首末列：首列正在到期、末列只有几手，两端都是交易所
    推定价，算出来的是结算程序而不是市场。
    """
    empty = {"spread": None, "spread_gap_days": None, "spread_annualized_pct": None}
    if not roll_from or not roll_to:
        return empty

    smap = {m["month"]: m.get("settle") for m in months}
    near, far = smap.get(roll_from), smap.get(roll_to)
    if near is None or far is None or near <= 0:
        return empty

    # 全精度落盘，不做任何舍入 —— 计算层保精度，展示层才格式化。
    # 舍入到显示精度会让断言比的是截断后的值（绿得没意义），且精度一旦丢就
    # 拿不回来。前端 _renderFrame 的 toFixed(2) 负责显示。
    spread = far - near
    days = month_gap_days(roll_from, roll_to)
    if days <= 0:
        return {"spread": spread, "spread_gap_days": days,
                "spread_annualized_pct": None}

    ann = spread / near * (365 / days) * 100
    return {"spread": spread, "spread_gap_days": days,
            "spread_annualized_pct": ann}


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


def front_remaining(months: list[dict], roll_from: str | None,
                    peak_oi: dict[str, int]) -> float | None:
    """
    近月剩余 = 到期月当前 OI / 到期月历史峰值 OI，从 1 降到 0。

    直接回答「这个合约还剩多少仓位没走」。分子分母是同一个合约，不受承接月
    的选取影响 —— 旧的 roll_to/(roll_from+roll_to) 是迁移占比，会随承接月
    换月而跳变，且方向是 0→1，读起来像进度条但分母含义会变。

    分母用历史峰值而非窗口内首帧：窗口滚动后首帧可能已在移仓中途，
    用它做分母会让曲线起点低于 1。
    """
    if not roll_from:
        return None
    oi_map = {m["month"]: m["oi"] for m in months}
    cur = oi_map.get(roll_from)
    pk = peak_oi.get(roll_from, 0)
    if cur is None or pk <= 0:
        return None
    return round(min(cur / pk, 1.0), 4)


def roll_noise(oi_chg: list[int | None], front_idx: int | None) -> float | None:
    """
    |到期月 OI 变化| / Σ|全部合约 OI 变化|，单帧原始值。

    衡量当日的持仓变动有多少来自移仓而非新增/离场。接近 1 说明当天的变化
    几乎全是到期月在流出（纯移仓噪音），接近 0 说明变化来自别处。

    暂不设阈值：当前数据全在移仓加剧的上升段，没有平静期可供定位拐点。
    详见 CLAUDE.md。

    全精度落盘，不做任何舍入 —— 阈值待定，将来要在这一列上跑分布，
    round(4) 会给分布垫一层量化地板（相邻值被吸附到同一格），
    影响拐点定位。展示层若需要再格式化。
    """
    if front_idx is None or front_idx < 0 or front_idx >= len(oi_chg):
        return None
    vals = [v for v in oi_chg if v is not None]
    if not vals:
        return None
    total = sum(abs(v) for v in vals)
    fv = oi_chg[front_idx]
    if total == 0 or fv is None:
        return None
    return abs(fv) / total


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
    # 并集口径：contracts = 全序列各帧 window_months 的并集，按月份顺序排序。
    #
    # 不再与「最后一帧是否仍挂牌」求交 —— 那个交集会让合约一到期就把自己的整列
    # 从全序列历史里抹掉。实测 06-29 掉 JUN26、07-30 掉 JUL26，两列在它们仍
    # 存续的那些帧上有真实 settle/oi 却无列可放（23 帧 24 格，量级最高达该帧
    # 主力月 oi 的 2.87%，比反向的「有列无值」上界 0.32% 高一个数量级）。
    #
    # 更要紧的是移仓起点：AUG26 一到期，它作为 front 的 19 帧、作为 roll_from
    # 的 24 帧会同时失去列位，AUG→DEC 的 272518→2908 流出在回放中无处呈现，
    # 只剩承接端 DEC26 的上升。轴是历史的坐标系，不该由最后一帧的存续状态决定。
    #
    # 轴仍不逐帧变（并集全序列算一次，长度恒定），前端 _initCharts 建图时
    # 一次性写 labels 的模式零改动可用。
    last_listed = {m["month"] for m in window_months(records[-1].get("months") or [])} \
        if records else set()
    all_labels: set[str] = set()
    for r in records:
        for m in window_months(r.get("months") or []):
            all_labels.add(m["month"])
    expired = sorted(all_labels - last_listed, key=month_key)
    contracts = sorted(all_labels, key=month_key)
    # 到期是合约的正常生命周期，不是异常 —— 记为 info，不进 warnings。
    # 不设条数上限：窗口保留 730 条，随时间推移这个列表本就会变长，
    # 截断只会在真正需要追溯时丢掉信息。
    info = [f"已到期合约仍保留 X 轴列位（末帧已不挂牌）：{', '.join(expired)}"] \
        if expired else []

    dates = [r["date"] for r in records]

    # Per-record maps for fast lookup
    #
    # oi_maps      窗口筛后（window_months）—— 展示视图字段用，as-of-now 语义
    # oi_maps_raw  该帧原始 months，未经任何过滤 —— 帧级取证字段用，
    #              as-of-that-frame 语义
    #
    # 两份分开建而非共用一份：窗口末端随 near 月移动（实测 06-29、07-30 各移动
    # 一次），拿会移动的边界去筛历史取证记录，等于让今天的窗口位置回头篡改
    # 昨天的取证结论。展示字段该跟着窗口走（图上只画窗口内那 13 列），
    # 取证字段不该。
    oi_maps: list[dict[str, int]] = []
    oi_maps_raw: list[dict[str, int | None]] = []
    for r in records:
        wm = window_months(r.get("months") or [])
        oi_maps.append({m["month"]: m["oi"] for m in wm})
        oi_maps_raw.append({m["month"]: m.get("oi")
                            for m in (r.get("months") or [])})

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

        # unreliable_chg 走该帧原始 months —— 既不经 contracts 存续筛，
        # 也不经 window_months 窗口筛。
        #
        # 这是帧级取证字段（as-of-that-frame）与展示视图字段（as-of-now）的
        # 二分。取证记的是「该帧当时 stored 与 diff 不符」，是历史事实；
        # 展示字段答的是「今天该画哪些列」。两道过滤的边界都随时间移动：
        #   contracts       按「最后一帧是否仍挂牌」算，合约到期即移出
        #   window_months   按 near + 1 年截，窗口末端随 near 月前移
        #
        # 先前两次都栽在同一处：
        #   一、曾在 `for label in contracts` 循环里顺带算，于是合约一到期就把
        #       自己在所有历史帧里的修订记录一起带走。实测 JUL26 在 07-27 确有
        #       修订（stored -5 / diff +3），到期后该帧标记就没了。
        #   二、改用 oi_map 后仍是 window_months 的产物，窗口外的修订合约照样
        #       漏标。对当前 24 帧零影响（新增 0 条），但窗口末端实测已移动两次
        #       （06-29 JUN27→JUL27、07-30 JUL27→AUG27），下一次移动就会漏。
        #
        # 失真单向（只漏报、不误报）且无视觉异常 —— 该字段全仓无前端消费端，
        # 漏标不会有任何页面症状，只能靠 Python 侧断言守。
        unreliable = []
        if idx > 0 and not gap_too_large and has_reliable_stored:
            prev_oi_map = oi_maps_raw[idx - 1]
            for label, oi_v in oi_maps_raw[idx].items():
                if oi_v is None or label not in stored_chg_map:
                    continue
                prev_oi = prev_oi_map.get(label)
                if prev_oi is None:
                    continue
                stored_val = stored_chg_map[label]
                if stored_val is not None and stored_val != oi_v - prev_oi:
                    unreliable.append(label)
        unreliable.sort(key=month_key)

        settle_arr   = []
        oi_arr       = []
        oi_chg_arr   = []

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
                    oi_chg_arr.append(oi - prev_oi)

            if oi is not None and oi > scale["oi_max"]:
                scale["oi_max"] = oi

        # Update delta scale (after computing chg)
        for v in oi_chg_arr:
            if v is not None and abs(v) > scale["delta_abs_max"]:
                scale["delta_abs_max"] = abs(v)

        # 展示用主力月与移仓两端分开取（见 find_roll_pair 的说明）
        front = find_front(wm)
        roll_from, roll_to = find_roll_pair(wm, peak_oi, ever_front)
        fr_remaining = front_remaining(wm, roll_from, peak_oi)
        rn_raw = roll_noise(oi_chg_arr,
                            contracts.index(roll_from) if roll_from in contracts else None)

        # KPI 算术：原先在前端 _renderFrame 里，下沉到这里。
        # 入参按 X 轴合约（contracts）而非原始窗口月份（wm），两者差在已到期月。
        axis_months = [{"month": lbl,
                        "settle": settle_arr[i],
                        "oi": oi_arr[i]}
                       for i, lbl in enumerate(contracts)]
        # total_oi 口径 B2：只累加 ever_front（已当过持仓最大月的已确立主角），
        # 零阈值。不传 major_months() 的返回值 —— 它含 MIN_NEXT_OI_RATIO 补位的
        # 末端承接月。价差用 major_months（问往哪移）、total_oi 用 ever_front
        # （问盘子多大），口径有意略窄。见 total_oi_of()。
        kpi_total_oi = total_oi_of(axis_months, ever_front)
        kpi_spread   = spread_metrics(axis_months, roll_from, roll_to)

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
            "date":            r["date"],
            "settle":          settle_arr,
            "oi":              oi_arr,
            "oi_chg":          oi_chg_arr,
            "front":           front,
            "roll_from":       roll_from,
            "roll_to":         roll_to,
            "front_remaining": fr_remaining,
            "roll_noise":      rn_raw,
            "in_roll_window":  in_roll_window,
            "unreliable_chg":  unreliable or None,
            "total_oi":        kpi_total_oi,
            **kpi_spread,
        })

    # roll_noise 的移动平均。逐帧算完原始值后统一做，窗口不足时用已有样本。
    # 只存字段，前端暂不使用 —— 阈值待数据覆盖一个完整移仓周期后再定。
    # 同 roll_noise：全精度落盘，不舍入，避免给待跑的分布垫量化地板。
    for i, f in enumerate(frames):
        lo = max(0, i - (ROLL_NOISE_MA_DAYS - 1))
        w = [frames[j]["roll_noise"] for j in range(lo, i + 1)
             if frames[j]["roll_noise"] is not None]
        f["roll_noise_ma"] = sum(w) / len(w) if w else None

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

_PASS_COUNT = 0


def _ok(msg: str) -> None:
    """
    打印一条 PASS 并计数。

    通过数必须和失败数一起报：真实数据抽查会在日期滑出窗口时静默跳过
    （见 run_tests 文档），只报 "0 failed" 的话，用例从 17 条掉到 3 条
    也照样全绿 —— 看不出来。
    """
    global _PASS_COUNT
    _PASS_COUNT += 1
    print(msg)


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
        _ok("  PASS  fixture day1: 首帧 oi_chg 全 None")

    want2 = {"FEB26": 1000, "APR26": -1000, "JUN26": 500}
    bad2 = {c: f2["oi_chg"][fx_idx[c]] for c, w in want2.items()
            if f2["oi_chg"][fx_idx[c]] != w}
    if bad2 or f2["unreliable_chg"]:
        errors.append(f"FIXTURE day2: 差分不符 {bad2}，unreliable={f2['unreliable_chg']}")
    else:
        _ok("  PASS  fixture day2: 差分与 CME 值一致，无 unreliable 标记")

    want3 = {"FEB26": 1500, "APR26": -1000, "JUN26": 500}
    bad3 = {c: f3["oi_chg"][fx_idx[c]] for c, w in want3.items()
            if f3["oi_chg"][fx_idx[c]] != w}
    if bad3:
        errors.append(f"FIXTURE day3: 差分不符 {bad3}（应以 diff 为准，非 CME stored）")
    elif set(f3["unreliable_chg"] or []) != {"FEB26"}:
        errors.append(f"FIXTURE day3: unreliable_chg 应为 ['FEB26']，"
                      f"得到 {f3['unreliable_chg']}")
    else:
        _ok("  PASS  fixture day3: 修订合约取 diff 并标记 unreliable_chg")

    # ── 1b. 移仓进度 fixture ─────────────────────────────────────────────
    # 构造一次完整的 FEB26 -> APR26 移仓：到期月持仓流出、承接月流入，
    # 并让两者在中途交叉（day3 起 APR26 持仓超过 FEB26）。
    #
    # 交叉点是关键：front_remaining 的分子分母都是到期月自己，交叉时必须
    # 毫无反应；旧的 roll_to/(roll_from+roll_to) 会在交叉处换标签并跌回低位。
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

    rolls  = [f["front_remaining"] for f in rf["frames"]]
    froms  = [f["roll_from"] for f in rf["frames"]]
    tos    = [f["roll_to"] for f in rf["frames"]]
    fronts = [f["front"] for f in rf["frames"]]

    if any(v is None for v in rolls):
        errors.append(f"ROLL fixture: front_remaining 不应有 None，得到 {rolls}")
    elif froms != ["FEB26"] * 5:
        # 分母必须全程锁在到期月，不随持仓交叉切换
        errors.append(f"ROLL fixture: roll_from 应恒为 FEB26，得到 {froms}")
    elif tos != ["APR26"] * 5:
        errors.append(f"ROLL fixture: roll_to 应恒为 APR26（跳过 MAR26 存根月），"
                      f"得到 {tos}")
    elif rolls != sorted(rolls, reverse=True):
        errors.append(f"ROLL fixture: front_remaining 应单调递减，得到 {rolls}")
    elif rolls[0] != 1.0:
        # 首帧移仓未启动，到期月持仓正处峰值 → 剩余必须是满值 1
        errors.append(f"ROLL fixture: 首帧 front_remaining 应为 1.0，得到 {rolls[0]}")
    elif rolls[-1] >= 0.1:
        # 末帧移仓近尾声（10000/200000 = 0.05）
        errors.append(f"ROLL fixture: 末帧 front_remaining 应 < 0.1（移仓近尾声），"
                      f"得到 {rolls[-1]}")
    else:
        _ok(f"  PASS  roll fixture: front_remaining {rolls[0]} -> {rolls[-1]} 单调递减")

    # front 应在交叉点切换到承接月，而 roll_from 不跟着切 —— 两者确实解耦
    if fronts != ["FEB26", "FEB26", "APR26", "APR26", "APR26"]:
        errors.append(f"ROLL fixture: front 应在 day3 交叉点切到 APR26，得到 {fronts}")
    else:
        _ok("  PASS  roll fixture: front 随持仓交叉切换，roll_from 保持不动")

    # 无承接月：roll_to 为 None，但 front_remaining 仍应算得出来 ——
    # 它只依赖到期月自身，不需要承接月。这正是换定义带来的好处。
    solo = derive([{"date": "2026-01-05", "months": [
        {"month": "FEB26", "settle": 4000.0, "oi": 100000},
        {"month": "MAR26", "settle": 4010.0, "oi": 200},  # 存根月，低于 1% 下限
    ]}])
    s0 = solo["frames"][0]
    if s0["roll_to"] is not None:
        errors.append(f"ROLL fixture: 无承接月时 roll_to 应为 None，得到 {s0['roll_to']}")
    elif s0["front_remaining"] != 1.0:
        errors.append(f"ROLL fixture: 无承接月时 front_remaining 应为 1.0（单帧即峰值），"
                      f"得到 {s0['front_remaining']}")
    else:
        _ok("  PASS  roll fixture: 无承接月 → roll_to=None 但 front_remaining 仍有值")

    # ── 1c. 跨周期趋势鲁棒性 ─────────────────────────────────────────────
    # 窗口保留 730 条（约两年），会跨越多个移仓周期。若主力月判定依赖跨时间的
    # 持仓量级比较，长期趋势会让一端的周期整段失效：
    #   全局峰值基准 → 持仓涨 5 倍时，最早一个周期的帧全部 front_remaining=null
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
        nulls = [f["date"] for f in tf["frames"] if f["front_remaining"] is None]
        if nulls:
            trend_bad.append(f"{label}: {len(nulls)}/{len(tf['frames'])} 帧失效")
    if trend_bad:
        errors.append("TREND fixture: 主力月判定受持仓量级影响 —— "
                      + "；".join(trend_bad))
    else:
        _ok(f"  PASS  trend fixture: {len(TRENDS)} 种趋势场景（涨 20x ~ 跌 1/20）"
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
            _ok(f"  PASS  {f0['date']}: 首帧 oi_chg 全 None")

    # 抽查 2：2026-07-24 —— CME 未修订，diff 必须与 stored 完全一致
    # 前提是它仍是中段帧：窗口滚动到它成为首帧时无前驱可差分，oi_chg 全 None
    # 是正确行为，此时比对 stored 会得到一堆假失败。
    #
    # 比对范围 = 该帧 window_months（derive 实际计算过 oi_chg 的范围），
    # 不是 contracts（展示列位）。contracts 是全序列各帧 window_months 的并集，
    # 含从未进入本帧窗口的月份 —— 那些格子 derive 根本没算，值是 None，代表
    # 「未计算」而非「算错」。拿 contracts 当校验范围会把前者误判成后者：
    # 实测 07-24 的 contracts 15 列 vs 该帧窗口 13 个，差集 ['JUN26','AUG27']，
    # AUG27 有列位却不在窗口内（stored=+11 / computed=None）。
    # 同一个列表不得同时充当展示范围与校验范围。
    f24 = get_frame("2026-07-24")
    r24 = next((r for r in records if r["date"] == "2026-07-24"), None)
    first_date = result["frames"][0]["date"] if result["frames"] else None
    if f24 is None or r24 is None:
        print("  SKIP  真实数据抽查 2：2026-07-24 已滑出窗口")
    elif f24["date"] == first_date:
        print("  SKIP  真实数据抽查 2：2026-07-24 已成为序列首帧（无前驱）")
    else:
        window24 = {m["month"] for m in window_months(r24.get("months") or [])}
        stored = {m["month"]: m.get("oi_chg") for m in r24.get("months", []) if "oi_chg" in m}
        checked = 0
        mismatches = []
        for contract, stored_val in stored.items():
            if stored_val is None:
                continue
            # 校验范围跟计算范围对齐：窗口外的合约 derive 未计算，不参与对账
            if contract not in window24:
                continue
            ci = contract_idx.get(contract)
            if ci is None:
                continue
            checked += 1
            computed = f24["oi_chg"][ci]
            if computed != stored_val:
                mismatches.append(f"    {contract}: stored={stored_val:+d}  computed={computed}")
        if not checked:
            errors.append(
                "抽查 2 自身失效：2026-07-24 窗口内无一条 stored oi_chg 可对账 "
                f"（窗口 {len(window24)} 个月份，stored {len(stored)} 条）"
            )
        elif mismatches:
            errors.append("MISMATCH 2026-07-24:\n" + "\n".join(mismatches))
        else:
            _ok(f"  PASS  2026-07-24: 窗口内 {checked} 条 oi_chg 与 CME stored 相符"
                  f"（窗口 {len(window24)} 个月份，stored 共 {len(stored)} 条，"
                  f"窗口外 {len(stored) - checked} 条未计算不参与对账）")

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
            _ok("  PASS  2026-07-27: 修订合约已正确标记 unreliable_chg")

    # ── 2b. KPI 算术 fixture ─────────────────────────────────────────────
    # 冻结数据：AUG26=4000.0 / DEC26=4122.0，间隔 122 天。
    # 用一个持仓在移仓中途的快照，让 roll_from=AUG26、roll_to=DEC26。
    KPI_FIXTURE = [
        {"date": "2026-01-05", "months": [
            {"month": "AUG26", "settle": 4000.0, "oi": 200000},
            {"month": "SEP26", "settle": 4030.0, "oi": 300},    # 存根月
            {"month": "DEC26", "settle": 4122.0, "oi": 80000},
        ]},
    ]
    kf = derive(KPI_FIXTURE)["frames"][0]

    # 期望值手算：spread = 4122 - 4000 = 122
    #              gap   = 2026-08-01 → 2026-12-01 = 122 天
    #              ann   = 122 / 4000 * (365 / 122) * 100 = 9.125（全精度，不舍入）
    # 断言比全精度值，容差 1e-9 —— 不用 round/toFixed 对齐后再比，
    # 否则比的是截断后的值，绿得没意义。
    want_ann = 122 / 4000 * (365 / 122) * 100
    EPS = 1e-9

    if kf["roll_from"] != "AUG26" or kf["roll_to"] != "DEC26":
        errors.append(f"KPI fixture: 锚点应为 AUG26→DEC26，"
                      f"得到 {kf['roll_from']}→{kf['roll_to']}")
    elif kf["total_oi"] != 200000:
        # B2 口径：只累加 ever_front。单帧 fixture 里只有 AUG26 当过持仓最大月
        # （200000），SEP26 存根月与 DEC26 承接月都未坐正 → 不计入。
        # 旧口径（全部挂牌月）会得到 280300 = 200000 + 300 + 80000。
        errors.append(f"KPI fixture: total_oi 应为 200000（B2 只含 ever_front），"
                      f"得到 {kf['total_oi']}")
    elif abs(kf["spread"] - 122.0) > EPS:
        errors.append(f"KPI fixture: spread 应为 122.0，得到 {kf['spread']!r}")
    elif kf["spread_gap_days"] != 122:
        # 这条防「月数 × 30」回归：4 个月按 30 天算是 120，会让年化偏高 1.7%
        errors.append(f"KPI fixture: spread_gap_days 应为 122（真实日历天数），"
                      f"得到 {kf['spread_gap_days']} —— 可能退回了「月数×30」")
    elif abs(kf["spread_annualized_pct"] - want_ann) > EPS:
        errors.append(f"KPI fixture: 年化应为 {want_ann!r}，"
                      f"得到 {kf['spread_annualized_pct']!r}")
    elif kf["spread_annualized_pct"] == round(kf["spread_annualized_pct"], 2) \
            and want_ann != round(want_ann, 2):
        # 落盘值恰好等于自身 2 位舍入、而真值不是 → 计算层仍在舍入
        errors.append(f"KPI fixture: 年化似被舍入到 2 位（{kf['spread_annualized_pct']!r}）"
                      f"—— 计算层应落全精度")
    else:
        _ok(f"  PASS  KPI fixture: total_oi=200000（B2 只含 ever_front）"
              f" spread=122.0 gap=122天 "
              f"年化={kf['spread_annualized_pct']!r}（全精度）")

    # 无承接月 → 三个价差字段全 None，但 total_oi 仍要算
    kf2 = derive([{"date": "2026-01-05", "months": [
        {"month": "AUG26", "settle": 4000.0, "oi": 100000},
        {"month": "SEP26", "settle": 4030.0, "oi": 200},   # 低于 1% 下限
    ]}])["frames"][0]
    if kf2["roll_to"] is not None:
        errors.append(f"KPI fixture: 无承接月时 roll_to 应为 None，得到 {kf2['roll_to']}")
    elif (kf2["spread"], kf2["spread_gap_days"],
          kf2["spread_annualized_pct"]) != (None, None, None):
        errors.append(f"KPI fixture: 无承接月时价差三字段应全 None，得到 "
                      f"{kf2['spread']} / {kf2['spread_gap_days']} / "
                      f"{kf2['spread_annualized_pct']}")
    elif kf2["total_oi"] != 100000:
        # B2 口径：只有 AUG26 当过持仓最大月，SEP26 存根月不计入。
        # 旧口径会得到 100200 = 100000 + 200。
        errors.append(f"KPI fixture: 无承接月时 total_oi 仍应算，"
                      f"应为 100000（B2 只含 ever_front），得到 {kf2['total_oi']}")
    else:
        _ok("  PASS  KPI fixture: 无承接月 → 价差三字段 None，"
              "total_oi=100000 仍有值")

    # B2 口径专项：未坐正的末端承接月不得计入 total_oi。
    # major_months() 会用 MIN_NEXT_OI_RATIO 给末端承接月补位，total_oi 若误用
    # 它的返回值就会把那个月算进来，并引入一个未跑分布的阈值。
    # 构造：AUG26 一直最大 → DEC26 反超坐正 → FEB27 在积累但从未最大。
    B2_FIXTURE = [
        {"date": "2026-01-05", "months": [
            {"month": "AUG26", "settle": 4000.0, "oi": 200000},
            {"month": "OCT26", "settle": 4050.0, "oi": 30000},   # 名义月，从未最大
            {"month": "DEC26", "settle": 4122.0, "oi": 50000},
            {"month": "FEB27", "settle": 4180.0, "oi": 5000},
        ]},
        {"date": "2026-01-06", "months": [
            {"month": "AUG26", "settle": 4000.0, "oi": 60000},
            {"month": "OCT26", "settle": 4050.0, "oi": 32000},
            {"month": "DEC26", "settle": 4122.0, "oi": 210000},   # 反超，坐正
            {"month": "FEB27", "settle": 4180.0, "oi": 16000},    # 积累中，未坐正
        ]},
    ]
    b2 = derive(B2_FIXTURE)
    b2f = b2["frames"][1]
    # ever_front = {AUG26, DEC26} → 60000 + 210000 = 270000
    # 若误用 major_months（含 FEB27 补位）会得到 286000
    # 若用旧口径（全部挂牌月）会得到 318000
    if b2f["total_oi"] != 270000:
        errors.append(
            f"B2 fixture: total_oi 应为 270000（仅 AUG26+DEC26 已坐正），"
            f"得到 {b2f['total_oi']} —— "
            f"286000 说明误用了 major_months（含 FEB27 补位，引入 "
            f"MIN_NEXT_OI_RATIO 阈值）；318000 说明退回了全部挂牌月旧口径"
        )
    elif (b2f["roll_from"], b2f["roll_to"]) != ("AUG26", "DEC26"):
        # 此帧 AUG26 仍持 60000（占自身峰值 30%，高于 ROLL_DONE_OI_RATIO），
        # 移仓未完毕 → 配对仍是 AUG26→DEC26。FEB27 要等 AUG26 走完才轮到。
        errors.append(f"B2 fixture: 配对应为 AUG26→DEC26（AUG26 移仓未完毕），"
                      f"得到 {b2f['roll_from']}→{b2f['roll_to']}")
    else:
        # FEB27 在 major_months 里（承接月补位）但不在 total_oi 里 ——
        # 这正是两者口径有意不同的证据
        _ok("  PASS  B2 fixture: total_oi=270000 只含已坐正主角"
              "（FEB27 在 major_months 却不计入 total_oi）")

    # ── 2c. roll_noise 全精度 fixture ────────────────────────────────────
    # 构造一组 oi_chg，使 |到期月变化| / Σ|全部变化| 落在无限小数上：
    #   AUG26 = -1000, DEC26 = +2000, SEP26 = -1  →  1000 / 3001
    # 该值 = 0.33322225924691766...，round(4) 会压成 0.3332，丢掉后续位。
    # 阈值待定、将来要在这列上跑分布，量化地板会影响拐点定位。
    NOISE_FIXTURE = [
        {"date": "2026-01-05", "months": [
            {"month": "AUG26", "settle": 4000.0, "oi": 200000, "oi_chg": 0},
            {"month": "SEP26", "settle": 4030.0, "oi": 300,    "oi_chg": 0},
            {"month": "DEC26", "settle": 4122.0, "oi": 80000,  "oi_chg": 0},
        ]},
        {"date": "2026-01-06", "months": [
            {"month": "AUG26", "settle": 4000.0, "oi": 199000},
            {"month": "SEP26", "settle": 4030.0, "oi": 299},
            {"month": "DEC26", "settle": 4122.0, "oi": 82000},
        ]},
    ]
    nf = derive(NOISE_FIXTURE)["frames"][1]
    want_noise = 1000 / 3001            # 到期月 AUG26 流出 1000，总变动 3001
    EPS_N = 1e-12

    if nf["roll_from"] != "AUG26":
        errors.append(f"NOISE fixture: roll_from 应为 AUG26，得到 {nf['roll_from']}")
    elif nf["roll_noise"] is None:
        errors.append("NOISE fixture: roll_noise 不应为 None")
    elif abs(nf["roll_noise"] - want_noise) > EPS_N:
        errors.append(f"NOISE fixture: roll_noise 应为 {want_noise!r}，"
                      f"得到 {nf['roll_noise']!r}")
    elif nf["roll_noise"] == round(nf["roll_noise"], 4):
        # 真值是无限小数，落盘值若恰等于自身 4 位舍入 → 计算层仍在舍入
        errors.append(f"NOISE fixture: roll_noise 似被舍入到 4 位"
                      f"（{nf['roll_noise']!r}）—— 应落全精度，"
                      f"round(4) 会给待跑的分布垫量化地板")
    elif nf["roll_noise_ma"] is None:
        errors.append("NOISE fixture: roll_noise_ma 不应为 None")
    elif abs(nf["roll_noise_ma"] - want_noise) > EPS_N:
        # 首帧 oi_chg 全 None 不入均值窗口，第二帧 ma == raw
        errors.append(f"NOISE fixture: roll_noise_ma 应为 {want_noise!r}，"
                      f"得到 {nf['roll_noise_ma']!r}")
    elif nf["roll_noise_ma"] == round(nf["roll_noise_ma"], 4):
        errors.append(f"NOISE fixture: roll_noise_ma 似被舍入到 4 位"
                      f"（{nf['roll_noise_ma']!r}）—— 应落全精度")
    else:
        _ok(f"  PASS  NOISE fixture: roll_noise={nf['roll_noise']!r}"
              f" 全精度（未舍入到 4 位）")

    # ── 2b. WINDOW fixture：窗口外的修订合约必须被标记 ────────────────────
    #
    # 真实数据证伪不了这条：当前 24 帧里所有修订合约恰好都落在窗口内，
    # 改走原始 months 对落盘产物零影响（新增 0 条）。所以必须造数据 ——
    # 否则「取证字段不经窗口筛」这个约束没有任何断言守着，退回 oi_map 全绿。
    #
    # window_months 取 near + 1 年为 cutoff：near=AUG26 → cutoff=AUG27，
    # 窗口 = months[:AUG27 的下标 + 1]。故 DEC27 落在窗口外。
    # DEC27 的 stored=-7 而差分=+50，是修订合约，必须出现在 unreliable_chg。
    WINDOW_FIXTURE = [
        {"date": "2026-01-05", "months": [
            {"month": "AUG26", "settle": 4000.0, "oi": 200000, "oi_chg": 0},
            {"month": "DEC26", "settle": 4122.0, "oi": 80000,  "oi_chg": 0},
            {"month": "AUG27", "settle": 4300.0, "oi": 5000,   "oi_chg": 0},
            {"month": "DEC27", "settle": 4400.0, "oi": 1000,   "oi_chg": 0},
        ]},
        {"date": "2026-01-06", "months": [
            {"month": "AUG26", "settle": 4000.0, "oi": 199000, "oi_chg": -1000},
            {"month": "DEC26", "settle": 4122.0, "oi": 82000,  "oi_chg": 2000},
            {"month": "AUG27", "settle": 4300.0, "oi": 5010,   "oi_chg": 10},
            # stored -7 ≠ 差分 +50 —— 修订合约，且在窗口外
            {"month": "DEC27", "settle": 4400.0, "oi": 1050,   "oi_chg": -7},
        ]},
    ]
    wres = derive(WINDOW_FIXTURE)
    wf = wres["frames"][1]
    w_window = [m["month"] for m in window_months(WINDOW_FIXTURE[1]["months"])]
    w_unrel = wf["unreliable_chg"] or []

    if "DEC27" in w_window:
        errors.append(
            f"WINDOW fixture 自身失效：DEC27 应落在窗口外，但 window_months "
            f"返回 {w_window} —— fixture 没在测它要测的路径")
    elif "DEC27" in wres["contracts"]:
        errors.append(
            f"WINDOW fixture 自身失效：DEC27 应不在 X 轴 contracts 里，"
            f"得到 {wres['contracts']}")
    elif "DEC27" not in w_unrel:
        errors.append(
            f"WINDOW fixture: 窗口外的修订合约 DEC27（stored=-7 / 差分=+50）"
            f"未被标记 unreliable_chg，得到 {w_unrel!r} —— 取证字段仍在经"
            f"window_months 筛，窗口末端一移动就漏标")
    elif sorted(w_unrel) != ["DEC27"]:
        errors.append(
            f"WINDOW fixture: unreliable_chg 应只含 DEC27，得到 {w_unrel!r}"
            f" —— 其余三个合约 stored 与差分一致，不该被标记")
    else:
        _ok(f"  PASS  WINDOW fixture: 窗口外修订合约 DEC27 已标记"
              f"（窗口={w_window}，contracts={wres['contracts']}）")

    # ── 2c. _round_floats：落盘精度 ──────────────────────────────────────
    #
    # 新代码路径，若无断言则「13 条护栏全绿」不覆盖它。
    # 只测这个纯函数本身，不测 main() 的写盘（那需要真落盘）。
    rf_in = {
        "f":    1 / 3,                       # 0.3333333333333333
        "i":    42,                          # int 不得变 float
        "b":    True,                        # bool 是 int 子类，不得被 round
        "n":    None,
        "s":    "AUG26",
        "lst":  [1 / 7, 2, None, [1 / 9]],   # 嵌套
        "d":    {"deep": {"x": 1 / 11}},
    }
    rf = _round_floats(rf_in)

    if rf["f"] != round(1 / 3, 12):
        errors.append(f"ROUND fixture: 顶层 float 未 round，得到 {rf['f']!r}")
    elif not isinstance(rf["i"], int) or isinstance(rf["i"], bool) or rf["i"] != 42:
        errors.append(f"ROUND fixture: int 被改动，得到 {rf['i']!r}")
    elif rf["b"] is not True or not isinstance(rf["b"], bool):
        errors.append(f"ROUND fixture: bool 被改动（bool 是 int 子类，"
                      f"不该被 round），得到 {rf['b']!r}")
    elif rf["n"] is not None or rf["s"] != "AUG26":
        errors.append(f"ROUND fixture: None/str 被改动")
    elif rf["lst"][0] != round(1 / 7, 12) or rf["lst"][3][0] != round(1 / 9, 12):
        errors.append(f"ROUND fixture: 列表（含嵌套）内 float 未 round，"
                      f"得到 {rf['lst']!r}")
    elif rf["d"]["deep"]["x"] != round(1 / 11, 12):
        errors.append(f"ROUND fixture: 嵌套 dict 内 float 未 round")
    elif _round_floats(rf) != rf:
        errors.append("ROUND fixture: 不幂等 —— 对已 round 的值再 round 应无变化")
    else:
        _ok(f"  PASS  ROUND fixture: float→12 位、int/bool/None/str 原样、"
              f"嵌套递归、幂等")

    # ── 3. 信封契约 ──────────────────────────────────────────────────────
    # 断言落盘信封字段齐全，且 data 内容与 derive() 的原始返回逐字段一致 ——
    # 信封只是包一层，不该改动任何业务数据。
    #
    # 注意：这里刻意**不**过 _round_floats —— 本条测的是「信封不改业务数据」，
    # 与「落盘时降精度」是两件事。main() 的写盘路径才有 _round_floats。
    env = envelope(
        source="cme_section62_term_structure",
        freq="daily",
        data={k: result[k] for k in ("dates", "contracts", "frames", "scale")},
        dates=result["dates"],
        derived_from=[upstream_ref(IN_PATH, "cme_section62")],
        warnings=result["warnings"],
        info=result["info"],
    )

    REQUIRED = ["schema_version", "source", "freq", "generated_at",
                "date_field", "coverage", "derived_from",
                "warnings", "info", "data"]
    missing_keys = [k for k in REQUIRED if k not in env]
    extra_keys   = [k for k in env if k not in REQUIRED]

    if missing_keys:
        errors.append(f"ENVELOPE: 缺字段 {missing_keys}")
    elif extra_keys:
        errors.append(f"ENVELOPE: 多出未声明字段 {extra_keys}")
    elif env["schema_version"] != 0:
        errors.append(f"ENVELOPE: schema_version 应为 0（格式尚未冻结），"
                      f"得到 {env['schema_version']}")
    elif set(env["data"].keys()) != {"dates", "contracts", "frames", "scale"}:
        errors.append(f"ENVELOPE: data 键集不符，得到 {sorted(env['data'])}")
    elif env["data"]["frames"] != result["frames"]:
        errors.append("ENVELOPE: data.frames 与 derive() 返回不一致 —— 信封改动了业务数据")
    elif env["data"]["scale"] != result["scale"]:
        errors.append("ENVELOPE: data.scale 与 derive() 返回不一致")
    elif env["data"]["contracts"] != result["contracts"]:
        errors.append("ENVELOPE: data.contracts 与 derive() 返回不一致")
    elif "warnings" in env["data"] or "info" in env["data"]:
        errors.append("ENVELOPE: warnings/info 应在信封层，不该留在 data 里")
    else:
        cov = env["coverage"]
        if cov["count"] != len(result["dates"]) \
                or cov["first"] != result["dates"][0] \
                or cov["last"] != result["dates"][-1]:
            errors.append(f"ENVELOPE: coverage 与 dates 不符，得到 {cov}")
        else:
            up = env["derived_from"]
            if not up or up[0]["source"] != "cme_section62":
                errors.append(f"ENVELOPE: derived_from 未记录上游 oi.json，得到 {up}")
            else:
                _ok(f"  PASS  信封: {len(REQUIRED)} 个字段齐全，"
                      f"data 与 derive() 逐字段一致，schema_version=0")

    # ── 4. 上游非空闸门 ──────────────────────────────────────────────────
    #
    # derive() 对空输入是宽容的（:377-378 的 `if records else set()` 兜住空
    # 列表不崩），空上游会算出 frames=0 / contracts=0 的空派生并照常落盘，
    # 把好产物覆盖掉且全绿无声。这五条守着那道闸。
    #
    # 前四条测「该拒时拒」，第五条测「不误伤」—— 只有前四条的话，把闸门写成
    # 无条件 return 也能全绿。
    GATE_CASES = [
        ("空数组 []",              [],                                    "b)"),
        ("空对象 {}",              {},                                    "a)"),
        ("非 list（字符串）",       "oops",                                "a)"),
        ("单帧但 months 为空",      [{"date": "2026-07-31", "months": []}], "c)"),
        ("多帧 months 全为空",      [{"date": "2026-07-30", "months": []},
                                    {"date": "2026-07-31", "months": []}], "c)"),
    ]
    gate_bad = []
    for label, payload, want_prefix in GATE_CASES:
        got = check_upstream_nonempty(payload)
        if got is None:
            gate_bad.append(f"{label}: 应被拒绝，实际放行")
        elif not got.startswith(want_prefix):
            gate_bad.append(f"{label}: 应命中判据 {want_prefix}，实际 {got!r}")
    if gate_bad:
        errors.append("GATE: 上游非空闸门漏放 —— " + "；".join(gate_bad))
    else:
        _ok(f"  PASS  上游非空闸门: {len(GATE_CASES)} 种空上游全部拒绝"
            f"（判据 a/b/c 分别命中）")

    # 不误伤：真实 records 必须放行。records 为空时（oi.json 本身空）跳过 ——
    # 那种情况下这条断言无数据可测，报 SKIP 而非假绿。
    if not records:
        print("  SKIP  上游非空闸门: oi.json 为空，无法测「不误伤」")
    else:
        gate_real = check_upstream_nonempty(records)
        if gate_real is not None:
            errors.append(f"GATE: 闸门误伤真实数据（{len(records)} 帧）—— "
                          f"命中 {gate_real!r}")
        else:
            _ok(f"  PASS  上游非空闸门: {len(records)} 帧真实数据放行（不误伤）")

    # 边界：只要有一帧有 months 就该放行 —— 判据 c) 是「全部为空」，
    # 不是「任一为空」。写成 any() 会把「某天数据缺失」误判成上游整体为空。
    partial = [{"date": "2026-07-30", "months": []},
               {"date": "2026-07-31", "months": [
                   {"month": "AUG26", "settle": 4000.0, "oi": 100}]}]
    if check_upstream_nonempty(partial) is not None:
        errors.append("GATE: 部分帧有 months 时被误拒 —— 判据 c) 应为"
                      " all() 而非 any()")
    else:
        _ok("  PASS  上游非空闸门: 部分帧空、至少一帧有合约 → 放行")

    # 闸门自身可证伪：把判据换成无条件放行，上面五条必须变红。
    # 这里不改代码，只断言「五条用例确实各有判据命中」，即闸门不是空壳。
    prefixes = {check_upstream_nonempty(p)[:2]
                for _, p, _ in GATE_CASES}
    if prefixes != {"a)", "b)", "c)"}:
        errors.append(f"GATE: 三条判据未全部被覆盖，实际命中 {sorted(prefixes)}"
                      f" —— 有判据从未被任何用例触发，等于没测")
    else:
        _ok("  PASS  上游非空闸门: 三条判据 a/b/c 均被用例覆盖")

    if errors:
        print("\nFAILURES:")
        for e in errors:
            print(" ", e)
    # 通过数与失败数一起报，且只走一条路径 —— 全绿时也照此格式输出。
    #
    # 只报失败数会漏掉「用例静默减少」：真实数据抽查在日期滑出 730 条窗口时
    # 转 SKIP（见本函数文档），条数从 17 掉到 3 也是 "0 failed" 全绿。
    # 通过数是唯一能看出这件事的量。
    print(f"\n{_PASS_COUNT} passed, {len(errors)} failed")
    if errors:
        sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────────

# 落盘精度：写 JSON 那一刻统一 round，计算链全程保持全精度。
#
# 起因：Actions runner 与本机对同一输入算出的 float 末位可能差 1 ULP
# （实测 roll_noise_ma 3 帧，Δ 量级 1e-17），谁最后跑谁的值进仓库，
# 每次往返产生无意义的 git diff。
#
# 12 位是「远超任何业务精度需求，又远低于 float64 的 ~15-17 位有效数字」
# 的位置：既不丢真实信息，又把两平台的末位分歧吃掉。
#
# 只降低概率，不消除：某值第 13 位若恰在舍入边界，两平台仍可能一边进一边退。
# 禁止改用容差比对来「彻底解决」—— 容差会把真实的微小变化也判为没变。
ROUND_NDIGITS = 12


def _round_floats(o):
    """
    递归 round 所有 float，其余类型原样透传。

    bool 是 int 的子类但不是 float，不会被 round —— 无需特判。
    int 保持 int（不转 float），None 保持 None。
    已在计算层有意 round(4) 的字段（front_remaining）再 round(12) 是幂等的，
    不会被改动。
    """
    if isinstance(o, float):
        return round(o, ROUND_NDIGITS)
    if isinstance(o, dict):
        return {k: _round_floats(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_round_floats(v) for v in o]
    return o


def check_upstream_nonempty(records) -> str | None:
    """
    上游非空闸门。返回命中的判据描述；None 表示通过。

    为什么需要这道闸：derive() 对空输入是「宽容」的 —— :377-378 的
    `if records else set()` 兜住空列表不崩，于是空上游会算出 frames=0 /
    contracts=0 的空派生，照常 exit 0 落盘，把好的产物覆盖掉且全绿无声。
    派生数据本可重算，但重算的前提是上游还在；上游空了还去覆盖产物，
    等于把「这次没读到数据」变成「历史数据不存在」。

    exit 1 而非 exit 2：oi.json 文件存在却内容为空，不是「上游未更新」
    （那由 fetch_oi 判定，它拿不到 PDF 时 exit 0 且不动 oi.json），
    而是文件被写坏或被清空 —— 需人工介入。
    """
    if not isinstance(records, list):
        return (f"a) 顶层不是 list（得到 {type(records).__name__}）"
                f" —— oi.json 应为帧数组")
    if len(records) == 0:
        return "b) records 长度为 0 —— 无任何帧"
    if all(not (r.get("months") if isinstance(r, dict) else None)
           for r in records):
        return (f"c) 全部 {len(records)} 帧的 months 均为空"
                f" —— 无任何合约行")
    return None


def main():
    run_test_mode = "--test" in sys.argv

    # 经 unwrap 读：oi.json 信封化前后都取到同一份帧数组。
    # 直读 json.load() 的返回值会在写入端信封化那天崩在这里 —— 且症状是
    # check_upstream_nonempty 命中「a) 顶层不是 list」→ exit 1 保留上一份派生，
    # 看起来像上游没数据，而实际上数据好着，只是形状变了。
    with open(IN_PATH, encoding="utf-8") as f:
        records = unwrap(json.load(f), strict=True)
    print(f"Loaded {len(records)} records from {IN_PATH}")

    if run_test_mode:
        run_tests(records)
        return

    # 上游非空闸门：拒绝用空产物覆盖好数据。必须在 derive() 之前 ——
    # derive() 对空输入不抛错，放过去就会落盘。
    empty_reason = check_upstream_nonempty(records)
    if empty_reason is not None:
        print(f"上游 oi 无数据，保留上一份派生：{empty_reason}", file=sys.stderr)
        print(f"::error title=派生层上游为空::"
              f"oi.json 无可用数据，term-structure-series.json 未更新。"
              f"命中判据：{empty_reason}", file=sys.stderr)
        sys.exit(1)

    # 清理上次崩溃留下的临时文件（只清本文件自己的，见 basename 隔离）
    swept = sweep_stale_tmp(OUT_PATH)
    if swept:
        print(f"清理上次残留的临时文件 {len(swept)} 个")

    result = derive(records)

    for i in result.get("info") or []:
        print("INFO:", i)

    if result["warnings"]:
        print("Warnings:")
        for w in result["warnings"]:
            print(" ", w)

    # warnings / info 提到信封层，data 只留业务数据
    #
    # _round_floats 只包在 data 上，且在此处（写盘前最后一刻）才调用 ——
    # derive() 的返回值仍是全精度，roll_noise_ma 的 3 帧滚动用的是未 round
    # 的 roll_noise，算完才在这里统一 round。
    data = _round_floats({
        "dates":     result["dates"],
        "contracts": result["contracts"],
        "frames":    result["frames"],
        "scale":     result["scale"],
    })

    # ── 幂等跳过：业务数据逐字段相同则不写盘 ────────────────────────────
    #
    # 只比 data，不含任何信封元数据 —— generated_at 每次都不同，带进来比
    # 永不相等，幂等等于没做。
    #
    # 可以逐字段相等比较、不需要容差：落盘前已过 _round_floats(12 位)，
    # 跨平台 1 ULP 的差异在第 13 位以后，round 之后消失。实测依据 ——
    # a31cdcb（round12）之后连续三次 data-bot 跑动 44ca17e / bed4445 /
    # 247bcb6 的 roll_noise_ma 翻转帧数均为 0；247bcb6 里 25 帧业务数据
    # 一字未变，整文件唯一变动的叶子是顶层 generated_at。
    #
    # 引容差是禁止的：CFTC/CME 的历史修订可能只差几个单位，容差会把真实的
    # 微小变化也判成没变。
    prev = unwrap(read_json_or(OUT_PATH, None), strict=True)
    if prev == data:
        print(f"业务数据与上一份逐字段相同，跳过写入"
              f"（generated_at 不刷新，git 无 diff）")
        return

    payload = envelope(
        source="cme_section62_term_structure",
        freq="daily",
        data=data,
        dates=result["dates"],
        derived_from=[upstream_ref(IN_PATH, "cme_section62")],
        warnings=result["warnings"],
        info=result["info"],
    )
    # 原子写：崩在中途只留临时文件，term-structure-series.json 保持完整旧版
    atomic_write_json(OUT_PATH, payload)

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
