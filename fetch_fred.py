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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from data_envelope import envelope
from io_utils import atomic_write_json, quarantine_write, read_json_or, sweep_stale_tmp

API_URL = "https://api.stlouisfed.org/fred/series/observations"
ROOT = Path(__file__).resolve().parent
QUAR_DIR = ROOT / "data" / "quarantine"
OBS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# (series_id, 目标文件, freq, kind, units)
#
# units 是 FRED 元数据页 https://fred.stlouisfed.org/data/<ID> 上的 Units **原文**，
# 逐条实测于 2026-08-22，不是凭记忆写的。采集层**原样落盘不换算**，只把这行抄进
# info（原始数据无条件落盘；换算是算术，归派生层）。
#
# 债务五条里只有 FDHBFIN 是 Billions，另三条债务是 Millions —— 这是最容易搞错
# 的一处：写反了外国持有会变成千分之一，在堆叠图里几乎看不见，而不是明显崩掉。
# 真正拦住这类错误的是派生层的恒等式闸与非负闸（见计划 §3.3），这里只负责如实记。
#
# 日/月频那八条的 units 暂留空：给它们补 units 会重写这八个文件的 info，
# 属本批次之外的改动面。留空时 build_info 不产出 units 行（不是产出空值）。
SERIES = (
    ("DGS2", "data/ust_dgs2.json", "daily", "rate", ""),
    ("DGS10", "data/ust_dgs10.json", "daily", "rate", ""),
    ("DGS30", "data/ust_dgs30.json", "daily", "rate", ""),
    ("DFEDTARU", "data/fed_target_upper.json", "daily", "rate", ""),
    ("DFEDTARL", "data/fed_target_lower.json", "daily", "rate", ""),
    ("DFF", "data/fed_effective_rate.json", "daily", "rate", ""),
    ("CPIAUCSL", "data/cpi_cpiaucsl.json", "monthly", "cpi", ""),
    ("CPILFESL", "data/cpi_cpilfesl.json", "monthly", "cpi", ""),
    # —— 美国债务面板（季频，Treasury Bulletin + BEA）——
    ("GFDEBTN", "data/debt_total.json", "quarterly", "debt", "Millions of Dollars"),
    ("FYGFDPUN", "data/debt_held_public.json", "quarterly", "debt", "Millions of Dollars"),
    ("FDHBATN", "data/debt_intragov.json", "quarterly", "debt", "Millions of Dollars"),
    ("FDHBFIN", "data/debt_foreign.json", "quarterly", "debt_foreign", "Billions of Dollars"),
    ("GDP", "data/gdp_nominal.json", "quarterly", "gdp", "Billions of Dollars"),
)

# 债务/GDP 类：值必须为正。与 CPI 指数 ≤ 0 同类，属可确定性判断，走 d 类硬闸。
POSITIVE_KINDS = frozenset({"cpi", "debt", "debt_foreign", "gdp"})

# 滞后多少天开始报 warning。**只进 warnings，不触发 d 类**（宏观 d 类阈值待实测后确定）。
# 季频那两个数是实测出来的，不是拍的：Treasury Bulletin 的 2026 Q1 数据发布于
# 2026-06-18，即观测日期（季首 2026-01-01）之后 168 天；下一期发布前最长会到
# 约 260 天，取 300 天留余量。套用原来的 100 天会天天报（实测当天 GFDEBTN 已 233 天）。
STALE_DAYS_BY_FREQ = {"daily": 10, "weekly": 100, "monthly": 100, "quarterly": 300}
# FDHBFIN 在同一次发布里天然比其余四条晚一个季度（实测 Last Updated 同为
# 2026-06-18，但它只到 2025 Q4，另三条已到 2026 Q1），故单独放宽。
STALE_DAYS_BY_KIND = {"debt_foreign": 400}


class FetchFailure(Exception):
    pass


class MissingKey(Exception):
    """FRED_API_KEY 缺失/为空。刻意不继承 FetchFailure：
    继承的话任何 `except FetchFailure` 都会顺手把它吃掉，又退回 exit 2。"""
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
        raise MissingKey("FRED_API_KEY 缺失")
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


def build_info(series_id: str, points: list[dict], old: dict | None, units: str = "") -> list[str]:
    latest = points[-12:]
    info = [
        "source=FRED",
        f"series_id={series_id}",
        "api_path=/fred/series/observations",
    ]
    if units:
        # FRED 元数据页 Units 原文，原样抄。派生层据此换算，所以这行不能省、不能改写。
        info.append(f"units={units}")
    info.append("latest_observations=[" + ",".join(_point_text(p) for p in latest) + "]")
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
    if kind in POSITIVE_KINDS and any(p["value"] <= 0 for p in points):
        failures.append(f"{kind} 值 ≤ 0")
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
        limit = STALE_DAYS_BY_KIND.get(kind, STALE_DAYS_BY_FREQ.get(freq, 100))
        if age > limit:
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


def process_one(series_id: str, relpath: str, freq: str, kind: str, units: str = "", *, fetcher=fetch_payload, base_dir: Path | None = None, quarantine_dir: Path | None = None) -> tuple[str, int, str]:
    base = Path(base_dir) if base_dir is not None else ROOT
    path = base / relpath
    quarantine = Path(quarantine_dir) if quarantine_dir is not None else base / "data" / "quarantine"
    sweep_stale_tmp(str(path))
    try:
        payload, raw = fetcher(series_id)
    except MissingKey as exc:
        print(f"{series_id}: key exit 3: {exc}")
        return "key", 3, ""
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
        info = build_info(series_id, points, _old_data(path), units)
        save_quarantine(series_id, failures, payload, raw, quarantine)
        atomic_write_json(str(path), failure_envelope(series_id, freq, failures, info), compact=False)
        print(f"{series_id}: d exit 1: {', '.join(failures)}")
        return "validation", 1, ""
    old = _old_data(path)
    info = build_info(series_id, points, old, units)
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

    def check(name, condition, detail=""):
        # detail 只在 FAIL 时打印：PASS 行文案与计数都不变，不影响基线表。
        nonlocal checks, failures
        checks += 1
        if condition:
            print(f"PASS {name}")
        else:
            failures += 1
            print(f"FAIL {name}" + (f"  {detail}" if detail else ""))

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

    def assert_hash_unchanged(label: str, fetcher, *, base_dir: Path, target_relpath: str, series_id: str = "DGS2", freq: str = "daily", kind: str = "rate", quarantine_dir: Path | None = None, expect_code: int = 2, expect_kinds: set[str] = frozenset({"download", "format"})):
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
        check(label, code == expect_code and before == after and result_kind in expect_kinds)

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
        check("13 个序列配置", len(SERIES) == 13)
        check("每行 5 元组（含 units）", all(len(x) == 5 for x in SERIES))
        check("CPI 月频", all(x[2] == "monthly" for x in SERIES if x[3] == "cpi"))
        check("利率日频", all(x[2] == "daily" for x in SERIES if x[3] == "rate"))

        # —— 债务面板五条（季频）——
        QUARTERLY = tuple(x for x in SERIES if x[2] == "quarterly")
        check("季频恰 5 条", len(QUARTERLY) == 5)
        check(
            "季频五条 series_id 与目标文件",
            tuple((x[0], x[1]) for x in QUARTERLY) == (
                ("GFDEBTN", "data/debt_total.json"),
                ("FYGFDPUN", "data/debt_held_public.json"),
                ("FDHBATN", "data/debt_intragov.json"),
                ("FDHBFIN", "data/debt_foreign.json"),
                ("GDP", "data/gdp_nominal.json"),
            ),
            str(tuple((x[0], x[1]) for x in QUARTERLY)),
        )
        UNITS = {x[0]: x[4] for x in SERIES}
        # 这条单列：五条里只有 FDHBFIN 是 Billions，写成 Millions 会让外国持有
        # 变成千分之一 —— 在堆叠图里几乎看不见，不会明显崩掉，只能靠断言拦。
        check("FDHBFIN units = Billions of Dollars",
              UNITS["FDHBFIN"] == "Billions of Dollars", UNITS["FDHBFIN"])
        check("GFDEBTN/FYGFDPUN/FDHBATN units = Millions of Dollars",
              all(UNITS[k] == "Millions of Dollars"
                  for k in ("GFDEBTN", "FYGFDPUN", "FDHBATN")),
              str({k: UNITS[k] for k in ("GFDEBTN", "FYGFDPUN", "FDHBATN")}))
        check("GDP units = Billions of Dollars",
              UNITS["GDP"] == "Billions of Dollars", UNITS["GDP"])
        check("季频五条 units 均非空", all(x[4] for x in QUARTERLY))
        check("日/月八条 units 留空（本批次不改它们的 info）",
              all(x[4] == "" for x in SERIES if x[2] != "quarterly"))

        # units 进 info：非空才产出一行，且是 FRED 原文原样
        _pt = [{"date": "2026-01-01", "value": 1.0}]
        info_q = build_info("FDHBFIN", _pt, None, "Billions of Dollars")
        check("units 非空 → info 有 units 行且为原文",
              "units=Billions of Dollars" in info_q, str(info_q))
        info_d = build_info("DGS2", _pt, None, "")
        check("units 为空 → info 无 units 行（不是空值行）",
              not any(x.startswith("units=") for x in info_d), str(info_d))

        # 非负闸：debt / gdp 与 CPI 同属 d 类硬闸（可确定性判断）
        check("debt 值 ≤ 0 → d 类",
              bool(validate_points([{"date": "2026-01-01", "value": 0}], "debt")))
        check("debt_foreign 值 ≤ 0 → d 类",
              bool(validate_points([{"date": "2026-01-01", "value": -1}], "debt_foreign")))
        check("gdp 值 ≤ 0 → d 类",
              bool(validate_points([{"date": "2026-01-01", "value": -1}], "gdp")))
        check("debt 正值不触发 d 类",
              not validate_points([{"date": "2026-01-01", "value": 1}], "debt"))

        # 滞后：季频只进 warnings，永不触发 d 类（宏观 d 类阈值待实测后确定）
        _stale = [{"date": "2020-01-01", "value": 1.0}]
        check("季频滞后只 warning 不 d",
              bool(warnings_for(_stale, "quarterly", "debt"))
              and not validate_points(_stale, "debt"))
        # 300 天内不报：套用日/月频的 100 天会天天报（实测当天 GFDEBTN 已 233 天）
        _recent = [{"date": (datetime.now(timezone.utc).date()
                             - timedelta(days=200)).isoformat(), "value": 1.0}]
        check("季频滞后 200 天不报 warning",
              not warnings_for(_recent, "quarterly", "debt"),
              str(warnings_for(_recent, "quarterly", "debt")))
        check("同样 200 天在月频要报 warning（证明阈值真按 freq 分流）",
              bool(warnings_for(_recent, "monthly", "cpi")))
        # FDHBFIN 天然晚一个季度，单独放宽到 400 天
        _lag = [{"date": (datetime.now(timezone.utc).date()
                          - timedelta(days=350)).isoformat(), "value": 1.0}]
        check("debt_foreign 350 天不报，同日期换 debt 要报",
              not warnings_for(_lag, "quarterly", "debt_foreign")
              and bool(warnings_for(_lag, "quarterly", "debt")))

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
                "key 缺失 exit 3（不再混进 a 的 2）且 hash 不变",
                missing_key_fetch,
                base_dir=root,
                target_relpath="data/ust_dgs2.json",
                expect_code=3,
                expect_kinds=frozenset({"key"}),
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
        # —— 严重度汇总：断言的是**进程码**，不是单序列的码 ——
        # 这几条是 max() 缺陷的回归闸门：把 worse_exit 换回 max，它们必须红。
        def batch_processor(root: Path, fetcher):
            def proc(series_id, relpath, freq, kind, units=""):
                return process_one(series_id, relpath, freq, kind, units, fetcher=fetcher,
                                   base_dir=root, quarantine_dir=root / "quarantine")
            return proc

        def raise_missing_key(series_id):
            # 刻意走真实 _key()（清空环境变量后调 fetch_payload），而不是直接
            # raise MissingKey：直接 raise 会绕过 _key()，那么「key 到底走 3 还是
            # 混进 a/b 的 2」这个分流就没被考到，把 _key() 改回抛 FetchFailure
            # 时这条仍会绿。
            os.environ.pop("FRED_API_KEY", None)
            return fetch_payload(series_id)

        def raise_download(series_id):
            raise FetchFailure(f"FRED {series_id} 下载失败: 模拟")

        def mixed_fetch(series_id):
            # DGS10 → c 类（日期非法）、CPIAUCSL → d 类（CPI ≤ 0）、DGS2 → a 类（下载失败），
            # 其余序列正常。同批里 1 和 2 并存，考的就是汇总取谁。
            if series_id == "DGS10":
                obs = [{"date": "bad-date", "value": "1.0"}]
            elif series_id == "CPIAUCSL":
                obs = [{"date": "2026-01-01", "value": "0"}]
            elif series_id == "DGS2":
                raise FetchFailure("FRED DGS2 下载失败: 模拟")
            else:
                obs = [{"date": "2026-01-01", "value": "1.0"}]
            payload = {"observations": obs}
            return payload, json.dumps(payload).encode()

        def key_and_download_fetch(series_id):
            if series_id == "DGS2":
                raise FetchFailure("FRED DGS2 下载失败: 模拟")
            raise MissingKey("FRED_API_KEY 缺失")

        def d_and_key_fetch(series_id):
            if series_id == "CPIAUCSL":
                obs = [{"date": "2026-01-01", "value": "0"}]
                payload = {"observations": obs}
                return payload, json.dumps(payload).encode()
            raise MissingKey("FRED_API_KEY 缺失")

        for label, fetcher, expect in [
            ("c/d 与 a 同批时进程码=1（1 比 2 严重，max() 会给出 2）", mixed_fetch, 1),
            ("key 缺失时进程码=3（不是 2）", raise_missing_key, 3),
            ("key 与 a 同批时进程码=3（3 比 2 严重，max() 会给出 3 —— 此条不区分实现）", key_and_download_fetch, 3),
            ("d 与 key 同批时进程码=1（1 比 3 严重，max() 会给出 3）", d_and_key_fetch, 1),
            ("整批只有 a 时进程码=2", raise_download, 2),
        ]:
            os.environ["FRED_API_KEY"] = "TEST_KEY"   # 上一条可能把它清了
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "data").mkdir(parents=True, exist_ok=True)
                rc = run_all(SERIES, processor=batch_processor(root, fetcher))
                check(f"{label} —— 实得 {rc}", rc == expect)

        os.environ["FRED_API_KEY"] = "TEST_KEY"
        check("严重度序 1 > 3 > 2 > 0",
              _severity(1) > _severity(3) > _severity(2) > _severity(0))
        check("未登记的码视为最严重（未预期退出路径必须能压过一切）",
              worse_exit(1, 9) == 9 and worse_exit(9, 1) == 9)
        check("worse_exit 与顺序无关（可交换）",
              all(worse_exit(a, b) == worse_exit(b, a) for a in (0, 1, 2, 3) for b in (0, 1, 2, 3)))

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


# 退出码的严重度**与数值大小无关**，故显式映射，不能用 max()：
#   1 需人工介入（c 类解析失败 / d 类校验不过，已隔离）
#   3 key 缺失或无效（配置问题，等不来自愈，必须有人去看 secrets.FRED_API_KEY）
#   2 上游未更新（下载失败 / 顶层格式非法，下次跑能补回）
#   0 正常
# 排序：1 > 3 > 2 > 0。用 max() 会让 2 盖住 1、3 盖住 1，把「要人管」的错
# 报成「上游没更新，正常」。
EXIT_SEVERITY = {0: 0, 2: 1, 3: 2, 1: 3}
_UNKNOWN_SEVERITY = max(EXIT_SEVERITY.values()) + 1


def _severity(code: int) -> int:
    # 未登记的码视为最严重：出现了没设计过的退出路径，就该压过一切被看见。
    return EXIT_SEVERITY.get(code, _UNKNOWN_SEVERITY)


def worse_exit(current: int, incoming: int) -> int:
    return incoming if _severity(incoming) > _severity(current) else current


def run_all(configs=SERIES, *, processor=process_one) -> int:
    """逐序列跑完，按严重度汇总成一个进程码。
    单独成函数是为了让 --test 能换掉 processor 直接验汇总结果（进程码本身）。"""
    result = 0
    for config in configs:
        _, code, _ = processor(*config)
        result = worse_exit(result, code)
    return result


def main() -> int:
    if "--test" in sys.argv:
        run_tests()
        return 0
    return run_all()


if __name__ == "__main__":
    raise SystemExit(main())
