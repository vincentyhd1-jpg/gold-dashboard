#!/usr/bin/env python3
"""Fetch the first FRED macro batch into independent envelope files."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from data_envelope import envelope
from io_utils import atomic_write_json, quarantine_write, read_json_or, sweep_stale_tmp

API_URL = "https://api.stlouisfed.org/fred/series/observations"
ROOT = Path(__file__).resolve().parent
QUAR_DIR = ROOT / "data" / "quarantine"
OBS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SERIES = (
    ("DGS2", "data/ust_dgs2.json", "daily", "rate"),
    ("DGS10", "data/ust_dgs10.json", "daily", "rate"),
    ("DGS30", "data/ust_dgs30.json", "daily", "rate"),
    ("DFEDTARU", "data/fed_target_upper.json", "daily", "rate"),
    ("DFEDTARL", "data/fed_target_lower.json", "daily", "rate"),
    ("DFF", "data/fed_effective_rate.json", "daily", "rate"),
    ("CPIAUCSL", "data/cpi_cpiaucsl.json", "monthly", "cpi"),
    ("CPILFESL", "data/cpi_cpilfesl.json", "monthly", "cpi"),
)


class FetchFailure(Exception):
    pass


class FormatFailure(Exception):
    pass


class ParseFailure(Exception):
    pass


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _key() -> str:
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise FetchFailure("FRED_API_KEY 缺失")
    return key


def fetch_payload(series_id: str, *, opener=urlopen) -> tuple[dict, bytes]:
    key = _key()
    query = (
        f"{API_URL}?series_id={series_id}&api_key={key}"
        "&file_type=json&sort_order=asc&observation_start=2016-01-01"
    )
    req = Request(query, headers={"Accept": "application/json", "User-Agent": "gold-dashboard/fred"})
    try:
        with opener(req, timeout=30) as response:
            status = getattr(response, "status", response.getcode())
            raw = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise FetchFailure(f"FRED {series_id} 下载失败: {exc}") from exc
    if status != 200:
        raise FetchFailure(f"FRED {series_id} HTTP {status}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormatFailure(f"FRED {series_id} 非 JSON 响应") from exc
    if not isinstance(payload, dict):
        raise FormatFailure(f"FRED {series_id} 顶层格式非法")
    if not isinstance(payload.get("observations"), list):
        raise FormatFailure(f"FRED {series_id} observations 格式非法")
    return payload, raw


def parse_observations(payload: dict, series_id: str) -> list[dict]:
    points: list[dict] = []
    seen: set[str] = set()
    for row in payload["observations"]:
        if not isinstance(row, dict):
            raise ParseFailure(f"{series_id}: observation 非对象")
        date = row.get("date")
        value = row.get("value")
        if not isinstance(date, str) or not OBS_RE.fullmatch(date):
            raise ParseFailure(f"{series_id}: date 格式非法: {date!r}")
        if date in seen:
            raise ParseFailure(f"{series_id}: 日期重复: {date}")
        seen.add(date)
        if value == ".":
            continue
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ParseFailure(f"{series_id}: value 非数字: {value!r}") from exc
        if not math.isfinite(number):
            raise ParseFailure(f"{series_id}: value 非有限数字: {value!r}")
        points.append({"date": date, "value": number})
    return points


def _old_data(path: Path) -> dict | None:
    old = read_json_or(str(path), None)
    if not isinstance(old, dict) or not isinstance(old.get("data"), list):
        return None
    return old


def _point_text(point: dict) -> str:
    return f"{point['date']}={point['value']:g}"


def build_info(series_id: str, points: list[dict], old: dict | None) -> list[str]:
    latest = points[-12:]
    info = [
        "source=FRED",
        f"series_id={series_id}",
        "api_path=/fred/series/observations",
        "latest_observations=[" + ",".join(_point_text(p) for p in latest) + "]",
    ]
    if old:
        old_tail = {p.get("date"): p.get("value") for p in old["data"][-12:] if isinstance(p, dict)}
        revisions = []
        for point in latest:
            if point["date"] in old_tail and old_tail[point["date"]] != point["value"]:
                revisions.append(f"{point['date']}:{old_tail[point['date']]}->{point['value']}")
        if revisions:
            info.append("revisions=[" + ",".join(revisions) + "]")
    return info


def validate_points(points: list[dict], kind: str) -> list[str]:
    failures = []
    if kind == "cpi" and any(p["value"] <= 0 for p in points):
        failures.append("CPI 指数 ≤ 0")
    return failures


def warnings_for(points: list[dict], freq: str, kind: str) -> list[str]:
    warnings = []
    if not points:
        warnings.append("有效观测点数为 0")
    if kind == "rate" and any(abs(p["value"]) > 100 for p in points):
        warnings.append("利率值域超出提示范围")
    if points:
        last = datetime.strptime(points[-1]["date"], "%Y-%m-%d").date()
        age = (datetime.now(timezone.utc).date() - last).days
        if age > (10 if freq == "daily" else 100):
            warnings.append(f"最新观测滞后 {age} 天")
    return warnings


def failure_envelope(series_id: str, freq: str, reason: list[str], info: list[str] | None = None) -> dict:
    payload = envelope("FRED", freq, None, dates=[], warnings=reason, info=info or [f"series_id={series_id}"])
    payload["coverage"] = None
    return payload


def stable_payload(payload):
    clone = json.loads(json.dumps(payload, ensure_ascii=False))
    if isinstance(clone, dict):
        clone.pop("generated_at", None)
    return clone


def save_quarantine(series_id: str, reason: list[str], payload: dict, raw: bytes, quarantine_dir: Path) -> None:
    quarantine_write(
        str(quarantine_dir), series_id.lower(), utc_stamp(),
        reason=reason, payload={"source_payload": payload}, raw=raw, raw_ext="raw.json",
    )


def process_one(series_id: str, relpath: str, freq: str, kind: str, *, fetcher=fetch_payload, base_dir: Path | None = None, quarantine_dir: Path | None = None) -> tuple[str, int, str]:
    base = Path(base_dir) if base_dir is not None else ROOT
    path = base / relpath
    quarantine = Path(quarantine_dir) if quarantine_dir is not None else base / "data" / "quarantine"
    sweep_stale_tmp(str(path))
    try:
        payload, raw = fetcher(series_id)
    except FetchFailure as exc:
        print(f"{series_id}: a/b exit 2: {exc}")
        return "download", 2, ""
    except FormatFailure as exc:
        print(f"{series_id}: b exit 2: {exc}")
        return "format", 2, ""
    try:
        points = parse_observations(payload, series_id)
    except ParseFailure as exc:
        save_quarantine(series_id, [str(exc)], payload, raw, quarantine)
        print(f"{series_id}: c exit 1: {exc}")
        return "parse", 1, ""
    failures = validate_points(points, kind)
    if failures:
        info = build_info(series_id, points, _old_data(path))
        save_quarantine(series_id, failures, payload, raw, quarantine)
        atomic_write_json(str(path), failure_envelope(series_id, freq, failures, info), compact=False)
        print(f"{series_id}: d exit 1: {', '.join(failures)}")
        return "validation", 1, ""
    old = _old_data(path)
    info = build_info(series_id, points, old)
    output = envelope(
        "FRED",
        freq,
        points,
        dates=[p["date"] for p in points],
        info=info,
        warnings=warnings_for(points, freq, kind),
    )
    if stable_payload(old or {}) == stable_payload(output):
        print(f"{series_id}: unchanged, skip write")
    else:
        atomic_write_json(str(path), output, compact=False)
        print(f"{series_id}: wrote count={len(points)} last={points[-1]['date'] if points else None} freq={freq}")
    return "ok", 0, (points[-1]["date"] if points else "")


def run_tests() -> None:
    checks = 0
    failures = 0

    def check(name, condition):
        nonlocal checks, failures
        checks += 1
        if condition:
            print(f"PASS {name}")
        else:
            failures += 1
            print(f"FAIL {name}")

    def file_hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    class Resp:
        def __init__(self, body, status=200):
            self.body = body
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.body

        def getcode(self):
            return self.status

    def opener_ok(req, timeout=30):
        body = {
            "observations": [
                {"date": "2026-01-01", "value": "1.25"},
                {"date": "2026-01-02", "value": "."},
                {"date": "2026-01-03", "value": "1.25"},
            ]
        }
        return Resp(json.dumps(body).encode())

    def seed_target(tmp_root: Path, relpath: str) -> Path:
        path = tmp_root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(envelope("FRED", "daily", [], dates=["2026-01-01"], info=["seed"]), ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def assert_hash_unchanged(label: str, fetcher, *, base_dir: Path, target_relpath: str, series_id: str = "DGS2", freq: str = "daily", kind: str = "rate", quarantine_dir: Path | None = None):
        path = seed_target(base_dir, target_relpath)
        before = file_hash(path)
        result_kind, code, _ = process_one(
            series_id,
            target_relpath,
            freq,
            kind,
            fetcher=fetcher,
            base_dir=base_dir,
            quarantine_dir=quarantine_dir or (base_dir / "quarantine"),
        )
        after = file_hash(path)
        check(label, code == 2 and before == after and result_kind in {"download", "format"})

    old_key = os.environ.get("FRED_API_KEY")
    os.environ["FRED_API_KEY"] = "TEST_KEY"
    try:
        payload, raw = fetch_payload("DGS2", opener=opener_ok)
        check("a/b 正常 JSON 请求可解析", payload["observations"] and raw)
        check(
            ". 跳过且持平保留",
            parse_observations(payload, "DGS2") == [
                {"date": "2026-01-01", "value": 1.25},
                {"date": "2026-01-03", "value": 1.25},
            ],
        )
        check("非法日期归 c", _classify_parse({"observations": [{"date": "bad", "value": "1"}]}) == "c")
        check("非数字归 c", _classify_parse({"observations": [{"date": "2026-01-01", "value": "x"}]}) == "c")
        check(
            "重复日期归 c",
            _classify_parse(
                {
                    "observations": [
                        {"date": "2026-01-01", "value": "1"},
                        {"date": "2026-01-01", "value": "2"},
                    ]
                }
            )
            == "c",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            tmp_quar = tmp_root / "quarantine"

            def d_fetch(series_id):
                raw = b'{"observations":[{"date":"2026-01-01","value":"0"}]}'
                return ({"observations": [{"date": "2026-01-01", "value": "0"}]}, raw)

            kind, code, _ = process_one(
                "CPIAUCSL",
                "data/cpi_cpiaucsl.json",
                "monthly",
                "cpi",
                fetcher=d_fetch,
                base_dir=tmp_root,
                quarantine_dir=tmp_quar,
            )
            failure_file = json.loads((tmp_root / "data" / "cpi_cpiaucsl.json").read_text(encoding="utf-8"))
            check(
                "CPI 非正值归 d 且 d mock 只写 tmpdir",
                kind == "validation"
                and code == 1
                and failure_file["data"] is None
                and failure_file["coverage"] is None
                and any(tmp_quar.glob("cpiaucsl-*.json")),
            )
        check(
            "利率值域仅 warning",
            bool(warnings_for([{"date": "2026-01-01", "value": 101}], "daily", "rate"))
            and not validate_points([{"date": "2026-01-01", "value": 101}], "rate"),
        )
        latest_info = build_info(
            "DGS2",
            [{"date": f"2026-01-{i:02d}", "value": i} for i in range(1, 15)],
            None,
        )[3]
        check("最新 12 点 info", latest_info.count("2026-") == 12)
        check(
            "revision info",
            "revisions=" in " ".join(
                build_info(
                    "DGS2",
                    [{"date": "2026-01-01", "value": 2}],
                    {"data": [{"date": "2026-01-01", "value": 1}]},
                )
            ),
        )
        check("失败 envelope data:null", failure_envelope("CPIAUCSL", "monthly", ["x"])["data"] is None)
        check("失败 envelope coverage:null", failure_envelope("CPIAUCSL", "monthly", ["x"])["coverage"] is None)
        check("8 个序列配置", len(SERIES) == 8)
        check("CPI 月频", all(x[2] == "monthly" for x in SERIES[-2:]))
        check("利率日频", all(x[2] == "daily" for x in SERIES[:-2]))

        def missing_key_fetch(series_id):
            os.environ.pop("FRED_API_KEY", None)
            return fetch_payload(series_id)

        def html_fetch(series_id):
            return fetch_payload(series_id, opener=lambda req, timeout=30: Resp(b"<html>bad</html>"))

        def no_observations_fetch(series_id):
            return fetch_payload(series_id, opener=lambda req, timeout=30: Resp(b"{}"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assert_hash_unchanged(
                "a 缺 key exit 2 且 hash 不变",
                missing_key_fetch,
                base_dir=root,
                target_relpath="data/ust_dgs2.json",
            )
        os.environ["FRED_API_KEY"] = "TEST_KEY"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assert_hash_unchanged(
                "b HTML/非 JSON exit 2 且 hash 不变",
                html_fetch,
                base_dir=root,
                target_relpath="data/ust_dgs2.json",
            )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assert_hash_unchanged(
                "b 顶层缺 observations exit 2 且 hash 不变",
                no_observations_fetch,
                base_dir=root,
                target_relpath="data/ust_dgs2.json",
            )
    finally:
        if old_key is None:
            os.environ.pop("FRED_API_KEY", None)
        else:
            os.environ["FRED_API_KEY"] = old_key

    print(f"{checks} passed, {failures} failed")
    if failures:
        raise SystemExit(1)

def _classify_parse(payload: dict) -> str:
    try:
        parse_observations(payload, "TEST")
    except ParseFailure:
        return "c"
    return "ok"


def main() -> int:
    if "--test" in sys.argv:
        run_tests()
        return 0
    result = 0
    for config in SERIES:
        _, code, _ = process_one(*config)
        result = max(result, code)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
