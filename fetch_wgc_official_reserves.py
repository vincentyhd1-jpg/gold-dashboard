#!/usr/bin/env python3
"""Fetch WGC quarterly official-gold and total-reserves reporting aggregates."""
from __future__ import annotations

import json
import math
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from data_envelope import assert_envelope, envelope
from io_utils import atomic_write_json, quarantine_write, read_json_or, sweep_stale_tmp

ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "data" / "wgc_official_reserves.json"
QUAR_DIR = ROOT / "data" / "quarantine"
SOURCE = "world_gold_council_official_reserves"
API_URL = (
    "https://fsapi.gold.org/api/cbd/v11/charts/getPage"
    "?page=date_range&periodicity=QTD_FULL"
    "&startDate=2000-03-31&endDate=2099-12-31"
)
MIN_MATCHED_REPORTING_ENTITIES = 90
METRICS = (
    "gold_reserves", "gold_reserves_tns", "total_reserves",
)
AGGREGATE_OR_INSTITUTION_NAMES = {
    "WLD", "WORLD", "GLOBAL", "EUR", "EUU", "EMU", "IMF", "ECB", "BIS",
    "G7", "G20",
}
ENTITY_NAME_RE = re.compile(r"^[A-Z]{3}$")


class NetworkFailure(RuntimeError):
    pass


class SourceFailure(RuntimeError):
    def __init__(self, message: str, *, payload=None, raw: bytes = b""):
        super().__init__(message)
        self.payload = payload
        self.raw = raw


def fetch_payload(*, opener=urlopen) -> tuple[dict, bytes]:
    request = Request(API_URL, headers={
        "Accept": "application/json",
        "User-Agent": "gold-dashboard/wgc-official-reserves",
    })
    try:
        with opener(request, timeout=60) as response:
            status = getattr(response, "status", response.getcode())
            raw = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise NetworkFailure(f"WGC download failed: {exc}") from exc
    if status != 200:
        raise NetworkFailure(f"WGC HTTP {status}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceFailure("WGC response is not valid JSON", raw=raw) from exc
    if not isinstance(payload, dict):
        raise SourceFailure("WGC response must be an object", payload=payload, raw=raw)
    return payload, raw


def _metric_series(payload: dict, metric: str) -> list[dict]:
    try:
        series = payload["chartData"]["linechart"]["QTD_FULL"][metric]["data"]
    except (KeyError, TypeError) as exc:
        raise SourceFailure(f"WGC metric missing: {metric}") from exc
    if not isinstance(series, list) or not series:
        raise SourceFailure(f"WGC metric empty: {metric}")
    return series


def _metric_entities(payload: dict, metric: str) -> dict[str, dict[int, float]]:
    entities: dict[str, dict[int, float]] = {}
    seen_names: set[str] = set()
    for series in _metric_series(payload, metric):
        if not isinstance(series, dict) or not isinstance(series.get("name"), str):
            raise SourceFailure(f"WGC {metric} series identity is invalid")
        name = series["name"]
        if name in seen_names:
            raise SourceFailure(f"WGC {metric} duplicate entity: {name}")
        if (not ENTITY_NAME_RE.fullmatch(name)
                or name in AGGREGATE_OR_INSTITUTION_NAMES):
            raise SourceFailure(
                f"WGC {metric} aggregate/institution series is not allowed: {name}")
        seen_names.add(name)
        points = series.get("data")
        if not isinstance(points, list):
            raise SourceFailure(f"WGC {metric}/{name} data is invalid")
        entity_values: dict[int, float] = {}
        seen_dates: set[int] = set()
        for point in points:
            if not isinstance(point, list) or len(point) != 2:
                raise SourceFailure(f"WGC {metric}/{name} point is invalid")
            timestamp, value = point
            if not isinstance(timestamp, int) or timestamp in seen_dates:
                raise SourceFailure(f"WGC {metric}/{name} timestamp is invalid")
            seen_dates.add(timestamp)
            if value is None:
                continue
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value < 0):
                raise SourceFailure(f"WGC {metric}/{name} value is invalid")
            entity_values[timestamp] = float(value)
        entities[name] = entity_values
    return entities


def _quarter_end(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    if (value.month, value.day) not in ((3, 31), (6, 30), (9, 30), (12, 31)):
        raise SourceFailure(f"WGC timestamp is not quarter-end: {value.date()}")
    return value.date().isoformat()


def parse_payload(payload: dict) -> list[dict]:
    metrics = {metric: _metric_entities(payload, metric) for metric in METRICS}
    catalogs = {metric: set(values) for metric, values in metrics.items()}
    if len({frozenset(names) for names in catalogs.values()}) != 1:
        raise SourceFailure("WGC metric entity catalogs differ")
    entity_catalog = sorted(next(iter(catalogs.values())))
    common = sorted(set.intersection(*(
        {timestamp for values in metric.values() for timestamp in values}
        for metric in metrics.values()
    )))
    observations: list[dict] = []
    for timestamp in common:
        reporting = {
            metric: sorted(name for name in entity_catalog
                           if timestamp in metrics[metric][name])
            for metric in METRICS
        }
        matched = sorted(set.intersection(*(
            set(reporting[metric]) for metric in METRICS
        )))
        if len(matched) < MIN_MATCHED_REPORTING_ENTITIES:
            continue
        values = {
            metric: sum(metrics[metric][name][timestamp] for name in matched)
            for metric in METRICS
        }
        observations.append({
            "date": _quarter_end(timestamp),
            "official_gold_value_usd_mn": round(values["gold_reserves"], 2),
            "official_gold_tonnes": round(values["gold_reserves_tns"], 2),
            "total_official_reserve_assets_usd_mn": round(
                values["total_reserves"], 2),
            "gold_reporting_entities_count": len(reporting["gold_reserves"]),
            "gold_tonnes_reporting_entities_count": len(reporting["gold_reserves_tns"]),
            "total_reserves_reporting_entities_count": len(reporting["total_reserves"]),
            "matched_reporting_entities_count": len(matched),
            "reporting_entities": {
                "gold_reserves": reporting["gold_reserves"],
                "gold_reserves_tonnes": reporting["gold_reserves_tns"],
                "total_reserves": reporting["total_reserves"],
                "matched": matched,
            },
        })
    if not observations:
        raise SourceFailure("WGC has no complete quarterly observations")
    dates = [row["date"] for row in observations]
    if dates != sorted(set(dates)):
        raise SourceFailure("WGC quarterly dates are not unique and chronological")
    return observations


def build_output(payload: dict) -> dict:
    observations = parse_payload(payload)
    dates = [row["date"] for row in observations]
    return envelope(
        SOURCE, "quarterly", observations, dates=dates, date_field="date",
        info=[
            "source=World_Gold_Council_Central_Bank_Dashboard",
            "gold_value_metric=Gold_reserves_USD_millions",
            "gold_tonnes_metric=Gold_reserves_tonnes",
            "denominator_metric=Total_reserves_USD_millions",
            "total_reserves_source_semantics=IMF_IFS_compatible_including_gold",
            f"minimum_matched_reporting_entities={MIN_MATCHED_REPORTING_ENTITIES}",
            "source_series_identity=ISO3_country_or_economy_codes_only",
            "source_provided_world_global_aggregate=false",
            "world_region_institution_aggregate_series=none_present_and_forbidden",
            "aggregation=quarter_specific_same_entity_intersection_no_fill_no_interpolation",
            "denominator_scope=matched_WGC_reporting_entity_sample_not_global_total",
            f"source_url={API_URL}",
        ],
    )


def _comparable(payload: dict) -> tuple:
    return tuple(payload.get(key) for key in (
        "source", "freq", "date_field", "coverage", "warnings", "info", "data",
    ))


def publish(output: dict, path: Path = OUTPUT_PATH) -> bool:
    assert_envelope(output)
    existing = read_json_or(str(path), None)
    if existing is not None:
        assert_envelope(existing)
        if _comparable(existing) == _comparable(output):
            return False
    atomic_write_json(str(path), output, compact=False)
    return True


def _quarantine(reason: str, payload, raw: bytes, quarantine_dir: Path) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine_write(str(quarantine_dir), "wgc-official-reserves", stamp,
        reason=[reason], payload={"source": SOURCE, "response": payload},
        raw=raw, raw_ext="raw.json")


def run_once(*, output_path: Path = OUTPUT_PATH, quarantine_dir: Path = QUAR_DIR,
             fetcher=fetch_payload) -> int:
    sweep_stale_tmp(str(output_path))
    try:
        payload, raw = fetcher()
    except NetworkFailure as exc:
        print(f"WGC official reserves fetch failed: {exc}", file=sys.stderr)
        return 2
    except SourceFailure as exc:
        _quarantine(str(exc), exc.payload, exc.raw, quarantine_dir)
        print(f"WGC official reserves validation failed: {exc}", file=sys.stderr)
        return 1
    try:
        publish(build_output(payload), output_path)
    except SourceFailure as exc:
        _quarantine(str(exc), payload, raw, quarantine_dir)
        print(f"WGC official reserves validation failed: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"WGC official reserves write failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _fixture() -> dict:
    dates = [978220800000, 985996800000]
    base_names = [f"{chr(65 + i // 26)}{chr(65 + (i // 26) % 26)}{chr(65 + i % 26)}"
                  for i in range(MIN_MATCHED_REPORTING_ENTITIES)]
    names = base_names + ["ZZG", "ZZR"]
    def metric(values, missing_name):
        return {"data": [
            {"name": name, "data": [
                [dates[0], None if name == missing_name else values[0]],
                [dates[1], None if name == missing_name else values[1]],
            ]}
            for name in names
        ]}
    return {"chartData": {"linechart": {"QTD_FULL": {
        "gold_reserves": metric((10.0, 11.0), "ZZR"),
        "gold_reserves_tns": metric((1.0, 1.0), "ZZR"),
        "total_reserves": metric((100.0, 110.0), "ZZG"),
    }}}}


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

    fixture = _fixture()
    rows = parse_payload(fixture)
    check("quarter-end observations preserved", [r["date"] for r in rows] == ["2000-12-31", "2001-03-31"])
    check("gold values aggregate exactly", rows[0]["official_gold_value_usd_mn"] == 900.0)
    check("total reserves aggregate exactly", rows[0]["total_official_reserve_assets_usd_mn"] == 9000.0)
    check("different reporter counts use the same entity intersection",
          rows[0]["gold_reporting_entities_count"] == MIN_MATCHED_REPORTING_ENTITIES + 1
          and rows[0]["total_reserves_reporting_entities_count"] == MIN_MATCHED_REPORTING_ENTITIES + 1
          and rows[0]["matched_reporting_entities_count"] == MIN_MATCHED_REPORTING_ENTITIES)
    check("equal counts with different entity names cannot masquerade as a match",
          "ZZG" not in rows[0]["reporting_entities"]["matched"]
          and "ZZR" not in rows[0]["reporting_entities"]["matched"]
          and set(rows[0]["reporting_entities"]["matched"])
          == (set(rows[0]["reporting_entities"]["gold_reserves"])
              & set(rows[0]["reporting_entities"]["gold_reserves_tonnes"])
              & set(rows[0]["reporting_entities"]["total_reserves"])))
    short = _fixture()
    short["chartData"]["linechart"]["QTD_FULL"]["total_reserves"]["data"][0]["data"] = [
        [dates, None] for dates, _value in short["chartData"]["linechart"]["QTD_FULL"]["total_reserves"]["data"][0]["data"]
    ]
    try:
        parse_payload(short)
    except SourceFailure:
        check("incomplete coverage is rejected", True)
    else:
        check("incomplete coverage is rejected", False)
    output = build_output(fixture)
    check("strict envelope", output["source"] == SOURCE and output["freq"] == "quarterly")
    check("IFS-compatible denominator metadata", any("IMF_IFS" in x for x in output["info"]))
    check("dynamic sample is not mislabeled as a global aggregate",
          "source_provided_world_global_aggregate=false" in output["info"]
          and "denominator_scope=matched_WGC_reporting_entity_sample_not_global_total" in output["info"])
    aggregate = _fixture()
    for metric in METRICS:
        aggregate["chartData"]["linechart"]["QTD_FULL"][metric]["data"].append({
            "name": "WLD", "data": [[978220800000, 1.0], [985996800000, 1.0]],
        })
    try:
        parse_payload(aggregate)
    except SourceFailure as exc:
        check("World/region aggregate cannot be summed with member economies",
              "aggregate/institution series is not allowed" in str(exc))
    else:
        check("World/region aggregate cannot be summed with member economies", False)
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "out.json"
        check("first publish writes", publish(output, path))
        check("idempotent publish skips", not publish(build_output(fixture), path))
        original = path.read_bytes()
        quarantine = Path(td) / "quarantine"
        bad = _fixture()
        bad["chartData"]["linechart"]["QTD_FULL"].pop("total_reserves")
        code = run_once(output_path=path, quarantine_dir=quarantine,
            fetcher=lambda: (bad, b'{"bad":"wgc"}'))
        evidence = list(quarantine.glob("wgc-official-reserves-*"))
        check("bad WGC response is quarantined and old file retained", code == 1
            and path.read_bytes() == original
            and len([p for p in evidence if p.name.endswith(".raw.json")]) == 1
            and len([p for p in evidence if p.name.endswith(".json")
                     and not p.name.endswith(".raw.json")]) == 1)
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_tests() if "--test" in sys.argv else run_once())
