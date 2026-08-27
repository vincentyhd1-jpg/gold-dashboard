#!/usr/bin/env python3
"""Build the audited accounting basis for the browser CBO scenario lab."""
from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
from pathlib import Path

from data_envelope import assert_envelope, envelope, upstream_ref
from io_utils import atomic_write_json, read_json_or, sweep_stale_tmp

ROOT = Path(__file__).resolve().parent
INPUT_PATH = ROOT / "data" / "derived" / "cbo_baseline_latest.json"
OUTPUT_PATH = ROOT / "data" / "derived" / "cbo_scenario_basis.json"
SOURCE = "cbo_fiscal_scenario_basis"
MODEL_VERSION = "1"
ANCHOR_YEAR = 2025
PROJECTION_START_YEAR = 2026
PROJECTION_END_YEAR = 2036
REQUIRED_FIELDS = (
    "year", "kind", "debt_held_by_public_bn",
    "debt_held_by_public_pct_gdp", "nominal_gdp_bn", "nominal_g_pct",
    "primary_balance_pct_gdp", "net_interest_pct_gdp",
    "overall_balance_pct_gdp",
)


class CboScenarioBasisFailure(RuntimeError):
    """The official baseline cannot safely support the scenario basis."""


def _finite(value, field: str) -> float:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value)):
        raise CboScenarioBasisFailure(f"{field}: 必须是有限数值，得到 {value!r}")
    return float(value)


def _validate_baseline(payload: dict) -> list[dict]:
    try:
        assert_envelope(payload)
    except ValueError as exc:
        raise CboScenarioBasisFailure(f"CBO baseline 不是 strict envelope: {exc}") from exc
    if payload.get("source") != "cbo_budget_baseline":
        raise CboScenarioBasisFailure("CBO baseline source 非法")
    if payload.get("freq") != "annual" or payload.get("date_field") != "year":
        raise CboScenarioBasisFailure("CBO baseline 必须是 annual/year")
    rows = payload.get("data", {}).get("annual")
    if not isinstance(rows, list):
        raise CboScenarioBasisFailure("CBO baseline annual 缺失")
    if [row.get("year") for row in rows] != list(range(ANCHOR_YEAR,
                                                       PROJECTION_END_YEAR + 1)):
        raise CboScenarioBasisFailure("CBO baseline 必须连续覆盖 2025–2036")
    for index, row in enumerate(rows):
        expected_kind = "actual" if index == 0 else "projection"
        if row.get("kind") != expected_kind:
            raise CboScenarioBasisFailure(f"{row.get('year')}: actual/projection 分界非法")
        missing = [field for field in REQUIRED_FIELDS if field not in row]
        if missing:
            raise CboScenarioBasisFailure(f"{row.get('year')}: 缺字段 {missing}")
        numeric_fields = REQUIRED_FIELDS[2:]
        for field in numeric_fields:
            if field == "nominal_g_pct" and index == 0 and row[field] is None:
                continue
            _finite(row[field], f"{row['year']} {field}")
        debt = _finite(row["debt_held_by_public_bn"], "debt bn")
        gdp = _finite(row["nominal_gdp_bn"], "GDP bn")
        debt_pct = _finite(row["debt_held_by_public_pct_gdp"], "debt/GDP")
        if not 1_000 <= debt <= 500_000 or not 1_000 <= gdp <= 500_000:
            raise CboScenarioBasisFailure(f"{row['year']}: USD bn 单位锚点失败")
        if not 50 <= debt_pct <= 250:
            raise CboScenarioBasisFailure(f"{row['year']}: debt/GDP 百分数单位锚点失败")
        primary = _finite(row["primary_balance_pct_gdp"], "primary balance")
        interest = _finite(row["net_interest_pct_gdp"], "net interest")
        overall = _finite(row["overall_balance_pct_gdp"], "overall balance")
        if interest < 0 or abs((primary - interest) - overall) > 0.01:
            raise CboScenarioBasisFailure(
                f"{row['year']}: surplus-positive / interest sign 口径损坏")
    return rows


def build_basis(payload: dict) -> dict:
    rows = _validate_baseline(payload)
    annual = []
    for index, row in enumerate(rows):
        basis_row = {
            "year": row["year"],
            "kind": row["kind"],
            "baseline_debt_bn": row["debt_held_by_public_bn"],
            "baseline_debt_pct_gdp": row["debt_held_by_public_pct_gdp"],
            "baseline_gdp_bn": row["nominal_gdp_bn"],
            "baseline_nominal_g_pct": row["nominal_g_pct"],
            "baseline_primary_balance_pct_gdp": row["primary_balance_pct_gdp"],
            "baseline_net_interest_pct_gdp": row["net_interest_pct_gdp"],
            "baseline_overall_balance_pct_gdp": row["overall_balance_pct_gdp"],
            "baseline_sfa_bn": None,
            "baseline_sfa_pct_gdp": None,
        }
        if index:
            previous_debt = rows[index - 1]["debt_held_by_public_bn"]
            deficit = (-row["overall_balance_pct_gdp"] / 100
                       * row["nominal_gdp_bn"])
            sfa_bn = row["debt_held_by_public_bn"] - previous_debt - deficit
            sfa_pct = sfa_bn / row["nominal_gdp_bn"] * 100
            closure = previous_debt + deficit + sfa_bn
            if abs(closure - row["debt_held_by_public_bn"]) > 1e-8:
                raise CboScenarioBasisFailure(f"{row['year']}: SFA accounting closure 失败")
            basis_row["baseline_sfa_bn"] = sfa_bn
            basis_row["baseline_sfa_pct_gdp"] = sfa_pct
        annual.append(basis_row)

    source_data = payload["data"]
    vintage = source_data.get("vintage", {})
    return {
        "annual": annual,
        "vintage": {
            "vintage_id": vintage.get("vintage_id"),
            "publication_date": vintage.get("publication_date"),
            "source_page_url": vintage.get("source_page_url"),
        },
        "methodology": {
            "model_version": MODEL_VERSION,
            "anchor_year": ANCHOR_YEAR,
            "projection_start_year": PROJECTION_START_YEAR,
            "projection_end_year": PROJECTION_END_YEAR,
            "stock_flow_reconciliation_method": (
                "official_debt_change_minus_overall_deficit_amount"),
            "overall_balance_sign": "surplus_positive_deficit_negative",
            "primary_balance_shock_sign": "positive_is_fiscal_improvement",
            "net_interest_spending_shock_sign": "positive_is_higher_spending",
            "scenario_is_deterministic": True,
            "scenario_is_not_forecast": True,
            "no_probability": True,
            "no_crisis_year": True,
            "no_forward_fiscal_gap": True,
            "official_baseline_is_read_only": True,
        },
    }


def build_output(input_path: Path = INPUT_PATH) -> dict:
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CboScenarioBasisFailure(f"无法读取 CBO baseline: {exc}") from exc
    data = build_basis(payload)
    try:
        reference = upstream_ref(str(input_path), "cbo_budget_baseline")
    except ValueError as exc:
        raise CboScenarioBasisFailure(f"CBO baseline upstream ref 非法: {exc}") from exc
    return envelope(
        SOURCE, "annual", data,
        dates=[str(row["year"]) for row in data["annual"]],
        date_field="year", derived_from=[reference] if reference else [],
        info=[
            "deterministic_user_scenario_basis_not_a_forecast",
            "official_cbo_baseline_remains_read_only",
            "stock_flow_adjustment_is_accounting_reconciliation_not_risk_metric",
            "zero_shock_reproduces_official_baseline",
            "no_probability_no_crisis_year_no_forward_fiscal_gap",
        ],
    )


def _comparable(payload: dict) -> tuple:
    keys = ("source", "freq", "date_field", "coverage", "derived_from",
            "warnings", "info", "data")
    return tuple(payload.get(key) for key in keys)


def publish(output: dict, output_path: Path = OUTPUT_PATH,
            writer=atomic_write_json) -> bool:
    assert_envelope(output)
    existing = read_json_or(str(output_path), None)
    if existing is not None:
        try:
            assert_envelope(existing)
        except ValueError as exc:
            raise CboScenarioBasisFailure(f"现有 scenario basis 不是合法信封: {exc}") from exc
        if _comparable(existing) == _comparable(output):
            return False
    writer(str(output_path), output, compact=False)
    return True


def run_once(input_path: Path = INPUT_PATH, output_path: Path = OUTPUT_PATH) -> int:
    sweep_stale_tmp(str(output_path))
    try:
        publish(build_output(input_path), output_path)
    except (CboScenarioBasisFailure, ValueError, OSError) as exc:
        print(f"CBO scenario basis 派生失败：{exc}", file=sys.stderr)
        return 1
    return 0


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

    original_input = INPUT_PATH.read_bytes()
    original_vintage = (ROOT / "data" / "cbo" / "baseline-2026-02.json").read_bytes()
    payload = json.loads(original_input)
    built_output = build_output(INPUT_PATH)
    data = built_output["data"]
    rows = data["annual"]
    try:
        assert_envelope(built_output)
        strict_output = True
    except ValueError:
        strict_output = False
    check("strict envelope", strict_output and built_output.get("schema_version") == 0
          and built_output.get("source") == SOURCE)
    check("annual frequency", built_output.get("freq") == "annual"
          and built_output.get("date_field") == "year")
    check("2025 anchor", rows[0]["year"] == 2025 and rows[0]["kind"] == "actual")
    check("2026–2036 projection", [r["year"] for r in rows[1:]] == list(range(2026, 2037)))
    check("source field completeness", all(set(row) >= {
        "baseline_debt_bn", "baseline_debt_pct_gdp", "baseline_gdp_bn",
        "baseline_nominal_g_pct", "baseline_primary_balance_pct_gdp",
        "baseline_net_interest_pct_gdp", "baseline_overall_balance_pct_gdp",
        "baseline_sfa_bn", "baseline_sfa_pct_gdp"} for row in rows))
    check("projection required fields have no null", all(
        all(value is not None for key, value in row.items() if key not in ("kind",))
        for row in rows[1:]))
    closure_errors = []
    for previous, row in zip(rows, rows[1:]):
        deficit = -row["baseline_overall_balance_pct_gdp"] / 100 * row["baseline_gdp_bn"]
        closure_errors.append(abs(previous["baseline_debt_bn"] + deficit
                                  + row["baseline_sfa_bn"] - row["baseline_debt_bn"]))
    check("SFA accounting closure", max(closure_errors) <= 1e-8,
          f"max_error={max(closure_errors):.3g}")
    check("zero-shock reproduction basis complete", all(
        row["baseline_nominal_g_pct"] is not None
        and row["baseline_sfa_pct_gdp"] is not None for row in rows[1:]))

    bare_rejected = unit_rejected = sign_rejected = corruption_rejected = False
    try:
        build_basis(payload["data"])
    except CboScenarioBasisFailure:
        bare_rejected = True
    corrupt = copy.deepcopy(payload)
    del corrupt["data"]["annual"][1]["overall_balance_pct_gdp"]
    try:
        build_basis(corrupt)
    except CboScenarioBasisFailure:
        corruption_rejected = True
    corrupt = copy.deepcopy(payload)
    corrupt["data"]["annual"][1]["nominal_gdp_bn"] /= 1000
    try:
        build_basis(corrupt)
    except CboScenarioBasisFailure:
        unit_rejected = True
    corrupt = copy.deepcopy(payload)
    corrupt["data"]["annual"][1]["primary_balance_pct_gdp"] *= -1
    try:
        build_basis(corrupt)
    except CboScenarioBasisFailure:
        sign_rejected = True
    check("input corruption fails", bare_rejected and corruption_rejected)
    check("unit corruption fails", unit_rejected)
    check("sign corruption fails", sign_rejected)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        input_path = root / "cbo_baseline_latest.json"
        output_path = root / "cbo_scenario_basis.json"
        input_path.write_bytes(original_input)
        output = build_output(input_path)
        first = publish(output, output_path)
        first_bytes = output_path.read_bytes()
        second = publish(build_output(input_path), output_path)
        check("idempotence", first and not second and output_path.read_bytes() == first_bytes)
        check("unchanged business data preserves generated_at",
              json.loads(output_path.read_text(encoding="utf-8"))["generated_at"]
              == json.loads(first_bytes)["generated_at"])
        old = output_path.read_bytes()
        bad = copy.deepcopy(payload)
        bad["data"]["annual"][2]["nominal_gdp_bn"] = None
        input_path.write_text(json.dumps(bad), encoding="utf-8")
        check("failed derive does not overwrite old basis",
              run_once(input_path, output_path) == 1 and output_path.read_bytes() == old)

    check("CBO immutable vintage unchanged",
          (ROOT / "data" / "cbo" / "baseline-2026-02.json").read_bytes() == original_vintage)
    check("cbo_baseline_latest unchanged", INPUT_PATH.read_bytes() == original_input)
    check("methodology prohibits forecast/probability/crisis/gap",
          data["methodology"]["scenario_is_not_forecast"]
          and data["methodology"]["no_probability"]
          and data["methodology"]["no_crisis_year"]
          and data["methodology"]["no_forward_fiscal_gap"])
    workflow = (ROOT / ".github" / "workflows" / "update-cot.yml").read_text(
        encoding="utf-8")
    test_command = "python derive_cbo_scenario_basis.py --test"
    check("daily workflow 只离线运行 scenario basis guard",
          workflow.count(test_command) == 1
          and "python derive_cbo_scenario_basis.py\n" not in workflow)
    check("daily workflow 不 commit scenario basis 或用户 scenario",
          "data/derived/cbo_scenario_basis.json" not in workflow
          and "user_scenario" not in workflow)
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
