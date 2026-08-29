#!/usr/bin/env python3
"""Derive a weekly, exact-date Global Gold Value vs U.S. Debt comparison."""
from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path

from data_envelope import assert_envelope, envelope, upstream_ref
from io_utils import atomic_write_json, read_json_or, sweep_stale_tmp

ROOT = Path(__file__).resolve().parent
GOLD_PATH = ROOT / "data" / "gold_price.json"
DEBT_PATH = ROOT / "data" / "treasury_debt_daily.json"
OUTPUT_PATH = ROOT / "data" / "derived" / "gold_vs_debt.json"

SOURCE = "derived_global_gold_value_vs_us_debt"
GOLD_SOURCE = "gold_price"
DEBT_SOURCE = "treasury_fiscal_data_debt_to_penny"
GOLD_STOCK_TONNES = 220_700.0
GOLD_STOCK_VINTAGE = "end-2025"
GOLD_STOCK_SOURCE_URL = "https://www.gold.org/goldhub/data/how-much-gold"
TROY_OZ_PER_METRIC_TONNE = 32_150.74656862798
STALE_MARKER = "committed gold-vs-debt output is stale relative to current sources"
PRICE_SOURCE_PREFIX = "price_source="


class GoldDebtFailure(RuntimeError):
    """The two production sources cannot safely support this comparison."""


def _load(path: Path, expected_source: str, expected_freq: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoldDebtFailure(f"cannot read {path.name}: {exc}") from exc
    try:
        assert_envelope(payload)
    except ValueError as exc:
        raise GoldDebtFailure(f"{path.name} is not a strict envelope: {exc}") from exc
    if payload.get("source") != expected_source:
        raise GoldDebtFailure(f"{path.name} source mismatch")
    if payload.get("freq") != expected_freq or payload.get("date_field") != "date":
        raise GoldDebtFailure(f"{path.name} must be {expected_freq}/date")
    if not isinstance(payload.get("data"), list) or not payload["data"]:
        raise GoldDebtFailure(f"{path.name} data is empty")
    return payload


def _finite_positive(value, field: str) -> float:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value <= 0):
        raise GoldDebtFailure(f"{field} must be finite and positive")
    return float(value)


def _gold_price_proxy(payload: dict) -> dict:
    """Return an explicit valuation-proxy contract from upstream metadata."""
    if not isinstance(payload, dict):
        raise GoldDebtFailure("gold_price must be an envelope object")
    info = payload.get("info")
    if not isinstance(info, list) or not all(isinstance(item, str) for item in info):
        raise GoldDebtFailure("gold_price info must be a list of strings")
    sources = [item[len(PRICE_SOURCE_PREFIX):].strip() for item in info
               if item.startswith(PRICE_SOURCE_PREFIX)]
    if len(sources) != 1 or not sources[0]:
        raise GoldDebtFailure("gold_price requires exactly one recognizable price_source")
    source = sources[0]
    normalized = source.lower()
    if "yahoo finance" in normalized and "gc=f" in normalized:
        instrument = "GC=F"
        instrument_label = "COMEX gold futures"
    elif "stooq" in normalized and "xauusd" in normalized:
        instrument = "XAUUSD"
        instrument_label = "XAUUSD gold spot proxy"
    else:
        raise GoldDebtFailure(f"unrecognized gold price_source: {source}")
    return {
        "gold_price_source": source,
        "gold_price_instrument": instrument,
        "gold_price_instrument_label": instrument_label,
        "gold_price_is_proxy": True,
    }


def _validated_rows(payload: dict, value_field: str,
                    allow_null: bool = False) -> list[dict]:
    rows = payload["data"]
    previous = None
    seen: set[str] = set()
    validated = []
    for row in rows:
        if not isinstance(row, dict):
            raise GoldDebtFailure("source row must be an object")
        date = row.get("date")
        if not isinstance(date, str) or len(date) != 10:
            raise GoldDebtFailure(f"invalid observation date: {date!r}")
        if date in seen or (previous is not None and date <= previous):
            raise GoldDebtFailure("source dates must be unique and chronological")
        raw_value = row.get(value_field)
        value = (None if allow_null and raw_value is None
                 else _finite_positive(raw_value, f"{date} {value_field}"))
        validated.append({"date": date, value_field: value})
        seen.add(date)
        previous = date
    return validated


def build_data(gold_payload: dict, debt_payload: dict) -> dict:
    price_proxy = _gold_price_proxy(gold_payload)
    gold_rows = _validated_rows(gold_payload, "price", allow_null=True)
    debt_rows = _validated_rows(debt_payload, "total_bn")
    debt_by_date = {row["date"]: row["total_bn"] for row in debt_rows}

    observations = []
    for row in gold_rows:
        date = row["date"]
        price = row["price"]
        debt_bn = debt_by_date.get(date)
        observations.append({
            "date": date,
            "gold_price_usd_oz": price,
            "global_gold_value_usd_tn": (None if price is None else
                GOLD_STOCK_TONNES * TROY_OZ_PER_METRIC_TONNE * price / 1e12),
            "us_total_public_debt_usd_tn": (
                None if debt_bn is None else debt_bn / 1000.0
            ),
        })

    return {
        "observations": observations,
        "methodology": {
            "global_gold_value_formula": (
                "fixed_above_ground_gold_stock_tonnes"
                " * troy_oz_per_metric_tonne * gold_price_usd_oz / 1e12"
            ),
            "gold_stock_tonnes": GOLD_STOCK_TONNES,
            "gold_stock_vintage": GOLD_STOCK_VINTAGE,
            "gold_stock_source_url": GOLD_STOCK_SOURCE_URL,
            "troy_oz_per_metric_tonne": TROY_OZ_PER_METRIC_TONNE,
            "gold_value_is_estimate": True,
            "gold_price_frequency": "weekly",
            **price_proxy,
            "debt_definition": "Total Public Debt Outstanding",
            "debt_source_field": "tot_pub_debt_out_amt",
            "debt_alignment": "exact_gold_observation_date_only",
            "no_forward_fill": True,
            "no_interpolation": True,
            "missing_exact_date_debt_remains_null": True,
        },
    }


def build_output(gold_path: Path = GOLD_PATH, debt_path: Path = DEBT_PATH) -> dict:
    gold = _load(gold_path, GOLD_SOURCE, "weekly")
    debt = _load(debt_path, DEBT_SOURCE, "daily")
    data = build_data(gold, debt)
    references = []
    for path, source in ((gold_path, GOLD_SOURCE), (debt_path, DEBT_SOURCE)):
        try:
            reference = upstream_ref(str(path), source)
        except ValueError as exc:
            raise GoldDebtFailure(f"invalid upstream ref for {path.name}: {exc}") from exc
        if reference:
            references.append(reference)
    dates = [row["date"] for row in data["observations"]]
    missing_debt = sum(
        row["us_total_public_debt_usd_tn"] is None
        for row in data["observations"]
    )
    return envelope(
        SOURCE, "weekly", data, dates=dates, date_field="date",
        derived_from=references,
        warnings=(
            [f"{missing_debt} weekly gold observations have no exact-date debt value"]
            if missing_debt else []
        ),
        info=[
            "global_gold_value_is_fixed_stock_estimate_not_daily_inventory_census",
            "gold_stock=end-2025_220700_metric_tonnes_world_gold_council",
            "debt=Total_Public_Debt_Outstanding",
            "mixed_frequency_alignment=weekly_gold_dates_exact_daily_debt_dates_only",
            "no_forward_fill_no_interpolation_null_is_not_zero",
            "units=USD_trillions",
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
            raise GoldDebtFailure(
                f"existing gold-vs-debt output is not a strict envelope: {exc}") from exc
        if _comparable(existing) == _comparable(output):
            return False
    writer(str(output_path), output, compact=False)
    return True


def run_once(gold_path: Path = GOLD_PATH, debt_path: Path = DEBT_PATH,
             output_path: Path = OUTPUT_PATH) -> int:
    sweep_stale_tmp(str(output_path))
    try:
        publish(build_output(gold_path, debt_path), output_path)
    except (GoldDebtFailure, ValueError, OSError) as exc:
        print(f"gold-vs-debt derivation failed: {exc}", file=sys.stderr)
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

    expected = build_output()
    strict = True
    try:
        assert_envelope(expected)
    except ValueError:
        strict = False
    check("strict output envelope", strict)
    check("output identity", expected["source"] == SOURCE
          and expected["freq"] == "weekly" and expected["date_field"] == "date")
    check("two strict upstream references", len(expected["derived_from"]) == 2
          and all(ref.get("envelope") is True for ref in expected["derived_from"]))
    observations = expected["data"]["observations"]
    source_gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))["data"]
    source_debt = json.loads(DEBT_PATH.read_text(encoding="utf-8"))["data"]
    source_debt_by_date = {row["date"]: row["total_bn"] for row in source_debt}
    check("one output observation per real weekly gold point",
          len(observations) == len(source_gold))
    check("coverage follows real weekly dates", expected["coverage"] == {
        "first": observations[0]["date"], "last": observations[-1]["date"],
        "count": len(observations)})
    check("gold formula uses fixed stock and exact unit conversion", all(
        row["global_gold_value_usd_tn"] is None
        if row["gold_price_usd_oz"] is None else abs(
            row["global_gold_value_usd_tn"] - (
                GOLD_STOCK_TONNES * TROY_OZ_PER_METRIC_TONNE
                * row["gold_price_usd_oz"] / 1e12)) < 1e-12
        for row in observations))
    check("debt uses exact Total Public Debt Outstanding source", all(
        row["us_total_public_debt_usd_tn"] == (
            source_debt_by_date[row["date"]] / 1000.0
            if row["date"] in source_debt_by_date else None)
        for row in observations))
    methodology = expected["data"]["methodology"]
    check("gold estimate is explicitly labeled", methodology["gold_value_is_estimate"] is True
          and methodology["gold_stock_vintage"] == GOLD_STOCK_VINTAGE)
    source_gold_payload = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    source_proxy = _gold_price_proxy(source_gold_payload)
    check("gold price source metadata propagates to methodology",
          all(methodology.get(key) == value for key, value in source_proxy.items()))
    check("gold price valuation is explicitly a proxy",
          methodology.get("gold_price_is_proxy") is True)
    if source_proxy["gold_price_instrument"] == "GC=F":
        check("GC=F is never represented as a spot-price claim",
              "spot" not in methodology.get("gold_price_instrument_label", "").lower())
    else:
        check("recognized non-GC instrument remains explicit",
              methodology.get("gold_price_instrument") == "XAUUSD")

    alternate_info = [item for item in source_gold_payload["info"]
                      if not item.startswith(PRICE_SOURCE_PREFIX)]
    if source_proxy["gold_price_instrument"] == "GC=F":
        alternate_info.insert(0, "price_source=Stooq (xauusd)")
        alternate_instrument = "XAUUSD"
    else:
        alternate_info.insert(0, "price_source=Yahoo Finance (GC=F)")
        alternate_instrument = "GC=F"
    alternate_gold = {**source_gold_payload, "info": alternate_info}
    alternate_methodology = build_data(
        alternate_gold, json.loads(DEBT_PATH.read_text(encoding="utf-8"))
    )["methodology"]
    check("fresh methodology follows upstream price_source switch",
          alternate_methodology["gold_price_instrument"] == alternate_instrument
          and alternate_methodology["gold_price_source"]
          != methodology["gold_price_source"]
          and alternate_methodology["gold_price_is_proxy"] is True)
    alternate_expected = {**expected, "data": {
        **expected["data"], "methodology": alternate_methodology}}
    check("price_source switch makes committed comparison stale",
          _comparable(alternate_expected) != _comparable(expected))
    check("debt definition is Total Public Debt Outstanding",
          methodology["debt_definition"] == "Total Public Debt Outstanding")
    check("alignment forbids fill", methodology["debt_alignment"]
          == "exact_gold_observation_date_only"
          and methodology["no_forward_fill"] is True
          and methodology["no_interpolation"] is True)

    fixture_gold = {
        **json.loads(GOLD_PATH.read_text(encoding="utf-8")),
        "data": [{"date": "2026-01-06", "price": 3000.0},
                 {"date": "2026-01-13", "price": 3100.0},
                 {"date": "2026-01-20", "price": None}],
    }
    fixture_debt = {
        **json.loads(DEBT_PATH.read_text(encoding="utf-8")),
        "data": [{"date": "2026-01-06", "total_bn": 38000.0},
                 {"date": "2026-01-12", "total_bn": 38100.0},
                 {"date": "2026-01-20", "total_bn": 38200.0}],
    }
    fixture = build_data(fixture_gold, fixture_debt)["observations"]
    check("exact-date debt retained", fixture[0]["us_total_public_debt_usd_tn"] == 38.0)
    check("missing exact-date debt remains null",
          fixture[1]["us_total_public_debt_usd_tn"] is None)
    check("prior-day debt is never forward-filled",
          fixture[1]["date"] == "2026-01-13"
          and fixture[1]["us_total_public_debt_usd_tn"] is None)
    check("missing gold remains null while exact-date debt stays observable",
          fixture[2]["gold_price_usd_oz"] is None
          and fixture[2]["global_gold_value_usd_tn"] is None
          and fixture[2]["us_total_public_debt_usd_tn"] == 38.2)

    bare_rejected = False
    try:
        build_data(fixture_gold["data"], fixture_debt)
    except (GoldDebtFailure, TypeError):
        bare_rejected = True
    check("bare source rejected", bare_rejected)

    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        output_path = temp / "gold_vs_debt.json"
        writes = 0

        def writer(path, payload, compact=False):
            nonlocal writes
            writes += 1
            atomic_write_json(path, payload, compact=compact)

        first = publish(expected, output_path, writer)
        first_bytes = output_path.read_bytes()
        second = publish(build_output(), output_path, writer)
        check("first publish writes", first is True and writes == 1)
        check("idempotent second publish skips write", second is False and writes == 1)
        check("idempotent publish preserves bytes", output_path.read_bytes() == first_bytes)

        bad_gold = temp / "bad_gold.json"
        bad_gold.write_text("[]", encoding="utf-8")
        old_bytes = output_path.read_bytes()
        code = run_once(bad_gold, DEBT_PATH, output_path)
        check("failed derive exits nonzero", code == 1)
        check("failed derive does not overwrite old output",
              output_path.read_bytes() == old_bytes)

    committed_exists = OUTPUT_PATH.exists()
    check("committed production output exists", committed_exists)
    if committed_exists:
        try:
            committed = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            assert_envelope(committed)
            committed_strict = (committed.get("source") == SOURCE
                                and committed.get("freq") == "weekly"
                                and committed.get("date_field") == "date")
        except (OSError, json.JSONDecodeError, ValueError):
            committed = {}
            committed_strict = False
        check("committed production output is strict", committed_strict)
        check(STALE_MARKER, _comparable(committed) == _comparable(expected))

    print(f"{passed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    if "--test" in sys.argv:
        run_tests()
    else:
        raise SystemExit(run_once())
