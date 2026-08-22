#!/usr/bin/env python3
"""
所有落盘 JSON 的统一信封。

元数据在外、业务数据在 data 里 —— 加新数据源（FRED / SPDR ETF / 上海黄金 /
COMEX 期权）时信封字段不会和业务字段撞名。

    {
      "schema_version": 0,
      "source":       "cme_section62",
      "freq":         "daily",
      "generated_at": "2026-07-31T03:20:00Z",
      "date_field":   "date",          // 跨源 join 的契约：时间轴字段叫什么
      "coverage":     {"first": ..., "last": ..., "count": N},
      "derived_from": [{"source": ..., "generated_at": ..., "coverage": {...}}],
      "warnings":     [],
      "info":         [],
      "data":         { ...业务数据原样... }
    }

schema_version 从 0 开始：格式还要为四个新数据源调整，现在是「尚未稳定、
随时可破坏性变更」的状态。等所有源接完、格式冻结再升 1。
"""

import json, os
from datetime import datetime, timezone

# 0 = 尚未稳定。加完 FRED / SPDR / 上海黄金 / COMEX 期权并冻结格式后升 1。
SCHEMA_VERSION = 0

# 时间轴字段名。四类现有数据恰好都叫 date，但那是巧合而非约定 ——
# 显式写进信封，跨源 join 时按这个字段取，不靠猜。
DEFAULT_DATE_FIELD = "date"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def coverage_of(dates: list[str]) -> dict:
    """时间跨度摘要。dates 须已按升序排好。"""
    if not dates:
        return {"first": None, "last": None, "count": 0}
    return {"first": dates[0], "last": dates[-1], "count": len(dates)}


def upstream_ref(path: str, source: str) -> dict | None:
    """
    读一个上游文件，摘出它的身份信息供 derived_from 使用。

    上游若已是信封格式，取其 source/generated_at/coverage；若还是裸数组
    （四个采集脚本本轮未迁移），退化为按 date 字段自行汇总，并标记
    envelope=False 以便日后审计哪些源还没迁。
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    if isinstance(raw, dict) and "schema_version" in raw:
        return {
            "source":       raw.get("source", source),
            "generated_at": raw.get("generated_at"),
            "coverage":     raw.get("coverage"),
            "envelope":     True,
        }

    # 裸格式：尽力汇总
    rows = raw if isinstance(raw, list) else raw.get("weekly") or []
    dates = sorted(r["date"] for r in rows
                   if isinstance(r, dict) and r.get("date"))
    return {
        "source":       source,
        "generated_at": None,          # 裸格式没有这个字段
        "coverage":     coverage_of(dates),
        "envelope":     False,
    }


def envelope(source: str, freq: str, data,
             *, dates: list[str] | None = None,
             date_field: str = DEFAULT_DATE_FIELD,
             derived_from: list[dict] | None = None,
             warnings: list[str] | None = None,
             info: list[str] | None = None) -> dict:
    """
    包装业务数据。data 原样放进 data 键，不做任何改写。
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "source":         source,
        "freq":           freq,
        "generated_at":   utc_now_iso(),
        "date_field":     date_field,
        "coverage":       coverage_of(dates or []),
        "derived_from":   [r for r in (derived_from or []) if r],
        "warnings":       warnings or [],
        "info":           info or [],
        "data":           data,
    }


# 落盘一律走 io_utils.atomic_write_json()。
#
# 本模块曾有一个 write_json()（非原子，直接 open+json.dump），靠注释写着
# 「需要原子性的用 io_utils」拦着 —— 注释挡不住误用：谁顺手 import 就绕过了
# 原子写保证，且不会有任何 verify 报警（内容是对的，只有崩溃时才暴露，
# 而崩溃不在测试路径上）。已删除，不留改名版 —— 留着仍是个能被 import 的
# 非原子入口。


# ── 读取端 ────────────────────────────────────────────────────────────────────

# 已知的 schema_version 集合。读到不在其中的值必须拒绝，不能按旧格式硬解 ——
# 未来版本可能改了 data 的内部结构，静默解析会读出错的业务数据。
KNOWN_SCHEMA_VERSIONS = frozenset({0})

REQUIRED_ENVELOPE_KEYS = (
    "schema_version", "source", "freq", "generated_at",
    "date_field", "coverage", "derived_from", "warnings", "info", "data",
)

# 频率白名单。往里加一项等于**放宽**校验，而放宽本身不会让任何断言变红 ——
# 所以 tools/verify-io-utils.py 里配了一条「VALID_FREQ 恰为这四项」的锚：
# 增或删任何一项都会红，逼人先来这里看清楚再改。
# quarterly：Treasury Bulletin 的债务持有人结构与 BEA 名义 GDP 都是季频，
# 观测日期按季度首月 1 日标记（2026-01-01 = 2026 Q1）。
VALID_FREQ = frozenset({"daily", "weekly", "monthly", "quarterly"})


def is_envelope(payload) -> bool:
    """是否信封格式。只看 schema_version 是否存在，不校验内容。"""
    return isinstance(payload, dict) and "schema_version" in payload


def assert_envelope(payload) -> None:
    """
    校验信封完整性。不合格抛 ValueError，不返回布尔 ——
    调用方要么拿到合格信封，要么拿到明确的错误，没有第三种。

    类型也是契约的一部分：schema_version 必须是 int，字符串 "0" 不接受。
    """
    if not isinstance(payload, dict):
        raise ValueError(
            f"信封必须是 dict，得到 {type(payload).__name__}")

    missing = [k for k in REQUIRED_ENVELOPE_KEYS if k not in payload]
    if missing:
        raise ValueError(f"信封缺字段：{missing}")

    sv = payload["schema_version"]
    if not isinstance(sv, int) or isinstance(sv, bool):
        raise ValueError(
            f"schema_version 必须是 int，得到 {type(sv).__name__}: {sv!r}")
    if sv not in KNOWN_SCHEMA_VERSIONS:
        raise ValueError(
            f"未知的 schema_version={sv}（已知 "
            f"{sorted(KNOWN_SCHEMA_VERSIONS)}）—— 拒绝按旧格式解析，"
            f"该文件可能来自更新的写入方"
        )

    if payload["freq"] not in VALID_FREQ:
        raise ValueError(
            f"freq 必须是 {sorted(VALID_FREQ)} 之一，得到 {payload['freq']!r}")


def unwrap(payload, *, strict: bool = False):
    """
    取出业务数据。信封格式返回 payload["data"]，裸格式原样返回。

    strict=True 时裸格式抛错（迁移完成后可用它锁死回退路径）。
    strict=False 是过渡期默认：四源尚未全部迁移。

    信封格式一律先过 assert_envelope —— 宁可拒绝也不静默按旧格式解析。
    """
    if is_envelope(payload):
        assert_envelope(payload)
        return payload["data"]
    if strict:
        raise ValueError(
            "期望信封格式，得到裸格式（无 schema_version）")
    return payload
