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


def write_json(path: str, payload: dict, *, compact: bool = True) -> None:
    """
    单点落盘：目录创建、分隔符、编码统一在这里，各脚本不再各写一遍。

    compact=True 用于派生文件（体积敏感、机器读）；
    compact=False 用于原始采集文件（人要 review diff）。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(payload, f, ensure_ascii=False, indent=2)
