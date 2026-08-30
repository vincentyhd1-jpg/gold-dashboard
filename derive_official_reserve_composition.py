#!/usr/bin/env python3
"""Derive quarterly official gold and foreign-official Treasury composition."""
from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path

from data_envelope import assert_envelope, envelope, upstream_ref
from io_utils import atomic_write_json, read_json_or, sweep_stale_tmp

ROOT = Path(__file__).resolve().parent
GOLD_PATH = ROOT / "data" / "wgc_official_reserves.json"
UST_PATH = ROOT / "data" / "foreign_official_ust.json"
OUTPUT_PATH = ROOT / "data" / "derived" / "official_reserve_composition.json"
SOURCE = "derived_official_reserve_composition"
GOLD_SOURCE = "world_gold_council_official_reserves"
UST_SOURCE = "FRED"
STALE_MARKER = "committed official reserve composition is stale relative to current sources"


class DerivationFailure(RuntimeError):
    pass


def _load(path: Path, source: str, freq: str,
          required_info: tuple[str, ...] = ()) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DerivationFailure(f"cannot read {path.name}: {exc}") from exc
    try:
        assert_envelope(payload)
    except ValueError as exc:
        raise DerivationFailure(f"{path.name} is not a strict envelope: {exc}") from exc
    if payload.get("source") != source:
        raise DerivationFailure(f"{path.name} source mismatch")
    if payload.get("freq") != freq or payload.get("date_field") != "date":
        raise DerivationFailure(f"{path.name} frequency/date contract mismatch")
    if not isinstance(payload.get("data"), list) or not payload["data"]:
        raise DerivationFailure(f"{path.name} data is empty")
    info = payload.get("info")
    if not isinstance(info, list) or any(anchor not in info for anchor in required_info):
        raise DerivationFailure(f"{path.name} source metadata mismatch")
    return payload


def _positive(value, field: str) -> float:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value <= 0):
        raise DerivationFailure(f"{field} must be finite and positive")
    return float(value)


def _period(date: str) -> str:
    if not isinstance(date, str) or len(date) != 10:
        raise DerivationFailure(f"invalid date: {date!r}")
    try:
        year, month, day = (int(part) for part in date.split("-"))
    except (ValueError, TypeError) as exc:
        raise DerivationFailure(f"invalid date: {date!r}") from exc
    if month not in (3, 6, 9, 12):
        raise DerivationFailure(f"not a quarter-end month: {date}")
    if day not in (1, 30, 31):
        raise DerivationFailure(f"unsupported quarterly source date: {date}")
    return f"{year}-Q{month // 3}"


def _gold_rows(payload: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    previous = ""
    for row in payload["data"]:
        if not isinstance(row, dict):
            raise DerivationFailure("WGC row must be an object")
        date = row.get("date")
        period = _period(date)
        if date <= previous or period in result:
            raise DerivationFailure("WGC dates must be unique and chronological")
        reporting = row.get("reporting_entities")
        if not isinstance(reporting, dict):
            raise DerivationFailure(f"{period} reporting entity identity is missing")
        entity_lists: dict[str, list[str]] = {}
        for field in ("gold_reserves", "gold_reserves_tonnes", "total_reserves", "matched"):
            values = reporting.get(field)
            if (not isinstance(values, list) or not values
                    or values != sorted(set(values))
                    or any(not isinstance(value, str) for value in values)):
                raise DerivationFailure(f"{period} {field} reporting entities are invalid")
            entity_lists[field] = values
        expected_matched = sorted(
            set(entity_lists["gold_reserves"])
            & set(entity_lists["gold_reserves_tonnes"])
            & set(entity_lists["total_reserves"])
        )
        if entity_lists["matched"] != expected_matched:
            raise DerivationFailure(f"{period} matched reporting entity identities differ")
        expected_counts = {
            "gold_reporting_entities_count": len(entity_lists["gold_reserves"]),
            "gold_tonnes_reporting_entities_count": len(entity_lists["gold_reserves_tonnes"]),
            "total_reserves_reporting_entities_count": len(entity_lists["total_reserves"]),
            "matched_reporting_entities_count": len(entity_lists["matched"]),
        }
        if any(row.get(field) != count for field, count in expected_counts.items()):
            raise DerivationFailure(f"{period} reporting entity counts differ from identities")
        gold_mn = _positive(row.get("official_gold_value_usd_mn"), f"{period} gold")
        total_mn = _positive(
            row.get("total_official_reserve_assets_usd_mn"), f"{period} reserves")
        if gold_mn >= total_mn:
            raise DerivationFailure(f"{period} gold must be below total reserves")
        result[period] = {
            "date": date,
            "gold_mn": gold_mn,
            "total_mn": total_mn,
            "tonnes": _positive(row.get("official_gold_tonnes"), f"{period} tonnes"),
            "matched_entities": entity_lists["matched"],
            "reporting_counts": expected_counts,
        }
        previous = date
    return result


def _ust_rows(payload: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    previous = ""
    for row in payload["data"]:
        if not isinstance(row, dict):
            raise DerivationFailure("FRED row must be an object")
        date = row.get("date")
        if not isinstance(date, str) or date <= previous:
            raise DerivationFailure("FRED dates must be unique and chronological")
        month = int(date[5:7]) if len(date) == 10 and date[4] == "-" else 0
        previous = date
        if month not in (3, 6, 9, 12):
            continue
        period = _period(date)
        result[period] = {
            "date": date,
            "ust_mn": _positive(row.get("value"), f"{period} UST"),
        }
    return result


def build_data(gold: dict, ust: dict) -> dict:
    gold_by_period = _gold_rows(gold)
    ust_by_period = _ust_rows(ust)
    periods = sorted(set(gold_by_period) & set(ust_by_period))
    if not periods:
        raise DerivationFailure("no common quarterly observations")
    observations = []
    for period in periods:
        gold_row = gold_by_period[period]
        ust_row = ust_by_period[period]
        gold_usd = round(gold_row["gold_mn"] * 1_000_000)
        total_usd = round(gold_row["total_mn"] * 1_000_000)
        ust_usd = round(ust_row["ust_mn"] * 1_000_000)
        observations.append({
            "period": period,
            "official_gold_value_usd": gold_usd,
            "official_gold_share_pct": gold_usd / total_usd * 100.0,
            "foreign_official_ust_value_usd": ust_usd,
            "total_official_reserve_assets_usd": total_usd,
            "official_gold_tonnes": gold_row["tonnes"],
            "matched_reporting_entities_count": len(gold_row["matched_entities"]),
            "matched_reporting_entities": gold_row["matched_entities"],
            "gold_source_date": gold_row["date"],
            "ust_source_date": ust_row["date"],
        })
    return {
        "metadata": {
            "frequency": "quarterly",
            "period_alignment": "calendar_quarter",
            "as_of": periods[-1],
        },
        "methodology": {
            "gold_value": "WGC official gold reserves valued at quarter-end market price",
            "gold_share_formula": (
                "MatchedReportingEntitiesOfficialGoldValue / "
                "MatchedReportingEntitiesTotalReserves * 100"
            ),
            "ust_value": "TIC/FRED Foreign Official U.S. Treasury Holdings (FORTREASPOS99990)",
            "ust_ratio_status": "not_produced_unmatched_statistical_universe",
            "gold_share_denominator": "Matched WGC reporting-entity Total reserves",
            "denominator_source": (
                "WGC Total reserves, IMF IFS-compatible including gold, quarter-specific "
                "same-entity intersection"
            ),
            "denominator_source_provided_global_aggregate": False,
            "denominator_is_dynamic_available_country_sum": False,
            "denominator_scope": "quarter-specific matched WGC reporting-entity sample",
            "gold_numerator_denominator_same_entity_universe": True,
            "tic_numerator_denominator_same_entity_universe": False,
            "quarterly_ust_rule": "March/June/September/December observations only",
            "cofer_usd_share_is_not_ust_share": True,
            "no_forward_fill": True,
            "no_interpolation": True,
            "foreign_official_scope": (
                "TIC foreign official institutions may include central banks, government "
                "departments, stabilization funds, and international official institutions"
            ),
        },
        "sources": {
            "official_gold": "World Gold Council Central Bank Dashboard",
            "total_official_reserve_assets": (
                "WGC / IMF IFS-compatible Total reserves, matched reporting-entity sample"
            ),
            "foreign_official_ust": "TIC/FRED FORTREASPOS99990",
        },
        "observations": observations,
    }


def build_output(gold_path: Path = GOLD_PATH, ust_path: Path = UST_PATH) -> dict:
    gold = _load(gold_path, GOLD_SOURCE, "quarterly", (
        "source=World_Gold_Council_Central_Bank_Dashboard",
        "denominator_metric=Total_reserves_USD_millions",
        "total_reserves_source_semantics=IMF_IFS_compatible_including_gold",
        "source_provided_world_global_aggregate=false",
        "aggregation=quarter_specific_same_entity_intersection_no_fill_no_interpolation",
        "denominator_scope=matched_WGC_reporting_entity_sample_not_global_total",
    ))
    ust = _load(ust_path, UST_SOURCE, "monthly", (
        "source=FRED", "series_id=FORTREASPOS99990", "units=Millions of Dollars",
    ))
    data = build_data(gold, ust)
    refs = []
    for path, source in ((gold_path, GOLD_SOURCE), (ust_path, UST_SOURCE)):
        try:
            ref = upstream_ref(str(path), source)
        except ValueError as exc:
            raise DerivationFailure(f"invalid upstream ref for {path.name}: {exc}") from exc
        if ref:
            refs.append(ref)
    periods = [row["period"] for row in data["observations"]]
    return envelope(
        SOURCE, "quarterly", data, dates=periods, date_field="period",
        derived_from=refs,
        info=[
            "three_series=official_gold_value_and_sample_share,foreign_official_ust_value",
            "gold_share_denominator=matched_WGC_reporting_entity_Total_reserves",
            "denominator_is_not_source_provided_global_aggregate",
            "foreign_official_ust_ratio_not_produced_due_unmatched_statistical_universe",
            "gold_source=World_Gold_Council",
            "denominator_source=IMF_IFS_compatible_Total_reserves_matched_sample",
            "ust_source=TIC_FRED_FORTREASPOS99990",
            "units=USD_and_percent",
            "quarterly_exact_observations_only_no_forward_fill_no_interpolation",
            "COFER_USD_share_is_explicitly_not_used",
        ],
    )


def _comparable(payload: dict) -> tuple:
    return tuple(payload.get(key) for key in (
        "source", "freq", "date_field", "coverage", "derived_from",
        "warnings", "info", "data",
    ))


def publish(output: dict, output_path: Path = OUTPUT_PATH) -> bool:
    assert_envelope(output)
    existing = read_json_or(str(output_path), None)
    if existing is not None:
        assert_envelope(existing)
        if _comparable(existing) == _comparable(output):
            return False
    atomic_write_json(str(output_path), output, compact=False)
    return True


def run_once() -> int:
    sweep_stale_tmp(str(OUTPUT_PATH))
    try:
        publish(build_output())
    except (DerivationFailure, ValueError, OSError) as exc:
        print(f"official reserve composition derivation failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _fixture_payloads() -> tuple[dict, dict]:
    matched = ["AAA", "BBB", "CCC"]
    gold_entities = matched + ["GGG"]
    reserve_entities = matched + ["RRR"]
    entity_fields = {
        "gold_reporting_entities_count": len(gold_entities),
        "gold_tonnes_reporting_entities_count": len(gold_entities),
        "total_reserves_reporting_entities_count": len(reserve_entities),
        "matched_reporting_entities_count": len(matched),
        "reporting_entities": {
            "gold_reserves": gold_entities,
            "gold_reserves_tonnes": gold_entities,
            "total_reserves": reserve_entities,
            "matched": matched,
        },
    }
    gold_rows = [
        {"date": "2024-03-31", "official_gold_value_usd_mn": 2_000_000,
         "official_gold_tonnes": 32_000, "total_official_reserve_assets_usd_mn": 10_000_000,
         **entity_fields},
        {"date": "2024-06-30", "official_gold_value_usd_mn": 2_100_000,
         "official_gold_tonnes": 32_100, "total_official_reserve_assets_usd_mn": 10_500_000,
         **entity_fields},
    ]
    ust_rows = [
        {"date": "2024-02-01", "value": 3_000_000},
        {"date": "2024-03-01", "value": 3_100_000},
        {"date": "2024-04-01", "value": 9_999_999},
        {"date": "2024-06-01", "value": 3_200_000},
    ]
    gold = envelope(GOLD_SOURCE, "quarterly", gold_rows,
        dates=[r["date"] for r in gold_rows], info=[
            "source=World_Gold_Council_Central_Bank_Dashboard",
            "denominator_metric=Total_reserves_USD_millions",
            "total_reserves_source_semantics=IMF_IFS_compatible_including_gold",
            "source_provided_world_global_aggregate=false",
            "aggregation=quarter_specific_same_entity_intersection_no_fill_no_interpolation",
            "denominator_scope=matched_WGC_reporting_entity_sample_not_global_total",
        ])
    ust = envelope(UST_SOURCE, "monthly", ust_rows,
        dates=[r["date"] for r in ust_rows], info=[
            "source=FRED", "series_id=FORTREASPOS99990", "units=Millions of Dollars",
        ])
    return gold, ust


def run_tests() -> int:
    passed = failed = 0
    def check(name, condition):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  PASS {name}")
        else:
            failed += 1
            print(f"  FAIL {name}")

    gold, ust = _fixture_payloads()
    data = build_data(gold, ust)
    rows = data["observations"]
    check("common quarterly index", [r["period"] for r in rows] == ["2024-Q1", "2024-Q2"])
    check("quarter-end month UST only", rows[0]["foreign_official_ust_value_usd"] == 3_100_000_000_000)
    check("official gold value preserved", rows[0]["official_gold_value_usd"] == 2_000_000_000_000)
    check("matched sample denominator preserved",
          rows[0]["total_official_reserve_assets_usd"] == 10_000_000_000_000)
    check("gold share formula", abs(rows[0]["official_gold_share_pct"] - 20.0) < 1e-12)
    check("UST ratio omitted for unmatched statistical universe",
          "foreign_official_ust_share_pct" not in rows[0]
          and data["methodology"]["ust_ratio_status"]
          == "not_produced_unmatched_statistical_universe")
    check("gold numerator and denominator retain the same entity intersection",
          rows[0]["matched_reporting_entities"] == ["AAA", "BBB", "CCC"]
          and rows[0]["matched_reporting_entities_count"] == 3)
    check("no forward fill", [r["ust_source_date"] for r in rows] == ["2024-03-01", "2024-06-01"])
    check("COFER excluded", data["methodology"]["cofer_usd_share_is_not_ust_share"] is True)
    check("TIC/FRED source label", data["sources"]["foreign_official_ust"] == "TIC/FRED FORTREASPOS99990")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        gp, up, op = root / "gold.json", root / "ust.json", root / "out.json"
        gp.write_text(json.dumps(gold), encoding="utf-8")
        up.write_text(json.dumps(ust), encoding="utf-8")
        output = build_output(gp, up)
        check("strict quarterly output", output["source"] == SOURCE and output["date_field"] == "period")
        check("coverage follows common periods", output["coverage"] == {"first": "2024-Q1", "last": "2024-Q2", "count": 2})
        check("derived_from has both sources", [r["source"] for r in output["derived_from"]] == [GOLD_SOURCE, UST_SOURCE])
        check("first publish writes", publish(output, op))
        check("idempotent publish skips", not publish(build_output(gp, up), op))
    try:
        bad = json.loads(json.dumps(gold))
        bad["data"][0]["total_official_reserve_assets_usd_mn"] = 1
        build_data(bad, ust)
    except DerivationFailure:
        check("invalid denominator rejected", True)
    else:
        check("invalid denominator rejected", False)
    try:
        bad = json.loads(json.dumps(gold))
        bad["data"][0]["reporting_entities"]["matched"] = ["AAA", "BBB", "RRR"]
        build_data(bad, ust)
    except DerivationFailure as exc:
        check("equal reporter counts with different entity identities are rejected",
              "matched reporting entity identities differ" in str(exc))
    else:
        check("equal reporter counts with different entity identities are rejected", False)
    try:
        bad = json.loads(json.dumps(ust))
        bad["data"] = [row for row in bad["data"] if row["date"][5:7] not in ("03", "06", "09", "12")]
        build_data(gold, bad)
    except DerivationFailure:
        check("no fabricated quarter from non-quarter month", True)
    else:
        check("no fabricated quarter from non-quarter month", False)
    if OUTPUT_PATH.exists():
        try:
            committed = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            expected = build_output()
            check(STALE_MARKER, _comparable(committed) == _comparable(expected))
        except (OSError, json.JSONDecodeError, ValueError, DerivationFailure):
            check(STALE_MARKER, False)
    else:
        check(STALE_MARKER, False)
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_tests() if "--test" in sys.argv else run_once())
