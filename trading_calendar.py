#!/usr/bin/env python3
"""
CME 交易日历（COMEX 金属）。

采集层（fetch_oi.py）与派生层（derive_term_structure.py）共用同一份假日表，
避免两处各存一份导致校验口径不一致。

新增年份时只需扩展 CME_HOLIDAYS。
"""

from datetime import date, timedelta

# 落在工作日上的美国联邦 / CME 假日（周末无需列出）
CME_HOLIDAYS = {
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
    "2027-05-31", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2025
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
}

# 假日表的覆盖范围。超出此范围时交易日判定只能依赖周末规则，
# 会把未登记的假日误判为交易日 —— 调用方应据此告警而非静默通过。
_COVERED_YEARS = {int(d[:4]) for d in CME_HOLIDAYS}
CALENDAR_MIN_YEAR = min(_COVERED_YEARS)
CALENDAR_MAX_YEAR = max(_COVERED_YEARS)


def is_calendar_covered(d: date) -> bool:
    """假日表是否覆盖该日期所在年份。"""
    return CALENDAR_MIN_YEAR <= d.year <= CALENDAR_MAX_YEAR


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in CME_HOLIDAYS


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def latest_trading_day_on_or_before(d: date) -> date:
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_days_between(a: date, b: date) -> int:
    """a 与 b 之间的交易日数量（不含两端）。"""
    count = 0
    d = a + timedelta(days=1)
    while d < b:
        if is_trading_day(d):
            count += 1
        d += timedelta(days=1)
    return count
