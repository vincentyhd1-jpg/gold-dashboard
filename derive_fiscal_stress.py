#!/usr/bin/env python3
"""Derive quarterly U.S. fiscal-sustainability indicators from strict envelopes."""
from __future__ import annotations

import calendar
import hashlib
import json
import math
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

from data_envelope import assert_envelope, envelope, upstream_ref
from io_utils import atomic_write_json, read_json_or, sweep_stale_tmp

ROOT = Path(__file__).resolve().parent
MTS_PATH = ROOT / "data" / "treasury_mts_fiscal.json"
DAILY_DEBT_PATH = ROOT / "data" / "treasury_debt_daily.json"
MACRO_DEBT_PATH = ROOT / "data" / "derived" / "macro_debt.json"
OUT_PATH = ROOT / "data" / "derived" / "macro_fiscal_stress.json"
SOURCE = "derived_fiscal_sustainability"
FIRST_MODEL_QUARTER = "2016-Q1"


class DeriveFailure(RuntimeError):
    """Input or derived contract failure; old output must remain untouched."""


def _load(path: Path, *, source: str, freq: str):
    payload = read_json_or(str(path), None)
    if payload is None:
        raise DeriveFailure(f"{path.name}: 缺少或无法读取")
    try:
        assert_envelope(payload)
    except ValueError as exc:
        raise DeriveFailure(f"{path.name}: 缺少或不是合法信封: {exc}") from exc
    if payload.get("source") != source or payload.get("freq") != freq:
        raise DeriveFailure(f"{path.name}: source/freq 不匹配")
    if not isinstance(payload.get("data"), (list, dict)):
        raise DeriveFailure(f"{path.name}: data 类型非法")
    return payload


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _month_end(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    serial = year * 12 + month - 1 + offset
    return serial // 12, serial % 12 + 1


def _quarter_end(date_string: str) -> str:
    year, month = map(int, date_string[:7].split("-"))
    end_month = ((month - 1) // 3 + 1) * 3
    return _month_end(year, end_month)


def _ttm_month_ends(quarter_end: str) -> list[str]:
    year, month = map(int, quarter_end[:7].split("-"))
    return [_month_end(*_shift_month(year, month, offset)) for offset in range(-11, 1)]


def _validate_mts(rows) -> dict[str, dict]:
    if not isinstance(rows, list) or not rows:
        raise DeriveFailure("treasury_mts_fiscal.json: data 为空")
    result: dict[str, dict] = {}
    required = ("receipts_bn", "total_outlays_bn", "net_interest_bn")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise DeriveFailure("treasury_mts_fiscal.json: 月记录格式非法")
        if row["date"] in result:
            raise DeriveFailure(f"treasury_mts_fiscal.json: 重复月份 {row['date']}")
        for field in required:
            if not _finite(row.get(field)) or (
                    field != "net_interest_bn" and row[field] < 0):
                raise DeriveFailure(f"treasury_mts_fiscal.json: {row['date']} {field} 非法")
        result[row["date"]] = row
    return result


def _validate_daily(rows) -> list[dict]:
    if not isinstance(rows, list) or not rows:
        raise DeriveFailure("treasury_debt_daily.json: data 为空")
    previous = None
    out = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise DeriveFailure("treasury_debt_daily.json: 日记录格式非法")
        if previous is not None and row["date"] <= previous:
            raise DeriveFailure("treasury_debt_daily.json: 日期未严格升序")
        public = row.get("public_bn")
        if public is not None and (not _finite(public) or public <= 0):
            raise DeriveFailure(f"treasury_debt_daily.json: {row['date']} public_bn 非法")
        out.append(row)
        previous = row["date"]
    return out


def _validate_macro(data) -> list[dict]:
    rows = data.get("debt") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise DeriveFailure("macro_debt.json: data.debt 为空")
    previous = None
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise DeriveFailure("macro_debt.json: 季度记录格式非法")
        if previous is not None and row["date"] <= previous:
            raise DeriveFailure("macro_debt.json: 日期未严格升序")
        previous = row["date"]
    return rows


def _ttm_fiscal(mts_by_date: dict[str, dict], quarter_end: str):
    months = _ttm_month_ends(quarter_end)
    rows = [mts_by_date.get(month) for month in months]
    if any(row is None for row in rows):
        return None, months
    fields = ("receipts_bn", "total_outlays_bn", "net_interest_bn")
    if any(not _finite(row.get(field)) for row in rows for field in fields):
        return None, months
    return {field: sum(row[field] for row in rows) for field in fields}, months


def _average_public_debt(daily_rows: list[dict], months: list[str]):
    month_keys = {month[:7] for month in months}
    valid = [row for row in daily_rows if row["date"][:7] in month_keys
             and _finite(row.get("public_bn"))]
    # Weekends and holidays are not observations and are never filled.  A
    # credible exact-window mean still requires at least one real observation
    # in every one of the twelve calendar months.
    observed_months = {row["date"][:7] for row in valid}
    if observed_months != month_keys:
        return None, len(valid)
    return sum(row["public_bn"] for row in valid) / len(valid), len(valid)


def _source_meta(mts: dict, daily: dict, macro: dict, complete_through: str | None) -> dict:
    macro_data = macro["data"]
    macro_meta = macro_data.get("meta", {}) if isinstance(macro_data, dict) else {}
    return {
        "mts_as_of": mts["coverage"]["last"],
        "daily_public_debt_as_of": next(
            (row["date"] for row in reversed(daily["data"])
             if _finite(row.get("public_bn"))), None),
        "gdp_as_of": macro_meta.get("gdp_last"),
        "public_debt_gdp_as_of": macro_meta.get("ratio_last"),
        "complete_through": complete_through,
        "coverage": {
            "mts": mts["coverage"],
            "daily_debt": daily["coverage"],
            "macro_debt": macro["coverage"],
        },
    }


def derive_payload(mts: dict, daily: dict, macro: dict) -> dict:
    mts_by_date = _validate_mts(mts["data"])
    daily_rows = _validate_daily(daily["data"])
    macro_rows = _validate_macro(macro["data"])
    macro_by_date = {row["date"]: row for row in macro_rows}

    rows: list[dict] = []
    consecutive_positive = 0
    for index, macro_row in enumerate(macro_rows):
        quarter = macro_row.get("quarter")
        if not isinstance(quarter, str) or quarter < FIRST_MODEL_QUARTER:
            continue
        row_date = macro_row["date"]
        quarter_end = _quarter_end(row_date)
        fiscal, months = _ttm_fiscal(mts_by_date, quarter_end)
        average_public_debt, daily_count = _average_public_debt(daily_rows, months)
        gdp = macro_row.get("gdp_bn")
        public_d = macro_row.get("public_gdp_pct")
        prior_year_date = f"{int(row_date[:4]) - 1:04d}{row_date[4:]}"
        prior_quarter_date = macro_rows[index - 1]["date"] if index else None
        prior_year_gdp = macro_by_date.get(prior_year_date, {}).get("gdp_bn")
        prior_public_d = macro_by_date.get(prior_quarter_date, {}).get(
            "public_gdp_pct") if prior_quarter_date else None

        receipts = fiscal["receipts_bn"] if fiscal else None
        outlays = fiscal["total_outlays_bn"] if fiscal else None
        interest = fiscal["net_interest_bn"] if fiscal else None
        overall = receipts - outlays if fiscal else None
        # Positive means a primary surplus. Net interest is added back to the
        # overall balance because it is already included in total net outlays.
        primary = overall + interest if fiscal else None
        nominal_g = ((gdp / prior_year_gdp) - 1) * 100 if (
            _finite(gdp) and gdp > 0 and _finite(prior_year_gdp)
            and prior_year_gdp > 0) else None
        effective_r = interest / average_public_debt * 100 if (
            _finite(interest) and _finite(average_public_debt)
            and average_public_debt > 0) else None
        r_minus_g = effective_r - nominal_g if (
            _finite(effective_r) and _finite(nominal_g)) else None
        primary_pct = primary / gdp * 100 if (
            _finite(primary) and _finite(gdp) and gdp > 0) else None
        stabilizing = r_minus_g * public_d / 100 if (
            _finite(r_minus_g) and _finite(public_d)) else None
        fiscal_gap = stabilizing - primary_pct if (
            _finite(stabilizing) and _finite(primary_pct)) else None
        interest_gdp = interest / gdp * 100 if (
            _finite(interest) and _finite(gdp) and gdp > 0) else None
        interest_receipts = interest / receipts * 100 if (
            _finite(interest) and _finite(receipts) and receipts > 0) else None
        observed_delta = public_d - prior_public_d if (
            _finite(public_d) and _finite(prior_public_d)) else None
        # r, g and the primary balance ratio are annual/TTM rates. Divide the
        # annual debt-dynamics RHS by four before comparing it with observed
        # quarter-over-quarter percentage-point change.
        model_delta = ((r_minus_g * public_d / 100) - primary_pct) / 4 if (
            _finite(r_minus_g) and _finite(public_d) and _finite(primary_pct)) else None
        residual = observed_delta - model_delta if (
            _finite(observed_delta) and _finite(model_delta)) else None

        required = (public_d, effective_r, nominal_g, r_minus_g, primary_pct,
                    stabilizing, fiscal_gap, interest_gdp, interest_receipts,
                    observed_delta, model_delta, residual)
        complete = all(_finite(value) for value in required)
        if complete and fiscal_gap > 0:
            consecutive_positive += 1
            condition = "gap_positive"
        elif complete:
            consecutive_positive = 0
            condition = "stabilizing_condition_met"
        else:
            consecutive_positive = 0
            condition = "unknown"

        rows.append({
            "quarter": quarter,
            "date": row_date,
            "fiscal_window_start": months[0],
            "fiscal_window_end": months[-1],
            "receipts_ttm_bn": receipts,
            "total_outlays_ttm_bn": outlays,
            "net_interest_ttm_bn": interest,
            "overall_balance_ttm_bn": overall,
            "primary_balance_ttm_bn": primary,
            "average_public_debt_bn": average_public_debt,
            "average_public_debt_observations": daily_count,
            "public_debt_gdp_pct": public_d if _finite(public_d) else None,
            "effective_r_pct": effective_r,
            "nominal_g_pct": nominal_g,
            "r_minus_g_pct_points": r_minus_g,
            "primary_balance_gdp_pct": primary_pct,
            "stabilizing_primary_balance_pct_gdp": stabilizing,
            "fiscal_gap_pct_gdp": fiscal_gap,
            "net_interest_gdp_pct": interest_gdp,
            "net_interest_receipts_pct": interest_receipts,
            "observed_delta_public_debt_gdp": observed_delta,
            "model_implied_delta_public_debt_gdp": model_delta,
            "stock_flow_residual_pct_gdp": residual,
            "gap_positive_consecutive_complete_quarters": consecutive_positive,
            "calculation_status": "complete" if complete else "incomplete",
            "trajectory_condition": condition,
        })

    if not rows:
        raise DeriveFailure("没有可输出的 2016-Q1 以后季度")
    complete_rows = [row for row in rows if row["calculation_status"] == "complete"]
    if not complete_rows:
        raise DeriveFailure("没有任何完整财政可持续性季度")
    latest = complete_rows[-1]
    meta = {
        **_source_meta(mts, daily, macro, latest["quarter"]),
        "history_boundary": FIRST_MODEL_QUARTER,
        "stress_level": "unscored",
        "threshold_version": None,
        "gap_positive_consecutive_complete_quarters": latest[
            "gap_positive_consecutive_complete_quarters"],
        "effective_r_method": "ttm_net_interest_divided_by_mean_valid_daily_public_debt",
        "nominal_g_method": "quarterly_nominal_gdp_yoy_t_over_t_minus_4",
        "fiscal_flow_method": "twelve_consecutive_calendar_months_divided_by_quarterly_gdp_saar",
        "debt_dynamics_interval": "annual_rate_rhs_divided_by_4_for_qoq_comparison",
    }
    derived = []
    for path, source in ((MTS_PATH, "treasury_fiscal_data_mts_table_9"),
                         (DAILY_DEBT_PATH, "treasury_fiscal_data_debt_to_penny"),
                         (MACRO_DEBT_PATH, "derived_macro_debt")):
        ref = upstream_ref(str(path), source)
        if ref:
            derived.append(ref)
    return envelope(
        SOURCE, "quarterly", {"quarterly": rows, "latest": latest, "meta": meta},
        dates=[row["date"] for row in rows], derived_from=derived,
        info=[
            "sign=overall_and_primary_balance_positive_means_surplus",
            "fiscal_gap=stabilizing_primary_balance_minus_actual_primary_balance",
            "fiscal_gap_positive=additional_primary_adjustment_required",
            "no_forward_fill_no_interpolation_no_threshold_score_no_forecast",
        ],
    )


def derive(root: Path = ROOT) -> dict:
    mts_path = root / "data" / "treasury_mts_fiscal.json"
    daily_path = root / "data" / "treasury_debt_daily.json"
    macro_path = root / "data" / "derived" / "macro_debt.json"
    mts = _load(mts_path, source="treasury_fiscal_data_mts_table_9", freq="monthly")
    daily = _load(daily_path, source="treasury_fiscal_data_debt_to_penny", freq="daily")
    macro = _load(macro_path, source="derived_macro_debt", freq="quarterly")
    # Tests and alternate roots must record their own upstream identities.
    global MTS_PATH, DAILY_DEBT_PATH, MACRO_DEBT_PATH
    old_paths = MTS_PATH, DAILY_DEBT_PATH, MACRO_DEBT_PATH
    try:
        MTS_PATH, DAILY_DEBT_PATH, MACRO_DEBT_PATH = mts_path, daily_path, macro_path
        return derive_payload(mts, daily, macro)
    finally:
        MTS_PATH, DAILY_DEBT_PATH, MACRO_DEBT_PATH = old_paths


def run_once(*, root: Path = ROOT) -> int:
    output_path = root / "data" / "derived" / "macro_fiscal_stress.json"
    sweep_stale_tmp(str(output_path))
    try:
        output = derive(root)
    except DeriveFailure as exc:
        print(f"ERROR fiscal sustainability 派生失败：{exc}；旧 {output_path.name} 保持不变")
        return 1
    existing = read_json_or(str(output_path), None)
    if existing is not None:
        try:
            assert_envelope(existing)
        except ValueError as exc:
            print(f"ERROR 现有 {output_path.name} 不是合法信封：{exc}；拒绝覆盖")
            return 1
        comparable_keys = ("source", "freq", "date_field", "coverage", "derived_from",
                           "warnings", "info", "data")
        if all(existing.get(key) == output.get(key) for key in comparable_keys):
            print(f"Fiscal sustainability 无业务变化，跳过写盘："
                  f"complete_through={output['data']['meta']['complete_through']}")
            return 0
    atomic_write_json(str(output_path), output, compact=False)
    print(f"Fiscal sustainability 已写入 {output_path}: "
          f"complete_through={output['data']['meta']['complete_through']}")
    return 0


def _test_envelope(source: str, freq: str, data, dates: list[str]) -> dict:
    return envelope(source, freq, data, dates=dates)


def _month_series(start_year=2015, start_month=1, count=24) -> list[dict]:
    rows = []
    for offset in range(count):
        year, month = _shift_month(start_year, start_month, offset)
        rows.append({"date": _month_end(year, month), "receipts_bn": 400.0,
                     "total_outlays_bn": 500.0, "net_interest_bn": 50.0})
    return rows


def _daily_series(start="2015-01-01", end="2016-12-31", public=10000.0,
                  total=15000.0) -> list[dict]:
    current = date.fromisoformat(start)
    last = date.fromisoformat(end)
    rows = []
    while current <= last:
        if current.weekday() < 5:
            rows.append({"date": current.isoformat(), "total_bn": total,
                         "public_bn": public, "intragov_bn": total - public})
        current = current.fromordinal(current.toordinal() + 1)
    return rows


def _macro_series() -> list[dict]:
    rows = []
    gdp = [9000.0, 9200.0, 9400.0, 9600.0, 10000.0, 10200.0, 10400.0, 10600.0]
    public_pct = [75.0, 75.2, 75.4, 75.6, 76.0, 76.2, 76.4, 76.6]
    for index in range(8):
        year = 2015 + index // 4
        quarter = index % 4 + 1
        month = 1 + (quarter - 1) * 3
        rows.append({"date": f"{year:04d}-{month:02d}-01",
                     "quarter": f"{year}-Q{quarter}", "gdp_bn": gdp[index],
                     "public_gdp_pct": public_pct[index],
                     "debt_gdp_pct": public_pct[index] + 25})
    return rows


def run_tests() -> None:
    passed = failed = 0

    def check(name: str, ok: bool, detail="") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"PASS {name}" + (f"  {detail}" if detail else ""))
        else:
            failed += 1
            print(f"FAIL {name}  {detail}")

    months = _month_series()
    daily_rows = _daily_series()
    macro_rows = _macro_series()
    mts = _test_envelope("treasury_fiscal_data_mts_table_9", "monthly", months,
                         [row["date"] for row in months])
    daily = _test_envelope("treasury_fiscal_data_debt_to_penny", "daily", daily_rows,
                           [row["date"] for row in daily_rows])
    macro = _test_envelope("derived_macro_debt", "quarterly",
                           {"debt": macro_rows, "meta": {
                               "gdp_last": macro_rows[-1]["date"],
                               "ratio_last": macro_rows[-1]["date"]}},
                           [row["date"] for row in macro_rows])
    result = derive_payload(mts, daily, macro)
    latest = result["data"]["latest"]
    first = result["data"]["quarterly"][0]
    check("历史严格从 2016-Q1 开始", first["quarter"] == "2016-Q1")
    check("TTM 使用恰好 12 个连续月", first["fiscal_window_start"] == "2015-04-30"
          and first["fiscal_window_end"] == "2016-03-31"
          and first["receipts_ttm_bn"] == 4800.0)
    check("overall balance 符号：盈余正", first["overall_balance_ttm_bn"] == -1200.0)
    check("primary balance=receipts-outlays+interest", first["primary_balance_ttm_bn"] == -600.0)
    check("effective r 只用公众债务分母", abs(first["effective_r_pct"] - 6.0) < 1e-12)
    check("effective r 未误用 total debt", abs(first["effective_r_pct"] - 4.0) > 1.0)
    expected_g = (10000.0 / 9000.0 - 1) * 100
    check("nominal g 严格 t/t-4", abs(first["nominal_g_pct"] - expected_g) < 1e-12)
    check("GDP SAAR 未除以 4", abs(first["primary_balance_gdp_pct"] - (-6.0)) < 1e-12)
    check("r-g 单位为 percentage points",
          abs(first["r_minus_g_pct_points"] - (6.0 - expected_g)) < 1e-12)
    expected_pstar = (6.0 - expected_g) * 76.0 / 100
    check("p* 包含 /100 单位换算",
          abs(first["stabilizing_primary_balance_pct_gdp"] - expected_pstar) < 1e-12)
    check("fiscal gap=p*-actual primary balance",
          abs(first["fiscal_gap_pct_gdp"] - (expected_pstar + 6.0)) < 1e-12)
    check("net interest/GDP 使用 TTM 与 SAAR",
          abs(first["net_interest_gdp_pct"] - 6.0) < 1e-12)
    check("net interest/receipts 同口径",
          abs(first["net_interest_receipts_pct"] - 12.5) < 1e-12)
    check("observed Δd 不被模型覆盖", abs(first["observed_delta_public_debt_gdp"] - 0.4) < 1e-12)
    check("模型 implied Δd 按季度区间 /4",
          abs(first["model_implied_delta_public_debt_gdp"]
              - first["fiscal_gap_pct_gdp"] / 4) < 1e-12)
    check("stock-flow residual 保持非零诊断",
          abs(first["stock_flow_residual_pct_gdp"]) > 0.1)
    check("完整季度输出 complete", first["calculation_status"] == "complete")
    check("正 gap 输出 gap_positive", first["trajectory_condition"] == "gap_positive")
    check("连续正 gap 只计完整季度", latest[
        "gap_positive_consecutive_complete_quarters"] == 4)
    check("C17 不输出阈值分数", result["data"]["meta"]["stress_level"] == "unscored"
          and result["data"]["meta"]["threshold_version"] is None)

    missing_months = [row for row in months if row["date"] != "2015-08-31"]
    incomplete = derive_payload(
        _test_envelope("treasury_fiscal_data_mts_table_9", "monthly", missing_months,
                       [row["date"] for row in missing_months]), daily, macro)
    incomplete_first = incomplete["data"]["quarterly"][0]
    check("缺一个月整组 TTM 置 null", incomplete_first["receipts_ttm_bn"] is None
          and incomplete_first["total_outlays_ttm_bn"] is None
          and incomplete_first["net_interest_ttm_bn"] is None)
    check("缺月季度 incomplete/unknown", incomplete_first["calculation_status"] == "incomplete"
          and incomplete_first["trajectory_condition"] == "unknown")
    check("incomplete 重置连续 gap 计数",
          incomplete_first["gap_positive_consecutive_complete_quarters"] == 0)

    missing_daily = [row for row in daily_rows if row["date"][:7] != "2015-08"]
    incomplete_debt = derive_payload(mts, _test_envelope(
        "treasury_fiscal_data_debt_to_penny", "daily", missing_daily,
        [row["date"] for row in missing_daily]), macro)["data"]["quarterly"][0]
    check("日频公众债务整月缺失不填补", incomplete_debt["average_public_debt_bn"] is None
          and incomplete_debt["effective_r_pct"] is None)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "data" / "derived").mkdir(parents=True)
        paths_payloads = [
            (root / "data" / "treasury_mts_fiscal.json", mts),
            (root / "data" / "treasury_debt_daily.json", daily),
            (root / "data" / "derived" / "macro_debt.json", macro),
        ]
        for path, payload in paths_payloads:
            path.write_text(json.dumps(payload), encoding="utf-8")
        rc = run_once(root=root)
        target = root / "data" / "derived" / "macro_fiscal_stress.json"
        first_bytes = target.read_bytes()
        first_hash = hashlib.sha256(first_bytes).hexdigest()
        check("成功派生 exit 0", rc == 0 and target.exists())
        rc = run_once(root=root)
        check("相同派生业务幂等 exit 0", rc == 0)
        check("幂等不刷新 generated_at", target.read_bytes() == first_bytes
              and hashlib.sha256(target.read_bytes()).hexdigest() == first_hash)

        old = first_bytes
        (root / "data" / "treasury_mts_fiscal.json").write_text("[]", encoding="utf-8")
        rc = run_once(root=root)
        check("裸格式上游 strict 拒绝", rc == 1)
        check("派生失败旧文件不覆盖", target.read_bytes() == old)
        check("派生失败不污染 macro_debt", json.loads(
            (root / "data" / "derived" / "macro_debt.json").read_text())[
                "source"] == "derived_macro_debt")

    workflow = (ROOT / ".github" / "workflows" / "update-cot.yml").read_text(
        encoding="utf-8")
    step_match = re.search(
        r"- name: Run derive_fiscal_stress\.py\n(?P<body>.*?)(?=\n\s+- name:)",
        workflow, re.S)
    step_body = step_match.group("body") if step_match else ""
    gate_match = re.search(
        r'case "\$\{\{ steps\.derive_fiscal_stress\.outputs\.code \}\}" in'
        r'(?P<body>.*?)esac', workflow, re.S)
    gate_body = gate_match.group("body") if gate_match else ""
    check("workflow validation 执行 fiscal derive 离线测试",
          "python derive_fiscal_stress.py --test" in workflow)
    check("workflow fiscal derive 捕获真实退出码", bool(step_match)
          and "id: derive_fiscal_stress" in step_body
          and 'echo "code=$?" >> "$GITHUB_OUTPUT"' in step_body
          and "exit 0" in step_body)
    check("workflow commit 包含 fiscal derived envelope",
          "data/derived/macro_fiscal_stress.json" in workflow)
    check("workflow fiscal derive gate 只有 0/1 与异常码", bool(gate_match)
          and "1) fail" in gate_body and '0|"")' in gate_body
          and "*) fail" in gate_body and "2)" not in gate_body)

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)


def main() -> int:
    if "--test" in sys.argv:
        run_tests()
        return 0
    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
