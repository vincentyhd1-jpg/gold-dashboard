#!/usr/bin/env python3
"""Derive macro rates, CPI YoY and federal debt structure from the 13 FRED envelopes."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

from data_envelope import assert_envelope, envelope, upstream_ref
from io_utils import atomic_write_json, read_json_or

ROOT = Path(__file__).resolve().parent
RATES_OUT = ROOT / "data" / "derived" / "macro_rates.json"
CPI_OUT = ROOT / "data" / "derived" / "macro_cpi.json"
DEBT_OUT = ROOT / "data" / "derived" / "macro_debt.json"

RATES_INPUTS = (
    ("ust_dgs2", "data/ust_dgs2.json", "dgs2"),
    ("ust_dgs10", "data/ust_dgs10.json", "dgs10"),
    ("ust_dgs30", "data/ust_dgs30.json", "dgs30"),
    ("fed_target_upper", "data/fed_target_upper.json", "target_upper"),
    ("fed_target_lower", "data/fed_target_lower.json", "target_lower"),
    ("fed_effective_rate", "data/fed_effective_rate.json", "dff"),
)
CPI_INPUTS = (
    ("cpi_cpiaucsl", "data/cpi_cpiaucsl.json", "cpiaucsl"),
    ("cpi_cpilfesl", "data/cpi_cpilfesl.json", "cpilfesl"),
)
DEBT_INPUTS = (
    ("debt_total", "data/debt_total.json", "debt_total"),
    ("debt_held_public", "data/debt_held_public.json", "debt_public"),
    ("debt_intragov", "data/debt_intragov.json", "debt_intragov"),
    ("debt_foreign", "data/debt_foreign.json", "debt_foreign"),
    ("gdp_nominal", "data/gdp_nominal.json", "gdp"),
)
ALL_INPUTS = RATES_INPUTS + CPI_INPUTS + DEBT_INPUTS

# 派生层统一归一到十亿美元（meta.units="billions_usd"）。
# FRED 原始单位（fetch_fred.py 已把它写进各上游 info 的 units= 行）：
#   GFDEBTN / FYGFDPUN / FDHBATN  Millions of Dollars  → 这里 ÷1000
#   FDHBFIN / GDP                 Billions of Dollars  → 原值不动
# 归一到十亿而非百万，是为了 debt_bn / gdp_bn * 100 不需要额外系数。
DEBT_FIELDS = ("debt_total", "debt_public", "debt_intragov", "debt_foreign", "gdp")
DEBT_MILLIONS_FIELDS = frozenset({"debt_total", "debt_public", "debt_intragov"})

# 堆叠三分量。只有这三个字段会被置 null —— total_bn / debt_gdp_pct 永不受影响。
STACK_FIELDS = ("intragov_bn", "domestic_public_bn", "foreign_bn")

# 恒等式阈值。1990+ 真实数据中绝大多数季度精确相等；2000 Q3、2013 Q4、
# 2014 Q1 的相对偏差分别约 9e-6 / 1.1e-2 / 1.4e-3，按既有 strict 语义只把
# 当季结构三分量置 null。不能为保住这三根柱放宽阈值，否则会掩盖真实源间不一致。
IDENTITY_TOL = 1e-6

# 量级带。越界只进 warning，不失败 —— 债务/GDP 比值没有确定性上下界，
# 硬闸门会在真实的极端年份误杀（停止线 §12.3#9）。
RATIO_WARN_MIN = 50.0
RATIO_WARN_MAX = 300.0


class DeriveFailure(Exception):
    pass


def _read_envelope(path: Path, label: str) -> dict:
    payload = read_json_or(str(path), None)
    if not isinstance(payload, dict):
        raise DeriveFailure(f"{label}: 缺少或不是信封")
    try:
        assert_envelope(payload)
    except ValueError as exc:
        raise DeriveFailure(f"{label}: {exc}") from exc
    if not isinstance(payload.get("data"), list):
        raise DeriveFailure(f"{label}: data 必须是数组")
    return payload


def _series_map(payload: dict, label: str) -> tuple[dict[str, float | None], str | None]:
    result = {}
    non_null = False
    for row in payload["data"]:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise DeriveFailure(f"{label}: data 点格式非法")
        key = row["date"]
        if key in result:
            raise DeriveFailure(f"{label}: 日期重复: {key}")
        value = row.get("value")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise DeriveFailure(f"{label}: value 类型非法: {value!r}")
        if value is not None:
            non_null = True
        result[key] = value
    if not result:
        return result, "empty"
    if not non_null:
        return result, "all-null"
    return result, None


def _month_key(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise DeriveFailure(f"CPI 日期非法: {value}") from exc
    return value[:7]


def _yoy(series: dict[str, float | None], current_date: str) -> float | None:
    current = series.get(current_date)
    if current is None:
        return None
    prior_key = f"{int(current_date[:4]) - 1:04d}-{current_date[5:7]}-01"
    prior = series.get(prior_key)
    if prior is None:
        return None
    if prior == 0:
        raise DeriveFailure(f"CPI 去年同期为 0: {prior_key}")
    return current / prior - 1


def _outer_join(series: dict[str, dict[str, float | None]]) -> list[dict]:
    dates = sorted({d for values in series.values() for d in values})
    return [{"date": d, **{name: values.get(d) for name, values in series.items()}} for d in dates]


def _coverage_text(rows: list[dict]) -> str:
    if not rows:
        return "empty"
    return f"{rows[0]['date']}..{rows[-1]['date']} count={len(rows)}"


def _warnings(payloads: dict[str, dict]) -> list[str]:
    return [f"{label}: {warning}" for label, payload in payloads.items() for warning in payload.get("warnings", [])]


def _load_group(root: Path, specs: tuple[tuple[str, str, str], ...]) -> tuple[dict[str, dict], dict[str, dict[str, float | None]], list[str], list[str]]:
    payloads: dict[str, dict] = {}
    maps: dict[str, dict[str, float | None]] = {}
    failures: list[str] = []
    refs: list[str] = []
    for label, relpath, field in specs:
        try:
            payload = _read_envelope(root / relpath, label)
            values, status = _series_map(payload, label)
        except DeriveFailure as exc:
            failures.append(str(exc))
            continue
        if status:
            failures.append(f"{label}: data {status}")
            continue
        payloads[label] = payload
        maps[field] = values
        refs.append(upstream_ref(str(root / relpath), label))
    return payloads, maps, refs, failures


def _build_rates(root: Path, warning_payloads: dict[str, dict] | None = None) -> tuple[dict | None, list[str]]:
    payloads, maps, refs, failures = _load_group(root, RATES_INPUTS)
    if failures:
        return None, failures
    ust = _outer_join({k: maps[k] for k in ("dgs2", "dgs10", "dgs30")})
    fed = _outer_join({k: maps[k] for k in ("target_upper", "target_lower", "dff")})
    info = [
        f"ust_coverage={_coverage_text(ust)}",
        f"fed_coverage={_coverage_text(fed)}",
        "date_alignment=outer_join",
        "missing_values=null",
    ]
    dates = sorted({row["date"] for row in ust} | {row["date"] for row in fed})
    payload = envelope(
        "derived_macro_rates",
        "daily",
        {"ust": ust, "fed": fed},
        dates=dates,
        derived_from=refs,
        warnings=_warnings(warning_payloads or payloads),
        info=info,
    )
    return payload, []


def _build_cpi(root: Path, warning_payloads: dict[str, dict] | None = None) -> tuple[dict | None, list[str]]:
    payloads, maps, refs, failures = _load_group(root, CPI_INPUTS)
    if failures:
        return None, failures
    cpi_rows = []
    for row in _outer_join({k: maps[k] for k in ("cpiaucsl", "cpilfesl")}):
        d = row["date"]
        cpi_rows.append({
            "date": d,
            "ref_period": _month_key(d),
            "cpiaucsl": row["cpiaucsl"],
            "cpiaucsl_yoy": _yoy(maps["cpiaucsl"], d),
            "cpilfesl": row["cpilfesl"],
            "cpilfesl_yoy": _yoy(maps["cpilfesl"], d),
            "published_at": None,
        })
    latest = cpi_rows[-1] if cpi_rows else None
    info = [
        f"cpiaucsl_coverage={_coverage_text([r for r in cpi_rows if r['cpiaucsl'] is not None])}",
        f"cpilfesl_coverage={_coverage_text([r for r in cpi_rows if r['cpilfesl'] is not None])}",
        "date_alignment=outer_join",
        "missing_values=null",
        "yoy=exact_prior_year_month",
        "published_at=not_collected",
    ]
    payload = envelope(
        "derived_macro_cpi",
        "monthly",
        {
            "cpi": cpi_rows,
            "meta": {
                "cpi_latest_ref_period": latest["ref_period"] if latest else None,
                "cpi_latest_published_at": None,
                "cpi_release_date_available": False,
            },
        },
        dates=[row["date"] for row in cpi_rows],
        derived_from=refs,
        warnings=_warnings(warning_payloads or payloads),
        info=info,
    )
    return payload, []


def _quarter_label(value: str) -> str:
    """2026-01-01 → 2026-Q1。季频观测日期按季度首月 1 日标记。"""
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise DeriveFailure(f"debt: 季频日期格式非法: {value!r}")
    month = value[5:7]
    if month not in ("01", "04", "07", "10") or value[8:] != "01":
        raise DeriveFailure(f"debt: 非季度首日: {value!r}")
    return f"{value[:4]}-Q{(int(month) - 1) // 3 + 1}"


def _quarter_index(value: str) -> int:
    return int(value[:4]) * 4 + (int(value[5:7]) - 1) // 3


def _blank_stack(row: dict) -> None:
    """
    全脚本唯一产生 null 堆叠分量的地方。

    两条路径共用它，所以不可能出现「半个堆叠」：
      1. 个别季度闸门失败（恒等式破裂 / 本国公众为负）
      2. stack_last 之后的右端截断（外国持有比另两条少一季）
    只置 STACK_FIELDS 三项 —— total_bn 与 debt_gdp_pct 照常输出到各自最末季。
    """
    for field in STACK_FIELDS:
        row[field] = None


def _build_debt(maps: dict[str, dict[str, float | None]], refs: list[str],
                payloads: dict[str, dict]) -> dict:
    scaled = {
        field: (
            {d: (None if v is None else v / 1000.0) for d, v in maps[field].items()}
            if field in DEBT_MILLIONS_FIELDS else dict(maps[field])
        )
        for field in DEBT_FIELDS
    }

    rows: list[dict] = []
    for joined in _outer_join(scaled):
        d = joined["date"]
        total, public, intragov = joined["debt_total"], joined["debt_public"], joined["debt_intragov"]
        foreign, gdp = joined["debt_foreign"], joined["gdp"]
        rows.append({
            "date": d,
            "quarter": _quarter_label(d),
            "total_bn": total,
            # C14 的日频金额线在 Treasury 覆盖前仍需一条真实季度公众持有历史；
            # 该字段直接来自 FYGFDPUN，不在前端用 domestic+foreign 反算。
            "public_bn": public,
            "intragov_bn": intragov,
            # 本国公众 = 公众持有 − 外国持有。外国缺失时被减数不全，整项 undefined。
            "domestic_public_bn": None if public is None or foreign is None else public - foreign,
            "foreign_bn": foreign,
            "gdp_bn": gdp,
            # 同季配对；GDP 用 SAAR 原值，不除以 4（存量比年化流量的惯例口径）。
            "debt_gdp_pct": None if total is None or not gdp else total / gdp * 100.0,
            "public_gdp_pct": None if public is None or not gdp else public / gdp * 100.0,
        })

    # ── 闸门先算「坏季度集合」，再按附录 A.1 分级：全部坏 → d 类；个别坏 → 置 null ──
    identity_scope: list[str] = []
    identity_bad: list[str] = []
    identity_max = 0.0
    for d in sorted(set(scaled["debt_total"]) | set(scaled["debt_public"]) | set(scaled["debt_intragov"])):
        total = scaled["debt_total"].get(d)
        public = scaled["debt_public"].get(d)
        intragov = scaled["debt_intragov"].get(d)
        if total is None or public is None or intragov is None or not total:
            continue
        rel = abs(total - (public + intragov)) / abs(total)
        identity_scope.append(d)
        identity_max = max(identity_max, rel)
        if rel > IDENTITY_TOL:
            identity_bad.append(d)

    negative_scope: list[str] = []
    negative_bad: list[str] = []
    for d in sorted(set(scaled["debt_public"]) | set(scaled["debt_foreign"])):
        public = scaled["debt_public"].get(d)
        foreign = scaled["debt_foreign"].get(d)
        if public is None or foreign is None:
            continue
        negative_scope.append(d)
        if public - foreign < 0:
            negative_bad.append(d)

    # d 类。本文件没有 quarantine，也没有 data:null 通道 —— write_outputs() 见
    # payload=None 直接跳过，旧文件原样留着，main() 返回 1。这正是 §7.3 验收里
    # 「恒等式破裂 → 失败且不覆盖旧文件」要的行为：写 data:null 反而会把上一份
    # 好数据冲掉。附录 A.1 的「整份 data:null」是采集层的措辞（那里有 quarantine
    # 兜住原始响应），派生层的对应物就是不落盘 + exit 1。
    if identity_scope and len(identity_bad) == len(identity_scope):
        raise DeriveFailure(
            f"debt: 恒等式在全部 {len(identity_scope)} 个季度破裂"
            f"（max_abs_rel={identity_max:.3e} > {IDENTITY_TOL:g}）—— d 类，不覆盖旧文件")
    if negative_scope and len(negative_bad) == len(negative_scope):
        raise DeriveFailure(
            f"debt: 本国公众持有在全部 {len(negative_scope)} 个季度为负 —— d 类，不覆盖旧文件")

    warnings = _warnings(payloads)
    by_date = {r["date"]: r for r in rows}
    for d in sorted(set(identity_bad) | set(negative_bad)):
        _blank_stack(by_date[d])
    if identity_bad:
        warnings.append(
            f"恒等式个别季破裂（max_abs_rel={identity_max:.3e}），该季三分量置 null: "
            + ", ".join(identity_bad))
    if negative_bad:
        warnings.append(
            "本国公众持有个别季为负，该季三分量置 null: " + ", ".join(negative_bad))

    # stack_last 由数据算：三分量同时非 null 的最末季。ALFRED 无法确认外国持有的
    # 滞后是常数，所以不写死滞后季数（§13.4）。其后每季三分量一律置 null ——
    # 约束落在数据里，不靠前端自觉；只画两个分量会让堆叠顶边掉 ~9.3 万亿，
    # 读起来像「债务单季跌 24%」，比留一个可见的缺口更坏（§9.1）。
    stack_last = next((r["date"] for r in reversed(rows)
                       if all(r[f] is not None for f in STACK_FIELDS)), None)
    for row in rows:
        if stack_last is None or row["date"] > stack_last:
            _blank_stack(row)

    total_last = next((r["date"] for r in reversed(rows) if r["total_bn"] is not None), None)
    ratio_last = next((r["date"] for r in reversed(rows) if r["debt_gdp_pct"] is not None), None)
    foreign_last = next((r["date"] for r in reversed(rows) if r["foreign_bn"] is not None), None)
    gdp_last = next((r["date"] for r in reversed(rows) if r["gdp_bn"] is not None), None)

    out_of_band = [f"{r['quarter']}={r['debt_gdp_pct']:.2f}" for r in rows
                   if r["debt_gdp_pct"] is not None
                   and not (RATIO_WARN_MIN <= r["debt_gdp_pct"] <= RATIO_WARN_MAX)]
    if out_of_band:
        warnings.append(
            f"debt_gdp_pct 越出 [{RATIO_WARN_MIN:g}, {RATIO_WARN_MAX:g}]（只 warning 不失败）: "
            + ", ".join(out_of_band))

    meta = {
        "units": "billions_usd",
        "stack_last": stack_last,
        "total_last": total_last,
        "ratio_last": ratio_last,
        "foreign_last": foreign_last,
        "gdp_last": gdp_last,
        "foreign_lag_quarters": (None if stack_last is None or total_last is None
                                 else _quarter_index(total_last) - _quarter_index(stack_last)),
        "identity_max_abs_rel": identity_max,
        "identity_bad_quarters": identity_bad,
        "negative_domestic_quarters": negative_bad,
    }
    return envelope(
        "derived_macro_debt", "quarterly", {"debt": rows, "meta": meta},
        dates=[r["date"] for r in rows], derived_from=refs, warnings=warnings,
        info=[
            "units_source=FRED metadata: GFDEBTN/FYGFDPUN/FDHBATN=Millions of Dollars, "
            "FDHBFIN/GDP=Billions of Dollars",
            "units_normalized=millions_divided_by_1000",
            f"debt_coverage={_coverage_text([r for r in rows if r['total_bn'] is not None])}",
            f"stack_coverage={_coverage_text([r for r in rows if r['foreign_bn'] is not None])}",
            f"ratio_coverage={_coverage_text([r for r in rows if r['debt_gdp_pct'] is not None])}",
            "date_alignment=outer_join",
            "ratio=debt_bn/gdp_bn*100 same_quarter",
            "gdp_basis=SAAR_raw_not_divided_by_4",
            "domestic_public=public_minus_foreign",
            f"identity=GFDEBTN==FYGFDPUN+FDHBATN tol={IDENTITY_TOL:g}",
            "stack_null_after=meta.stack_last",
            "missing_values=null",
        ])


def derive(root: Path = ROOT) -> tuple[dict[str, dict | None], list[str]]:
    rate_payloads, rate_maps, rate_refs, rate_failures = _load_group(root, RATES_INPUTS)
    cpi_payloads, cpi_maps, cpi_refs, cpi_failures = _load_group(root, CPI_INPUTS)
    debt_payloads, debt_maps, debt_refs, debt_failures = _load_group(root, DEBT_INPUTS)
    rates = None
    if not rate_failures:
        ust = _outer_join({k: rate_maps[k] for k in ("dgs2", "dgs10", "dgs30")})
        fed = _outer_join({k: rate_maps[k] for k in ("target_upper", "target_lower", "dff")})
        rates = envelope("derived_macro_rates", "daily", {"ust": ust, "fed": fed}, dates=sorted({r["date"] for r in ust} | {r["date"] for r in fed}), derived_from=rate_refs, warnings=_warnings(rate_payloads), info=[f"ust_coverage={_coverage_text(ust)}", f"fed_coverage={_coverage_text(fed)}", "date_alignment=outer_join", "missing_values=null"])
    cpi = None
    if not cpi_failures:
        cpi_rows = []
        for row in _outer_join({k: cpi_maps[k] for k in ("cpiaucsl", "cpilfesl")}):
            d = row["date"]
            cpi_rows.append({"date": d, "ref_period": _month_key(d), "cpiaucsl": row["cpiaucsl"], "cpiaucsl_yoy": _yoy(cpi_maps["cpiaucsl"], d), "cpilfesl": row["cpilfesl"], "cpilfesl_yoy": _yoy(cpi_maps["cpilfesl"], d), "published_at": None})
        latest = cpi_rows[-1] if cpi_rows else None
        cpi = envelope("derived_macro_cpi", "monthly", {"cpi": cpi_rows, "meta": {"cpi_latest_ref_period": latest["ref_period"] if latest else None, "cpi_latest_published_at": None, "cpi_release_date_available": False}}, dates=[r["date"] for r in cpi_rows], derived_from=cpi_refs, warnings=_warnings(cpi_payloads), info=[f"cpiaucsl_coverage={_coverage_text([r for r in cpi_rows if r['cpiaucsl'] is not None])}", f"cpilfesl_coverage={_coverage_text([r for r in cpi_rows if r['cpilfesl'] is not None])}", "date_alignment=outer_join", "missing_values=null", "yoy=exact_prior_year_month", "published_at=not_collected"])
    # 债务链独立于 rates/cpi：各自一次 _load_group、各自一个 if not ...failures 闸、
    # write_outputs() 跳过 None —— 一条链失败不连累其余两条。
    debt = None
    if not debt_failures:
        try:
            debt = _build_debt(debt_maps, debt_refs, debt_payloads)
        except DeriveFailure as exc:
            debt_failures.append(str(exc))
    return ({"macro_rates.json": rates, "macro_cpi.json": cpi, "macro_debt.json": debt},
            rate_failures + cpi_failures + debt_failures)


def _stable(payload: dict) -> dict:
    clone = json.loads(json.dumps(payload, ensure_ascii=False))
    clone.pop("generated_at", None)
    return clone


def write_outputs(outputs: dict[str, dict | None], root: Path = ROOT) -> None:
    out_dir = root / "data" / "derived"
    for name, payload in outputs.items():
        if payload is None:
            continue
        path = out_dir / name
        old = read_json_or(str(path), None)
        if isinstance(old, dict) and _stable(old) == _stable(payload):
            print(f"unchanged, skip write {path}")
            continue
        atomic_write_json(str(path), payload, compact=False)
        print(f"wrote {path}")


def run_tests() -> None:
    checks = 0
    failures = 0

    def check(name, ok):
        nonlocal checks, failures
        checks += 1
        print(f"{'PASS' if ok else 'FAIL'} {name}")
        failures += not ok

    def seed_input(root: Path, label: str, rows: list[dict], *, freq: str, warnings: list[str] | None = None) -> None:
        relpath = next(rel for lab, rel, _ in ALL_INPUTS if lab == label)
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(envelope("FRED", freq, rows, dates=[r["date"] for r in rows], warnings=warnings or []), ensure_ascii=False), encoding="utf-8")

    def seed_outputs(root: Path, rates_payload: dict, cpi_payload: dict, debt_payload: dict) -> None:
        (root / "data" / "derived").mkdir(parents=True, exist_ok=True)
        (root / "data" / "derived" / "macro_rates.json").write_text(json.dumps(rates_payload, ensure_ascii=False), encoding="utf-8")
        (root / "data" / "derived" / "macro_cpi.json").write_text(json.dumps(cpi_payload, ensure_ascii=False), encoding="utf-8")
        (root / "data" / "derived" / "macro_debt.json").write_text(json.dumps(debt_payload, ensure_ascii=False), encoding="utf-8")

    def dummy_out(source: str, freq: str, data: dict) -> dict:
        return envelope(source, freq, data, dates=[])

    def seed_rates_cpi(root: Path) -> None:
        seed_input(root, "ust_dgs2", [{"date": "2026-01-02", "value": 2.0}, {"date": "2026-01-03", "value": 2.1}], freq="daily", warnings=["fixture warning"])
        seed_input(root, "ust_dgs10", [{"date": "2026-01-02", "value": 4.0}], freq="daily")
        seed_input(root, "ust_dgs30", [{"date": "2026-01-03", "value": 5.0}], freq="daily")
        seed_input(root, "fed_target_upper", [{"date": "2026-01-02", "value": 4.5}, {"date": "2026-01-03", "value": 4.5}], freq="daily")
        seed_input(root, "fed_target_lower", [{"date": "2026-01-02", "value": 4.25}, {"date": "2026-01-03", "value": 4.25}], freq="daily")
        seed_input(root, "fed_effective_rate", [{"date": "2026-01-02", "value": 4.33}], freq="daily")
        seed_input(root, "cpi_cpiaucsl", [{"date": "2025-01-01", "value": 100}, {"date": "2026-01-01", "value": 105}, {"date": "2026-03-01", "value": 110}], freq="monthly")
        seed_input(root, "cpi_cpilfesl", [{"date": "2025-01-01", "value": 200}, {"date": "2026-01-01", "value": 210}], freq="monthly")

    def seed_debt(root: Path, **overrides) -> None:
        debt_fixture = {
            "debt_total": [
                {"date": "2025-07-01", "value": 37637552.59555763},
                {"date": "2025-10-01", "value": 38514009.0},
                {"date": "2026-01-01", "value": 39065421.0},
            ],
            "debt_held_public": [
                {"date": "2025-07-01", "value": 30298281.33869089},
                {"date": "2025-10-01", "value": 30870713.09449654},
                {"date": "2026-01-01", "value": 31454810.955549758},
            ],
            "debt_intragov": [
                {"date": "2025-07-01", "value": 7339271.25686674},
                {"date": "2025-10-01", "value": 7643295.90550346},
                {"date": "2026-01-01", "value": 7610610.04445024},
            ],
            "debt_foreign": [
                {"date": "2025-07-01", "value": 9234.4},
                {"date": "2025-10-01", "value": 9270.9},
            ],
            "gdp_nominal": [
                {"date": "2025-07-01", "value": 31098.027},
                {"date": "2025-10-01", "value": 31422.526},
                {"date": "2026-01-01", "value": 31865.721},
                {"date": "2026-04-01", "value": 32475.21},
            ],
        }
        for label, rows in debt_fixture.items():
            seed_input(root, label, overrides.get(label) or [dict(r) for r in rows], freq="quarterly")

    def debt_rows(payload: dict | None) -> dict:
        return {r["date"]: r for r in payload["data"]["debt"]} if payload else {}

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seed_rates_cpi(root)
        seed_debt(root)

        outputs, failures_list = derive(root)
        rates = outputs["macro_rates.json"]
        cpi = outputs["macro_cpi.json"]
        debt = outputs["macro_debt.json"]
        rows = debt_rows(debt)

        check("13 个上游信封均读取", not failures_list and all(outputs.values()))
        check("rates freq=daily", rates["freq"] == "daily")
        check("cpi freq=monthly", cpi["freq"] == "monthly")
        check("debt freq=quarterly", debt["freq"] == "quarterly")
        check("三条派生频率两两不同", len({rates["freq"], cpi["freq"], debt["freq"]}) == 3)
        check("UST 外连接保留缺失字段 null", rates["data"]["ust"] == [{"date": "2026-01-02", "dgs2": 2.0, "dgs10": 4.0, "dgs30": None}, {"date": "2026-01-03", "dgs2": 2.1, "dgs10": None, "dgs30": 5.0}])
        check("Fed 外连接保留 DFF 缺失 null", rates["data"]["fed"][1]["dff"] is None)
        cpi_rows = cpi["data"]["cpi"]
        check("YoY 精确匹配去年同期", abs(cpi_rows[1]["cpiaucsl_yoy"] - 0.05) < 1e-12 and abs(cpi_rows[1]["cpilfesl_yoy"] - 0.05) < 1e-12)
        check("去年同期缺失时 YoY=null", cpi_rows[-1]["cpiaucsl_yoy"] is None)
        check("不插值、不最近值替代", cpi_rows[-1]["cpiaucsl_yoy"] is None)
        check("上游 warnings 按派生链拆分", rates["warnings"] == ["ust_dgs2: fixture warning"] and cpi["warnings"] == [] and debt["warnings"] == [])
        check("CPI 发布日不编造", cpi["data"]["meta"]["cpi_latest_published_at"] is None and not cpi["data"]["meta"]["cpi_release_date_available"])
        check("CPI 最新参考期", cpi["data"]["meta"]["cpi_latest_ref_period"] == "2026-03")
        check("rates 仅 6 个上游 derived_from", len(rates["derived_from"]) == 6)
        check("cpi 仅 2 个上游 derived_from", len(cpi["derived_from"]) == 2)
        check("debt 仅 5 个上游 derived_from", len(debt["derived_from"]) == 5)
        check("debt units=billions_usd", debt["data"]["meta"]["units"] == "billions_usd")
        check("ratio 三锚点同季命中", abs(rows["2025-07-01"]["debt_gdp_pct"] - 121.02874756510319) < 1e-12 and abs(rows["2025-10-01"]["debt_gdp_pct"] - 122.56815063181108) < 1e-12 and abs(rows["2026-01-01"]["debt_gdp_pct"] - 122.59387132649533) < 1e-12)
        check("同季 GDP 与债务对齐", rows["2025-07-01"]["gdp_bn"] == 31098.027 and rows["2026-01-01"]["gdp_bn"] == 31865.721)
        check("stack 右端短一季只清三分量", rows["2026-01-01"]["total_bn"] is not None and rows["2026-01-01"]["debt_gdp_pct"] is not None and rows["2026-01-01"]["intragov_bn"] is None and rows["2026-01-01"]["domestic_public_bn"] is None and rows["2026-01-01"]["foreign_bn"] is None)
        check("stack 最后完整季与 meta 对账", debt["data"]["meta"]["stack_last"] == "2025-10-01" and debt["data"]["meta"]["total_last"] == "2026-01-01" and debt["data"]["meta"]["ratio_last"] == "2026-01-01" and debt["data"]["meta"]["foreign_lag_quarters"] == 1)
        check("meta 字段完整", set(debt["data"]["meta"]) == {"units", "stack_last", "total_last", "ratio_last", "foreign_last", "gdp_last", "foreign_lag_quarters", "identity_max_abs_rel", "identity_bad_quarters", "negative_domestic_quarters"})
        check("debt 行数为 4 条季度外连接", len(debt["data"]["debt"]) == 4)
        check("季度标签正确", debt["data"]["debt"][0]["quarter"] == "2025-Q3" and debt["data"]["debt"][3]["quarter"] == "2026-Q2")
        check("首季本国公众计算正确", abs(rows["2025-07-01"]["domestic_public_bn"] - 21063.881338690895) < 1e-12)
        check("2025-10 公众/GDP 比值正确", abs(rows["2025-10-01"]["public_gdp_pct"] - 98.24389386931063) < 1e-12)
        check("public_bn 直接来自 FYGFDPUN /1000", rows["2025-10-01"]["public_bn"] == 30870.71309449654)
        check("foreign/GDP 独立截至日期进入 meta", debt["data"]["meta"]["foreign_last"] == "2025-10-01" and debt["data"]["meta"]["gdp_last"] == "2026-04-01")
        check("GDP 右端多一季只留 GDP", rows["2026-04-01"]["total_bn"] is None and rows["2026-04-01"]["debt_gdp_pct"] is None and rows["2026-04-01"]["gdp_bn"] == 32475.21)
        check("clean meta 里无坏季度", debt["data"]["meta"]["identity_bad_quarters"] == [] and debt["data"]["meta"]["negative_domestic_quarters"] == [])
        check("clean identity max below tol", debt["data"]["meta"]["identity_max_abs_rel"] < IDENTITY_TOL)
        check("info 含单位归一说明", any("units_normalized=millions_divided_by_1000" in item for item in debt["info"]) and any("stack_null_after=meta.stack_last" in item for item in debt["info"]))
        check("三条百万美元债务字段均除以 1000",
              abs(rows["2025-07-01"]["total_bn"] - 37637.55259555763) < 1e-12
              and abs(rows["2025-07-01"]["intragov_bn"] - 7339.27125686674) < 1e-12
              and abs((rows["2025-07-01"]["domestic_public_bn"]
                       + rows["2025-07-01"]["foreign_bn"]) - 30298.28133869089) < 1e-9)
        check("外国持有十亿美元原值不缩放",
              rows["2025-07-01"]["foreign_bn"] == 9234.4
              and rows["2025-10-01"]["foreign_bn"] == 9270.9)

        good_rates = dummy_out("derived_macro_rates", "daily", {"ust": [{"date": "2026-01-02", "dgs2": 2.0}], "fed": [{"date": "2026-01-02", "target_upper": 4.5, "target_lower": 4.25, "dff": 4.33}]})
        good_cpi = dummy_out("derived_macro_cpi", "monthly", {"cpi": [{"date": "2026-01-01", "ref_period": "2026-01", "cpiaucsl": 1, "cpiaucsl_yoy": 0.1, "cpilfesl": 1, "cpilfesl_yoy": 0.2, "published_at": None}], "meta": {"cpi_latest_ref_period": "2026-01", "cpi_latest_published_at": None, "cpi_release_date_available": False}})
        good_debt = dummy_out("derived_macro_debt", "quarterly", {"debt": [{"date": "old"}], "meta": {"units": "billions_usd", "stack_last": None, "total_last": None, "ratio_last": None, "foreign_lag_quarters": None, "identity_max_abs_rel": 0.0, "identity_bad_quarters": [], "negative_domestic_quarters": []}})
        seed_outputs(root, good_rates, good_cpi, good_debt)
        write_outputs(outputs, root)
        rates_path = root / "data" / "derived" / "macro_rates.json"
        cpi_path = root / "data" / "derived" / "macro_cpi.json"
        debt_path = root / "data" / "derived" / "macro_debt.json"
        first_rates = rates_path.read_text(encoding="utf-8")
        first_cpi = cpi_path.read_text(encoding="utf-8")
        first_debt = debt_path.read_text(encoding="utf-8")
        write_outputs(outputs, root)
        check("幂等第二次跳过写盘", rates_path.read_text(encoding="utf-8") == first_rates and cpi_path.read_text(encoding="utf-8") == first_cpi and debt_path.read_text(encoding="utf-8") == first_debt)

    # 长历史 fixture：锁死派生链不依赖 2016 起点，也不按固定长度裁剪。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seed_rates_cpi(root)
        quarter_dates = [
            f"{year}-{month:02d}-01"
            for year in range(1990, 2027)
            for month in (1, 4, 7, 10)
            if f"{year}-{month:02d}-01" <= "2026-01-01"
        ]
        total_points = []
        public_points = []
        intragov_points = []
        foreign_points = []
        gdp_points = []
        for idx, quarter in enumerate(quarter_dates):
            total = 3_000_000 + idx * 100_000
            public = total * 3 // 4
            total_points.append({"date": quarter, "value": total})
            public_points.append({"date": quarter, "value": public})
            intragov_points.append({"date": quarter, "value": total - public})
            foreign_points.append({"date": quarter, "value": 400 + idx * 10})
            gdp_points.append({"date": quarter, "value": 6_000 + idx * 250})
        seed_debt(
            root,
            debt_total=total_points,
            debt_held_public=public_points,
            debt_intragov=intragov_points,
            debt_foreign=foreign_points,
            gdp_nominal=gdp_points,
        )
        outputs, failures_list = derive(root)
        debt = outputs["macro_debt.json"]
        rows = debt["data"]["debt"] if debt else []
        check(
            "1990-Q1 至 2026-Q1 的 145 季长历史完整派生",
            not failures_list and len(rows) == 145
            and rows[0]["date"] == "1990-01-01"
            and rows[-1]["date"] == "2026-01-01",
        )
        check(
            "长历史首季金额、结构与比率均保留真实值",
            bool(rows)
            and all(rows[0][field] is not None for field in (
                "total_bn", "gdp_bn", "debt_gdp_pct",
                "intragov_bn", "domestic_public_bn", "foreign_bn",
            )),
        )

    # strict envelope：只破坏 rates 的一个上游，另外两条链仍应独立产出。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seed_rates_cpi(root)
        seed_debt(root)
        (root / "data" / "ust_dgs2.json").write_text("[]", encoding="utf-8")
        outputs, strict_failures = derive(root)
        check("裸格式上游被 strict envelope 拒绝",
              outputs["macro_rates.json"] is None
              and any("ust_dgs2: 缺少或不是信封" in item for item in strict_failures))
        check("rates 裸格式失败不拖累 cpi/debt",
              outputs["macro_cpi.json"] is not None
              and outputs["macro_debt.json"] is not None)

    # rates 链失败：旧 rates 必须逐字节保留，cpi/debt 仍正常产出并覆盖各自哨兵。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seed_rates_cpi(root)
        seed_debt(root)
        seed_input(root, "ust_dgs2", [], freq="daily")
        old_rates = dummy_out("derived_macro_rates", "daily", {"ust": [{"date": "old-rates"}], "fed": []})
        old_cpi = dummy_out("derived_macro_cpi", "monthly", {"cpi": [{"date": "old-cpi"}], "meta": {}})
        old_debt = dummy_out("derived_macro_debt", "quarterly", {"debt": [{"date": "old-debt"}], "meta": {}})
        seed_outputs(root, old_rates, old_cpi, old_debt)
        rates_path = root / "data" / "derived" / "macro_rates.json"
        cpi_path = root / "data" / "derived" / "macro_cpi.json"
        debt_path = root / "data" / "derived" / "macro_debt.json"
        old_rates_text = rates_path.read_text(encoding="utf-8")

        outputs, rate_failures = derive(root)
        check("rates 空上游时该链失败",
              outputs["macro_rates.json"] is None
              and any("ust_dgs2: data empty" in item for item in rate_failures))
        check("rates 失败时 cpi/debt 仍产出",
              outputs["macro_cpi.json"] is not None
              and outputs["macro_debt.json"] is not None)
        write_outputs(outputs, root)
        check("rates 失败时旧 macro_rates.json 逐字节不覆盖",
              rates_path.read_text(encoding="utf-8") == old_rates_text)
        check("rates 失败时 cpi/debt 新输出正常落盘",
              json.loads(cpi_path.read_text(encoding="utf-8"))["data"]["cpi"][0]["date"] != "old-cpi"
              and json.loads(debt_path.read_text(encoding="utf-8"))["data"]["debt"][0]["date"] != "old-debt")

    # CPI 链失败：旧 CPI 必须逐字节保留，rates/debt 仍正常产出并覆盖各自哨兵。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seed_rates_cpi(root)
        seed_debt(root)
        null_cpi = [{"date": "2025-01-01", "value": None},
                    {"date": "2026-01-01", "value": None}]
        seed_input(root, "cpi_cpiaucsl", null_cpi, freq="monthly")
        seed_input(root, "cpi_cpilfesl", null_cpi, freq="monthly")
        old_rates = dummy_out("derived_macro_rates", "daily", {"ust": [{"date": "old-rates"}], "fed": []})
        old_cpi = dummy_out("derived_macro_cpi", "monthly", {"cpi": [{"date": "old-cpi"}], "meta": {}})
        old_debt = dummy_out("derived_macro_debt", "quarterly", {"debt": [{"date": "old-debt"}], "meta": {}})
        seed_outputs(root, old_rates, old_cpi, old_debt)
        rates_path = root / "data" / "derived" / "macro_rates.json"
        cpi_path = root / "data" / "derived" / "macro_cpi.json"
        debt_path = root / "data" / "derived" / "macro_debt.json"
        old_cpi_text = cpi_path.read_text(encoding="utf-8")

        outputs, cpi_failures = derive(root)
        check("CPI 全 null 时该链失败",
              outputs["macro_cpi.json"] is None
              and any("cpi_cpiaucsl: data all-null" in item for item in cpi_failures)
              and any("cpi_cpilfesl: data all-null" in item for item in cpi_failures))
        check("CPI 失败时 rates/debt 仍产出",
              outputs["macro_rates.json"] is not None
              and outputs["macro_debt.json"] is not None)
        write_outputs(outputs, root)
        check("CPI 失败时旧 macro_cpi.json 逐字节不覆盖",
              cpi_path.read_text(encoding="utf-8") == old_cpi_text)
        check("CPI 失败时 rates/debt 新输出正常落盘",
              json.loads(rates_path.read_text(encoding="utf-8"))["data"]["ust"][0]["date"] != "old-rates"
              and json.loads(debt_path.read_text(encoding="utf-8"))["data"]["debt"][0]["date"] != "old-debt")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seed_rates_cpi(root)
        seed_debt(root, debt_total=[{"date": "2025-07-01", "value": 37637552595.55763}, {"date": "2025-10-01", "value": 38514009000.0}, {"date": "2026-01-01", "value": 39065421000.0}])
        old_rates = dummy_out("derived_macro_rates", "daily", {"ust": [{"date": "old-rates"}], "fed": []})
        old_cpi = dummy_out("derived_macro_cpi", "monthly", {"cpi": [{"date": "old-cpi"}], "meta": {}})
        old_debt = dummy_out("derived_macro_debt", "quarterly", {"debt": [{"date": "old-debt"}], "meta": {}})
        seed_outputs(root, old_rates, old_cpi, old_debt)
        debt_path = root / "data" / "derived" / "macro_debt.json"
        old_debt_text = debt_path.read_text(encoding="utf-8")
        outputs, bad_failures = derive(root)
        check("全季度恒等式破裂时 debt 失败且不出输出", outputs["macro_debt.json"] is None and any("恒等式" in item for item in bad_failures))
        check("debt 全失败不拖累 rates/cpi",
              outputs["macro_rates.json"] is not None
              and outputs["macro_cpi.json"] is not None)
        write_outputs(outputs, root)
        check("debt 全失败时旧 macro_debt.json 逐字节不覆盖",
              debt_path.read_text(encoding="utf-8") == old_debt_text)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seed_rates_cpi(root)
        seed_debt(root, debt_total=[{"date": "2025-07-01", "value": 37638552.59555763}, {"date": "2025-10-01", "value": 38514009.0}, {"date": "2026-01-01", "value": 39065421.0}])
        outputs, failures_list = derive(root)
        debt = outputs["macro_debt.json"]
        rows = debt_rows(debt)
        check("个别季度恒等式破裂只置该季三分量 null",
              debt is not None
              and debt["data"]["meta"]["identity_bad_quarters"] == ["2025-07-01"]
              and debt["data"]["meta"]["stack_last"] == "2025-10-01"
              and debt["data"]["meta"]["foreign_lag_quarters"] == 1
              and all(rows["2025-07-01"][field] is None for field in STACK_FIELDS)
              and all(rows["2025-10-01"][field] is not None for field in STACK_FIELDS)
              and rows["2025-07-01"]["total_bn"] is not None
              and rows["2025-07-01"]["debt_gdp_pct"] is not None
              and rows["2026-01-01"]["total_bn"] is not None
              and rows["2026-01-01"]["debt_gdp_pct"] is not None
              and all(rows["2026-01-01"][field] is None for field in STACK_FIELDS))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seed_rates_cpi(root)
        seed_debt(root, debt_held_public=[{"date": "2025-07-01", "value": 30298281.33869089}, {"date": "2025-10-01", "value": 30870713.09449654}, {"date": "2026-01-01", "value": 31454810.955549758}], debt_foreign=[{"date": "2025-07-01", "value": 40000.0}, {"date": "2025-10-01", "value": 9270.9}])
        outputs, failures_list = derive(root)
        debt = outputs["macro_debt.json"]
        rows = debt_rows(debt)
        check("本国公众持有为负只影响该季", debt["data"]["meta"]["negative_domestic_quarters"] == ["2025-07-01"] and rows["2025-07-01"]["domestic_public_bn"] is None and rows["2025-10-01"]["domestic_public_bn"] is not None)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seed_rates_cpi(root)
        outputs, failures_list = derive(root)
        check("债务链缺输入不拖累 rates/cpi", outputs["macro_rates.json"] is not None and outputs["macro_cpi.json"] is not None and outputs["macro_debt.json"] is None and any("debt_total" in item or "debt_held_public" in item for item in failures_list))

    print(f"{checks - failures} passed, {failures} failed")
    if failures:
        raise SystemExit(1)

def main(root: Path = ROOT) -> int:
    if "--test" in sys.argv:
        run_tests()
        return 0
    outputs, failures = derive(root)
    write_outputs(outputs, root)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
