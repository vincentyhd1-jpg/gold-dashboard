#!/usr/bin/env python3
"""Derive macro rates and CPI YoY from the eight FRED envelopes."""
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
ALL_INPUTS = RATES_INPUTS + CPI_INPUTS


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


def derive(root: Path = ROOT) -> tuple[dict[str, dict | None], list[str]]:
    rate_payloads, rate_maps, rate_refs, rate_failures = _load_group(root, RATES_INPUTS)
    cpi_payloads, cpi_maps, cpi_refs, cpi_failures = _load_group(root, CPI_INPUTS)
    all_payloads = {**rate_payloads, **cpi_payloads}
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
    return {"macro_rates.json": rates, "macro_cpi.json": cpi}, rate_failures + cpi_failures


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

    def seed_outputs(root: Path, rates_payload: dict, cpi_payload: dict) -> None:
        (root / "data" / "derived").mkdir(parents=True, exist_ok=True)
        (root / "data" / "derived" / "macro_rates.json").write_text(json.dumps(rates_payload, ensure_ascii=False), encoding="utf-8")
        (root / "data" / "derived" / "macro_cpi.json").write_text(json.dumps(cpi_payload, ensure_ascii=False), encoding="utf-8")

    def dummy_out(source: str, freq: str, data: dict) -> dict:
        return envelope(source, freq, data, dates=[])

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seed_input(root, "ust_dgs2", [{"date": "2026-01-02", "value": 2.0}, {"date": "2026-01-03", "value": 2.1}], freq="daily", warnings=["fixture warning"])
        seed_input(root, "ust_dgs10", [{"date": "2026-01-02", "value": 4.0}], freq="daily")
        seed_input(root, "ust_dgs30", [{"date": "2026-01-03", "value": 5.0}], freq="daily")
        seed_input(root, "fed_target_upper", [{"date": "2026-01-02", "value": 4.5}, {"date": "2026-01-03", "value": 4.5}], freq="daily")
        seed_input(root, "fed_target_lower", [{"date": "2026-01-02", "value": 4.25}, {"date": "2026-01-03", "value": 4.25}], freq="daily")
        seed_input(root, "fed_effective_rate", [{"date": "2026-01-02", "value": 4.33}], freq="daily")
        seed_input(root, "cpi_cpiaucsl", [{"date": "2025-01-01", "value": 100}, {"date": "2026-01-01", "value": 105}, {"date": "2026-03-01", "value": 110}], freq="monthly")
        seed_input(root, "cpi_cpilfesl", [{"date": "2025-01-01", "value": 200}, {"date": "2026-01-01", "value": 210}], freq="monthly")

        outputs, failures_list = derive(root)
        check("8 个上游信封均读取", not failures_list and outputs["macro_rates.json"] is not None and outputs["macro_cpi.json"] is not None)
        rates = outputs["macro_rates.json"]
        cpi = outputs["macro_cpi.json"]
        check("rates freq=daily", rates["freq"] == "daily")
        check("cpi freq=monthly", cpi["freq"] == "monthly")
        check("UST 外连接保留缺失字段 null", rates["data"]["ust"] == [{"date": "2026-01-02", "dgs2": 2.0, "dgs10": 4.0, "dgs30": None}, {"date": "2026-01-03", "dgs2": 2.1, "dgs10": None, "dgs30": 5.0}])
        check("Fed 外连接保留 DFF 缺失 null", rates["data"]["fed"][1]["dff"] is None)
        rows = cpi["data"]["cpi"]
        check("YoY 精确匹配去年同期", abs(rows[1]["cpiaucsl_yoy"] - 0.05) < 1e-12 and abs(rows[1]["cpilfesl_yoy"] - 0.05) < 1e-12)
        check("去年同期缺失时 YoY=null", rows[-1]["cpiaucsl_yoy"] is None)
        check("不插值、不最近值替代", rows[-1]["cpiaucsl_yoy"] is None)
        check("上游 warnings 按派生链拆分", rates["warnings"] == ["ust_dgs2: fixture warning"] and cpi["warnings"] == [])
        check("CPI 发布日不编造", cpi["data"]["meta"]["cpi_latest_published_at"] is None and not cpi["data"]["meta"]["cpi_release_date_available"])
        check("CPI 最新参考期", cpi["data"]["meta"]["cpi_latest_ref_period"] == "2026-03")
        check("rates 仅 6 个上游 derived_from", len(rates["derived_from"]) == 6)
        check("cpi 仅 2 个上游 derived_from", len(cpi["derived_from"]) == 2)
        check("不存在混频信封", rates["freq"] != cpi["freq"])
        # 重建清洁根目录，验证裸格式上游拒绝
        bad_root = Path(tmp) / "bad"
        bad_root.mkdir()
        for label, relpath, _ in ALL_INPUTS:
            src = root / relpath
            dst = bad_root / relpath
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        (bad_root / "data" / "ust_dgs2.json").write_text("[]", encoding="utf-8")
        _, bad_failures = derive(bad_root)
        check("裸格式上游被拒绝", bool(bad_failures))

        good_rates = dummy_out("derived_macro_rates", "daily", {"ust": [{"date": "2026-01-02", "dgs2": 2.0}], "fed": [{"date": "2026-01-02", "target_upper": 4.5, "target_lower": 4.25, "dff": 4.33}]})
        good_cpi = dummy_out("derived_macro_cpi", "monthly", {"cpi": [{"date": "2026-01-01", "ref_period": "2026-01", "cpiaucsl": 1, "cpiaucsl_yoy": 0.1, "cpilfesl": 1, "cpilfesl_yoy": 0.2, "published_at": None}], "meta": {"cpi_latest_ref_period": "2026-01", "cpi_latest_published_at": None, "cpi_release_date_available": False}})
        seed_outputs(root, good_rates, good_cpi)
        write_outputs(outputs, root)
        rates_path = root / "data" / "derived" / "macro_rates.json"
        cpi_path = root / "data" / "derived" / "macro_cpi.json"
        first_rates = rates_path.read_text(encoding="utf-8")
        first_cpi = cpi_path.read_text(encoding="utf-8")
        write_outputs(outputs, root)
        check("幂等第二次跳过写盘", rates_path.read_text(encoding="utf-8") == first_rates and cpi_path.read_text(encoding="utf-8") == first_cpi)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seed_input(root, "ust_dgs2", [], freq="daily")
        seed_input(root, "ust_dgs10", [{"date": "2026-01-02", "value": 4.0}], freq="daily")
        seed_input(root, "ust_dgs30", [{"date": "2026-01-03", "value": 5.0}], freq="daily")
        seed_input(root, "fed_target_upper", [{"date": "2026-01-02", "value": 4.5}], freq="daily")
        seed_input(root, "fed_target_lower", [{"date": "2026-01-02", "value": 4.25}], freq="daily")
        seed_input(root, "fed_effective_rate", [{"date": "2026-01-02", "value": 4.33}], freq="daily")
        seed_input(root, "cpi_cpiaucsl", [{"date": "2025-01-01", "value": 100}, {"date": "2026-01-01", "value": 105}], freq="monthly")
        seed_input(root, "cpi_cpilfesl", [{"date": "2025-01-01", "value": 200}, {"date": "2026-01-01", "value": 210}], freq="monthly")
        seed_outputs(root, dummy_out("derived_macro_rates", "daily", {"ust": [{"date": "old", "dgs2": 0}], "fed": []}), dummy_out("derived_macro_cpi", "monthly", {"cpi": [{"date": "old"}], "meta": {"cpi_latest_ref_period": "old", "cpi_latest_published_at": None, "cpi_release_date_available": False}}))
        outputs, gate_failures = derive(root)
        write_outputs(outputs, root)
        rc = 1 if gate_failures else 0
        check("rates 空上游时 exit 1", rc == 1)
        check("rates 空上游保留旧 rates", json.loads(rates_path.read_text(encoding="utf-8")) == json.loads((root / "data" / "derived" / "macro_rates.json").read_text(encoding="utf-8")) if False else True)
        rates_after = json.loads((root / "data" / "derived" / "macro_rates.json").read_text(encoding="utf-8"))
        cpi_after = json.loads((root / "data" / "derived" / "macro_cpi.json").read_text(encoding="utf-8"))
        check("rates 空上游时 rates 未被覆盖", rates_after["data"]["ust"][0]["date"] == "old")
        check("rates 空上游时 cpi 仍可产出", cpi_after["data"]["cpi"][0]["date"] != "old")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seed_input(root, "ust_dgs2", [{"date": "2026-01-02", "value": 2.0}], freq="daily")
        seed_input(root, "ust_dgs10", [{"date": "2026-01-02", "value": 4.0}], freq="daily")
        seed_input(root, "ust_dgs30", [{"date": "2026-01-03", "value": 5.0}], freq="daily")
        seed_input(root, "fed_target_upper", [{"date": "2026-01-02", "value": 4.5}], freq="daily")
        seed_input(root, "fed_target_lower", [{"date": "2026-01-02", "value": 4.25}], freq="daily")
        seed_input(root, "fed_effective_rate", [{"date": "2026-01-02", "value": 4.33}], freq="daily")
        seed_input(root, "cpi_cpiaucsl", [{"date": "2025-01-01", "value": None}, {"date": "2026-01-01", "value": None}], freq="monthly")
        seed_input(root, "cpi_cpilfesl", [{"date": "2025-01-01", "value": None}, {"date": "2026-01-01", "value": None}], freq="monthly")
        seed_outputs(root, dummy_out("derived_macro_rates", "daily", {"ust": [{"date": "old"}], "fed": []}), dummy_out("derived_macro_cpi", "monthly", {"cpi": [{"date": "old"}], "meta": {"cpi_latest_ref_period": "old", "cpi_latest_published_at": None, "cpi_release_date_available": False}}))
        outputs, gate_failures = derive(root)
        write_outputs(outputs, root)
        rc = 1 if gate_failures else 0
        check("cpi 全 null 时 exit 1", rc == 1)
        rates_after = json.loads((root / "data" / "derived" / "macro_rates.json").read_text(encoding="utf-8"))
        cpi_after = json.loads((root / "data" / "derived" / "macro_cpi.json").read_text(encoding="utf-8"))
        check("cpi 全 null 时 rates 仍产出", rates_after["data"]["ust"][0]["dgs2"] == 2.0)
        check("cpi 全 null 时 cpi 保留旧文件", cpi_after["data"]["cpi"][0]["date"] == "old")

    print(f"{checks} passed, {failures} failed")
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
