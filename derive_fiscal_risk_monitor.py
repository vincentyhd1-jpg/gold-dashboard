#!/usr/bin/env python3
"""Derive the descriptive C18C fiscal risk monitor from the C17 output."""
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
INPUT_PATH = ROOT / "data" / "derived" / "macro_fiscal_stress.json"
OUTPUT_PATH = ROOT / "data" / "derived" / "fiscal_risk_monitor.json"
SOURCE = "derived_fiscal_risk_monitor"
UPSTREAM_SOURCE = "derived_fiscal_sustainability"
MODEL_VERSION = "0"
STALE_MARKER = (
    "committed fiscal risk monitor is stale relative to current fiscal stress source"
)

CORE_FIELDS = (
    "public_debt_gdp_pct",
    "effective_r_pct",
    "nominal_g_pct",
    "r_minus_g_pct_points",
    "primary_balance_gdp_pct",
    "stabilizing_primary_balance_pct_gdp",
    "fiscal_gap_pct_gdp",
    "net_interest_gdp_pct",
    "net_interest_receipts_pct",
    "observed_delta_public_debt_gdp",
    "model_implied_delta_public_debt_gdp",
    "stock_flow_residual_pct_gdp",
)
YOY_FIELDS = {
    "debt_gdp_yoy_change_pp": "public_debt_gdp_pct",
    "primary_balance_yoy_change_pp": "primary_balance_gdp_pct",
    "fiscal_gap_yoy_change_pp": "fiscal_gap_pct_gdp",
    "r_minus_g_yoy_change_pp": "r_minus_g_pct_points",
    "net_interest_gdp_yoy_change_pp": "net_interest_gdp_pct",
    "net_interest_receipts_yoy_change_pp": "net_interest_receipts_pct",
}


class FiscalRiskMonitorFailure(RuntimeError):
    """The C17 source cannot safely support the descriptive monitor."""


def _finite_or_none(value, field: str) -> float | None:
    if value is None:
        return None
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value)):
        raise FiscalRiskMonitorFailure(
            f"{field}: must be finite or null, got {value!r}")
    return float(value)


def _quarter_parts(quarter: str) -> tuple[int, int]:
    try:
        year_text, quarter_text = quarter.split("-Q")
        year = int(year_text)
        number = int(quarter_text)
    except (AttributeError, ValueError) as exc:
        raise FiscalRiskMonitorFailure(f"invalid quarter: {quarter!r}") from exc
    if len(year_text) != 4 or number not in (1, 2, 3, 4):
        raise FiscalRiskMonitorFailure(f"invalid quarter: {quarter!r}")
    return year, number


def _quarter_date(quarter: str) -> str:
    year, number = _quarter_parts(quarter)
    return f"{year:04d}-{1 + (number - 1) * 3:02d}-01"


def _quarter_index(quarter: str) -> int:
    year, number = _quarter_parts(quarter)
    return year * 4 + number - 1


def _condition(value: float | None, positive: str, zero: str,
               negative: str) -> str:
    if value is None:
        return "unknown"
    if value > 0:
        return positive
    if value < 0:
        return negative
    return zero


def _validate_source(payload: dict) -> list[dict]:
    try:
        assert_envelope(payload)
    except ValueError as exc:
        raise FiscalRiskMonitorFailure(
            f"fiscal stress source is not a strict envelope: {exc}") from exc
    if payload.get("source") != UPSTREAM_SOURCE:
        raise FiscalRiskMonitorFailure("fiscal stress source identity mismatch")
    if payload.get("freq") != "quarterly" or payload.get("date_field") != "date":
        raise FiscalRiskMonitorFailure("fiscal stress source must be quarterly/date")
    rows = payload.get("data", {}).get("quarterly")
    if not isinstance(rows, list) or not rows:
        raise FiscalRiskMonitorFailure("fiscal stress quarterly rows are empty")

    previous_date = None
    seen_quarters: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise FiscalRiskMonitorFailure("fiscal stress row must be an object")
        quarter = row.get("quarter")
        date = row.get("date")
        if quarter in seen_quarters:
            raise FiscalRiskMonitorFailure(f"duplicate quarter: {quarter}")
        seen_quarters.add(quarter)
        if date != _quarter_date(quarter):
            raise FiscalRiskMonitorFailure(
                f"{quarter}: date does not match quarter ({date!r})")
        if previous_date is not None and date <= previous_date:
            raise FiscalRiskMonitorFailure("quarterly rows are not strictly chronological")
        previous_date = date

        status = row.get("calculation_status")
        if status not in ("complete", "incomplete"):
            raise FiscalRiskMonitorFailure(f"{quarter}: invalid calculation_status")
        for field in CORE_FIELDS:
            if field not in row:
                raise FiscalRiskMonitorFailure(f"{quarter}: missing {field}")
            value = _finite_or_none(row[field], f"{quarter} {field}")
            if status == "complete" and value is None:
                raise FiscalRiskMonitorFailure(
                    f"{quarter}: complete row has null {field}")

        r_minus_g = row["r_minus_g_pct_points"]
        if r_minus_g is not None and abs(
                r_minus_g - (row["effective_r_pct"] - row["nominal_g_pct"])) > 1e-9:
            raise FiscalRiskMonitorFailure(f"{quarter}: r-minus-g identity failed")
        gap = row["fiscal_gap_pct_gdp"]
        if gap is not None and abs(
                gap - (row["stabilizing_primary_balance_pct_gdp"]
                       - row["primary_balance_gdp_pct"])) > 1e-9:
            raise FiscalRiskMonitorFailure(f"{quarter}: fiscal-gap identity failed")
        expected_trajectory = "unknown" if gap is None else (
            "gap_positive" if gap > 0 else "stabilizing_condition_met")
        if row.get("trajectory_condition") != expected_trajectory:
            raise FiscalRiskMonitorFailure(
                f"{quarter}: trajectory condition sign mismatch")
    return rows


def build_monitor(payload: dict) -> dict:
    rows = _validate_source(payload)
    by_quarter = {row["quarter"]: row for row in rows}
    quarterly = []
    for row in rows:
        current = copy.deepcopy(row)
        year, number = _quarter_parts(row["quarter"])
        prior = by_quarter.get(f"{year - 1}-Q{number}")
        for output_field, source_field in YOY_FIELDS.items():
            current_value = row[source_field]
            prior_value = prior.get(source_field) if prior else None
            current[output_field] = (
                current_value - prior_value
                if row.get("calculation_status") == "complete"
                and prior is not None
                and prior.get("calculation_status") == "complete"
                and current_value is not None
                and prior_value is not None
                else None
            )
        current["fiscal_gap_condition"] = _condition(
            row["fiscal_gap_pct_gdp"], "gap_positive", "gap_nonpositive",
            "gap_nonpositive")
        current["r_minus_g_condition"] = _condition(
            row["r_minus_g_pct_points"], "positive", "zero", "negative")
        current["primary_balance_condition"] = _condition(
            row["primary_balance_gdp_pct"], "surplus", "balanced", "deficit")
        current["debt_yoy_direction"] = _condition(
            current["debt_gdp_yoy_change_pp"], "rising", "flat", "falling")
        quarterly.append(current)

    complete_rows = [row for row in quarterly
                     if row.get("calculation_status") == "complete"
                     and all(row.get(field) is not None for field in CORE_FIELDS)]
    if not complete_rows:
        raise FiscalRiskMonitorFailure("no complete fiscal-stress quarter")
    latest_complete = copy.deepcopy(complete_rows[-1])
    latest_observed_quarter = quarterly[-1]["quarter"]
    latest_complete_quarter = latest_complete["quarter"]
    lag = _quarter_index(latest_observed_quarter) - _quarter_index(latest_complete_quarter)
    if lag < 0:
        raise FiscalRiskMonitorFailure("latest complete quarter is after observed quarter")

    return {
        "quarterly": quarterly,
        "latest_complete": latest_complete,
        "latest_observed_quarter": latest_observed_quarter,
        "latest_complete_quarter": latest_complete_quarter,
        "complete_lag_quarters": lag,
        "methodology": {
            "model_version": MODEL_VERSION,
            "historical_source": "C17",
            "yoy_method": "same_quarter_previous_year",
            "latest_snapshot": "latest_complete",
            "source_is_c17_derived_fiscal_stress": True,
            "missing_prior_year_remains_null": True,
            "null_is_not_zero": True,
            "zero_is_only_mathematical_boundary": True,
            "zero_boundaries": {
                "fiscal_gap_pct_gdp": 0,
                "r_minus_g_pct_points": 0,
                "primary_balance_gdp_pct": 0,
                "debt_gdp_yoy_change_pp": 0,
            },
            "no_forward_fill": True,
            "no_interpolation": True,
            "no_risk_score": True,
            "no_composite_score": True,
            "no_probability": True,
            "no_crisis_year": True,
            "no_dynamic_risk_color": True,
            "no_policy_threshold": True,
            "no_arbitrary_thresholds": True,
            "no_risk_colors": True,
            "no_forward_fiscal_gap": True,
            "market_yields_are_not_effective_r": True,
            "cbo_baseline_is_context_not_prediction_truth": True,
            "no_market_rate_substitution_for_effective_r": True,
            "conditions_are_descriptive_signs_only": True,
        },
    }


def build_output(input_path: Path = INPUT_PATH) -> dict:
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FiscalRiskMonitorFailure(f"cannot read fiscal stress source: {exc}") from exc
    data = build_monitor(payload)
    try:
        reference = upstream_ref(str(input_path), UPSTREAM_SOURCE)
    except ValueError as exc:
        raise FiscalRiskMonitorFailure(f"invalid fiscal stress upstream ref: {exc}") from exc
    return envelope(
        SOURCE, "quarterly", data,
        dates=[row["date"] for row in data["quarterly"]],
        date_field="date", derived_from=[reference] if reference else [],
        info=[
            "descriptive_multi_indicator_monitor_not_a_risk_score",
            "same_quarter_year_over_year_changes_without_fill",
            "zero_lines_are_mathematical_sign_boundaries_not_policy_thresholds",
            "no_probability_no_crisis_year_no_dynamic_risk_color",
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
            raise FiscalRiskMonitorFailure(
                f"existing fiscal risk monitor is not a strict envelope: {exc}") from exc
        if _comparable(existing) == _comparable(output):
            return False
    writer(str(output_path), output, compact=False)
    return True


def run_once(input_path: Path = INPUT_PATH, output_path: Path = OUTPUT_PATH) -> int:
    sweep_stale_tmp(str(output_path))
    try:
        publish(build_output(input_path), output_path)
    except (FiscalRiskMonitorFailure, ValueError, OSError) as exc:
        print(f"fiscal risk monitor derivation failed: {exc}", file=sys.stderr)
        return 1
    return 0


def run_tests() -> None:
    passed = failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"PASS {name}" + (f"  {detail}" if detail else ""))
        else:
            failed += 1
            print(f"FAIL {name}" + (f"  {detail}" if detail else ""))

    original_input = INPUT_PATH.read_bytes()
    source = json.loads(original_input)
    expected = build_output(INPUT_PATH)
    data = expected["data"]
    rows = data["quarterly"]
    latest = data["latest_complete"]

    strict = True
    try:
        assert_envelope(expected)
    except ValueError:
        strict = False
    check("strict output envelope", strict)
    check("output identity", expected["source"] == SOURCE
          and expected["freq"] == "quarterly" and expected["date_field"] == "date")
    check("coverage follows quarterly dates", expected["coverage"] == {
        "first": rows[0]["date"], "last": rows[-1]["date"], "count": len(rows)})
    check("derived_from identifies current C17 source",
          expected["derived_from"] == [upstream_ref(str(INPUT_PATH), UPSTREAM_SOURCE)])
    check("quarterly count copied", len(rows) == len(source["data"]["quarterly"]))
    check("quarterly source fields copied exactly", all(
        all(row.get(key) == source_row.get(key) for key in source_row)
        for row, source_row in zip(rows, source["data"]["quarterly"])))
    source_rows = source["data"]["quarterly"]
    expected_latest_observed = source_rows[-1]["quarter"]
    expected_complete_source_rows = [
        row for row in source_rows
        if row.get("calculation_status") == "complete"
        and all(row.get(field) is not None for field in CORE_FIELDS)
    ]
    expected_latest_complete = expected_complete_source_rows[-1]["quarter"]
    expected_lag = (_quarter_index(expected_latest_observed)
                    - _quarter_index(expected_latest_complete))
    expected_latest_row = next(
        row for row in rows if row["quarter"] == expected_latest_complete)
    check("latest observed quarter dynamically follows source",
          data["latest_observed_quarter"] == expected_latest_observed)
    check("latest complete quarter dynamically follows source",
          data["latest_complete_quarter"] == expected_latest_complete)
    check("complete lag dynamically follows source",
          data["complete_lag_quarters"] == expected_lag)
    check("latest complete equals dynamically selected derived row",
          latest == expected_latest_row
          and latest["calculation_status"] == "complete"
          and all(latest[field] is not None for field in CORE_FIELDS))

    row_by_quarter = {row["quarter"]: row for row in rows}
    source_by_quarter = {row["quarter"]: row for row in source_rows}

    def has_complete_same_quarter_prior(row):
        year, number = _quarter_parts(row["quarter"])
        return (row.get("calculation_status") == "complete"
                and source_by_quarter.get(
                    f"{year - 1}-Q{number}", {}).get(
                        "calculation_status") == "complete")

    yoy_anchor = next((row for row in rows
                       if has_complete_same_quarter_prior(row)), None)
    check("dynamic same-quarter YoY anchor exists", yoy_anchor is not None)
    current = yoy_anchor
    current_parts = _quarter_parts(current["quarter"])
    prior = row_by_quarter[f"{current_parts[0] - 1}-Q{current_parts[1]}"]
    for output_field, source_field in YOY_FIELDS.items():
        check(f"{output_field} same-quarter YoY",
              abs(current[output_field]
                  - (current[source_field] - prior[source_field])) < 1e-12)
    current_index = rows.index(current)
    previous_quarter = rows[current_index - 1] if current_index else None
    check("same-quarter YoY is not previous-quarter change",
          previous_quarter is not None and any(
              abs((current[source_field] - prior[source_field])
                  - (current[source_field] - previous_quarter[source_field])) > 1e-12
              for source_field in YOY_FIELDS.values()))
    no_prior_row = next(row for row in rows if (
        f"{_quarter_parts(row['quarter'])[0] - 1}-Q"
        f"{_quarter_parts(row['quarter'])[1]}" not in row_by_quarter))
    check("missing-prior production row YoY remains null", all(
        no_prior_row[field] is None for field in YOY_FIELDS))

    def expected_condition(value, positive, zero, negative):
        if value is None:
            return "unknown"
        return positive if value > 0 else negative if value < 0 else zero

    check("production conditions dynamically follow current signs", all(
        row["fiscal_gap_condition"] == expected_condition(
            row["fiscal_gap_pct_gdp"], "gap_positive", "gap_nonpositive",
            "gap_nonpositive")
        and row["r_minus_g_condition"] == expected_condition(
            row["r_minus_g_pct_points"], "positive", "zero", "negative")
        and row["primary_balance_condition"] == expected_condition(
            row["primary_balance_gdp_pct"], "surplus", "balanced", "deficit")
        and row["debt_yoy_direction"] == expected_condition(
            row["debt_gdp_yoy_change_pp"], "rising", "flat", "falling")
        for row in rows))

    bare_rejected = duplicate_rejected = chronology_rejected = False
    missing_rejected = nonfinite_rejected = identity_rejected = sign_rejected = False
    try:
        build_monitor(source["data"])
    except FiscalRiskMonitorFailure:
        bare_rejected = True
    corrupt = copy.deepcopy(source)
    corrupt["data"]["quarterly"].append(copy.deepcopy(corrupt["data"]["quarterly"][-1]))
    try:
        build_monitor(corrupt)
    except FiscalRiskMonitorFailure:
        duplicate_rejected = True
    corrupt = copy.deepcopy(source)
    corrupt["data"]["quarterly"][1], corrupt["data"]["quarterly"][2] = (
        corrupt["data"]["quarterly"][2], corrupt["data"]["quarterly"][1])
    try:
        build_monitor(corrupt)
    except FiscalRiskMonitorFailure:
        chronology_rejected = True
    corrupt = copy.deepcopy(source)
    del corrupt["data"]["quarterly"][0]["net_interest_gdp_pct"]
    try:
        build_monitor(corrupt)
    except FiscalRiskMonitorFailure:
        missing_rejected = True
    corrupt = copy.deepcopy(source)
    corrupt["data"]["quarterly"][0]["net_interest_gdp_pct"] = math.inf
    try:
        build_monitor(corrupt)
    except FiscalRiskMonitorFailure:
        nonfinite_rejected = True
    corrupt = copy.deepcopy(source)
    corrupt["data"]["quarterly"][0]["r_minus_g_pct_points"] += 0.001
    try:
        build_monitor(corrupt)
    except FiscalRiskMonitorFailure:
        identity_rejected = True
    corrupt = copy.deepcopy(source)
    corrupt["data"]["quarterly"][0]["trajectory_condition"] = "unknown"
    try:
        build_monitor(corrupt)
    except FiscalRiskMonitorFailure:
        sign_rejected = True
    check("bare input rejected", bare_rejected)
    check("duplicate quarter rejected", duplicate_rejected)
    check("out-of-order quarter rejected", chronology_rejected)
    check("missing core field rejected", missing_rejected)
    check("non-finite value rejected", nonfinite_rejected)
    check("r-minus-g identity enforced", identity_rejected)
    check("trajectory sign enforced", sign_rejected)

    missing_prior = copy.deepcopy(source)
    missing_prior_quarter = prior["quarter"]
    current_quarter = current["quarter"]
    missing_prior["data"]["quarterly"] = [
        row for row in missing_prior["data"]["quarterly"]
        if row["quarter"] != missing_prior_quarter]
    missing_monitor = build_monitor(missing_prior)
    missing_current = next(row for row in missing_monitor["quarterly"]
                           if row["quarter"] == current_quarter)
    check("missing same-quarter prior produces null, never zero", all(
        missing_current[field] is None for field in YOY_FIELDS))

    incomplete_prior = copy.deepcopy(source)
    incomplete_prior_row = next(
        row for row in incomplete_prior["data"]["quarterly"]
        if row["quarter"] == missing_prior_quarter)
    incomplete_prior_row["calculation_status"] = "incomplete"
    for field in CORE_FIELDS:
        incomplete_prior_row[field] = None
    incomplete_prior_row["trajectory_condition"] = "unknown"
    incomplete_monitor = build_monitor(incomplete_prior)
    incomplete_current = next(
        row for row in incomplete_monitor["quarterly"]
        if row["quarter"] == current_quarter)
    check("incomplete or null same-quarter prior produces null, never zero", all(
        incomplete_current[field] is None for field in YOY_FIELDS))

    rolling_complete = copy.deepcopy(source)
    rolling_rows = rolling_complete["data"]["quarterly"]
    rolling_last = rolling_rows[-1]
    rolling_template = next(
        row for row in reversed(rolling_rows[:-1])
        if row.get("calculation_status") == "complete"
        and all(row.get(field) is not None for field in CORE_FIELDS))
    for field in CORE_FIELDS:
        rolling_last[field] = rolling_template[field]
    rolling_last["calculation_status"] = "complete"
    rolling_last["trajectory_condition"] = (
        "gap_positive" if rolling_last["fiscal_gap_pct_gdp"] > 0
        else "stabilizing_condition_met")
    rolling_complete_data = build_monitor(rolling_complete)
    check("rolling fixture latest observed equals latest complete with lag zero",
          rolling_complete_data["latest_observed_quarter"] == rolling_last["quarter"]
          and rolling_complete_data["latest_complete_quarter"] == rolling_last["quarter"]
          and rolling_complete_data["complete_lag_quarters"] == 0
          and rolling_complete_data["latest_complete"]
              == rolling_complete_data["quarterly"][-1])

    rolling_incomplete = copy.deepcopy(rolling_complete)
    new_row = copy.deepcopy(rolling_incomplete["data"]["quarterly"][-1])
    last_year, last_number = _quarter_parts(new_row["quarter"])
    new_number = 1 if last_number == 4 else last_number + 1
    new_year = last_year + 1 if last_number == 4 else last_year
    new_row["quarter"] = f"{new_year}-Q{new_number}"
    new_row["date"] = _quarter_date(new_row["quarter"])
    new_row["calculation_status"] = "incomplete"
    for field in CORE_FIELDS:
        new_row[field] = None
    new_row["trajectory_condition"] = "unknown"
    rolling_incomplete["data"]["quarterly"].append(new_row)
    rolling_incomplete_data = build_monitor(rolling_incomplete)
    rolling_expected_lag = (_quarter_index(new_row["quarter"])
                            - _quarter_index(rolling_last["quarter"]))
    check("rolling fixture new incomplete quarter increases lag dynamically",
          rolling_incomplete_data["latest_observed_quarter"] == new_row["quarter"]
          and rolling_incomplete_data["latest_complete_quarter"] == rolling_last["quarter"]
          and rolling_incomplete_data["complete_lag_quarters"] == rolling_expected_lag
          and rolling_expected_lag > 0)

    condition_target_quarter = expected_latest_complete
    condition_year, condition_number = _quarter_parts(condition_target_quarter)
    condition_prior_quarter = f"{condition_year - 1}-Q{condition_number}"

    def condition_fixture(gap, r_minus_g, primary, debt_change):
        fixture = copy.deepcopy(source)
        fixture_rows = fixture["data"]["quarterly"]
        target = next(
            row for row in fixture_rows
            if row["quarter"] == condition_target_quarter)
        fixture_prior = next(
            row for row in fixture_rows
            if row["quarter"] == condition_prior_quarter)
        target["r_minus_g_pct_points"] = r_minus_g
        target["effective_r_pct"] = target["nominal_g_pct"] + r_minus_g
        target["primary_balance_gdp_pct"] = primary
        target["stabilizing_primary_balance_pct_gdp"] = primary + gap
        target["fiscal_gap_pct_gdp"] = gap
        target["trajectory_condition"] = (
            "gap_positive" if gap > 0 else "stabilizing_condition_met")
        target["public_debt_gdp_pct"] = (
            fixture_prior["public_debt_gdp_pct"] + debt_change)
        built = build_monitor(fixture)
        return built["latest_complete"]

    opposite_row = condition_fixture(0.5, 1.0, 0.25, -1.0)
    zero_row = condition_fixture(0.0, 0.0, 0.0, 0.0)
    negative_row = condition_fixture(-0.5, -1.0, -0.25, 1.0)
    check("synthetic opposite conditions are independent of production snapshot",
          opposite_row["quarter"] == condition_target_quarter
          and opposite_row["fiscal_gap_condition"] == "gap_positive"
          and opposite_row["r_minus_g_condition"] == "positive"
          and opposite_row["primary_balance_condition"] == "surplus"
          and opposite_row["debt_yoy_direction"] == "falling")
    check("synthetic zero boundaries map exactly",
          zero_row["fiscal_gap_condition"] == "gap_nonpositive"
          and zero_row["r_minus_g_condition"] == "zero"
          and zero_row["primary_balance_condition"] == "balanced"
          and zero_row["debt_yoy_direction"] == "flat")
    check("synthetic negative conditions map exactly",
          negative_row["fiscal_gap_condition"] == "gap_nonpositive"
          and negative_row["r_minus_g_condition"] == "negative"
          and negative_row["primary_balance_condition"] == "deficit"
          and negative_row["debt_yoy_direction"] == "rising")

    method = data["methodology"]
    check("methodology prohibits score/threshold/probability", method["no_risk_score"]
          and method["no_composite_score"] and method["no_policy_threshold"]
          and method["no_arbitrary_thresholds"]
          and method["no_probability"])
    check("methodology prohibits crisis year and dynamic colors", method["no_crisis_year"]
          and method["no_dynamic_risk_color"])
    check("methodology prohibits fill and interpolation", method["no_forward_fill"]
          and method["no_interpolation"] and method["missing_prior_year_remains_null"])
    check("methodology keeps market rates contextual", method[
        "no_market_rate_substitution_for_effective_r"]
          and method["market_yields_are_not_effective_r"]
          and method["cbo_baseline_is_context_not_prediction_truth"])
    check("methodology zero boundaries are exact and exhaustive",
          method["zero_boundaries"] == {
              "fiscal_gap_pct_gdp": 0,
              "r_minus_g_pct_points": 0,
              "primary_balance_gdp_pct": 0,
              "debt_gdp_yoy_change_pp": 0,
          })
    check("methodology snapshot and source identities",
          method["historical_source"] == "C17"
          and method["yoy_method"] == "same_quarter_previous_year"
          and method["latest_snapshot"] == "latest_complete"
          and method["null_is_not_zero"]
          and method["no_forward_fiscal_gap"])
    forbidden = {"risk_score", "composite_score", "stress_score", "crisis_year",
                 "risk_probability", "risk_level"}
    check("output has no scoring fields", not any(
        key in forbidden for row in rows for key in row))

    committed_exists = OUTPUT_PATH.is_file()
    committed = read_json_or(str(OUTPUT_PATH), {}) if committed_exists else {}
    committed_strict = False
    try:
        assert_envelope(committed)
        committed_strict = True
    except ValueError:
        pass
    check("committed fiscal risk monitor exists", committed_exists)
    check("committed fiscal risk monitor is strict envelope", committed_strict)
    check("committed fiscal risk monitor identity", committed_strict
          and committed.get("source") == SOURCE and committed.get("freq") == "quarterly"
          and committed.get("date_field") == "date")
    check(STALE_MARKER, _comparable(committed) == _comparable(expected))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        input_path = root / "macro_fiscal_stress.json"
        output_path = root / "fiscal_risk_monitor.json"
        input_path.write_bytes(original_input)
        first = publish(build_output(input_path), output_path)
        first_bytes = output_path.read_bytes()
        second = publish(build_output(input_path), output_path)
        check("idempotent publish", first and not second
              and output_path.read_bytes() == first_bytes)
        old = output_path.read_bytes()
        bad = copy.deepcopy(source)
        bad["data"]["quarterly"][-2]["fiscal_gap_pct_gdp"] = None
        input_path.write_text(json.dumps(bad), encoding="utf-8")
        check("failed derivation preserves old output",
              run_once(input_path, output_path) == 1 and output_path.read_bytes() == old)

        input_path.write_bytes(original_input)
        stale = copy.deepcopy(source)
        stale["data"]["quarterly"][-2]["net_interest_receipts_pct"] += 0.001
        input_path.write_text(json.dumps(stale), encoding="utf-8")
        check("valid source change makes old committed basis stale",
              _comparable(json.loads(old)) != _comparable(build_output(input_path)))

    check("production source unchanged", INPUT_PATH.read_bytes() == original_input)
    workflow = (ROOT / ".github" / "workflows" / "update-cot.yml").read_text(
        encoding="utf-8")
    check("workflow runs fiscal risk monitor test once",
          workflow.count("python derive_fiscal_risk_monitor.py --test") == 1)
    check("workflow runs fiscal risk monitor production derivation once",
          workflow.count("python derive_fiscal_risk_monitor.py\n") == 1)
    check("workflow commits fiscal risk monitor output",
          "data/derived/fiscal_risk_monitor.json" in workflow)

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
