#!/usr/bin/env python3
"""Fetch MTS Table 9 monthly receipts, outlays and net interest."""
from __future__ import annotations

import calendar
import hashlib
import json
import re
import sys
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from data_envelope import assert_envelope, envelope
from io_utils import atomic_write_json, quarantine_write, read_json_or, sweep_stale_tmp

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / "data" / "treasury_mts_fiscal.json"
QUAR_DIR = ROOT / "data" / "quarantine"
API_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
    "v1/accounting/mts/mts_table_9"
)
SOURCE = "treasury_fiscal_data_mts_table_9"
FIELDS = (
    "record_date", "parent_id", "classification_id", "classification_desc",
    "line_code_nbr", "data_type_cd", "record_type_cd",
    "current_month_rcpt_outly_amt",
)
PAGE_SIZE = 10_000
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TEMP_HTTP_CODES = frozenset({403, 408, 425, 429, 500, 502, 503, 504})
BILLION = Decimal("1000000000")


class FetchFailure(RuntimeError):
    """Temporary network/upstream failure: retain the previous file, exit 2."""


class FormatFailure(RuntimeError):
    """Deterministic HTTP/JSON schema failure: quarantine evidence, exit 1."""

    def __init__(self, message: str, *, payload=None, raw: bytes = b""):
        super().__init__(message)
        self.payload = payload
        self.raw = raw


class ValidationFailure(RuntimeError):
    """A parsed response violates the MTS Table 9 business contract."""


def _request_url(page_number: int) -> str:
    return API_URL + "?" + urlencode({
        "fields": ",".join(FIELDS),
        "sort": "record_date,parent_id,classification_id",
        "page[number]": page_number,
        "page[size]": PAGE_SIZE,
    })


def _fetch_page(page_number: int, *, opener=urlopen) -> tuple[dict, bytes]:
    request = Request(
        _request_url(page_number),
        headers={"User-Agent": "gold-dashboard/1.0 (+https://www.zhangtongxue.com)"},
    )
    try:
        with opener(request, timeout=45) as response:
            status = getattr(response, "status", response.getcode())
            raw = response.read()
    except HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        if exc.code in TEMP_HTTP_CODES:
            raise FetchFailure(f"Treasury MTS HTTP {exc.code}") from exc
        raise FormatFailure(f"Treasury MTS HTTP {exc.code}", raw=raw) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise FetchFailure(f"Treasury MTS 下载失败: {exc}") from exc

    if status in TEMP_HTTP_CODES:
        raise FetchFailure(f"Treasury MTS HTTP {status}")
    if status != 200:
        raise FormatFailure(f"Treasury MTS HTTP {status}", raw=raw)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FormatFailure("Treasury MTS 非 JSON 响应", raw=raw) from exc
    if not isinstance(payload, dict):
        raise FormatFailure("Treasury MTS 顶层格式非法", payload=payload, raw=raw)
    if not isinstance(payload.get("data"), list) or not isinstance(payload.get("meta"), dict):
        raise FormatFailure("Treasury MTS 缺少 data/meta", payload=payload, raw=raw)
    return payload, raw


def fetch_payload(*, opener=urlopen) -> tuple[dict, bytes]:
    pages: list[dict] = []
    rows: list[dict] = []
    page_number = 1
    total_pages = 1
    while page_number <= total_pages:
        payload, _raw = _fetch_page(page_number, opener=opener)
        pages.append(payload)
        rows.extend(payload["data"])
        try:
            total_pages = int(payload["meta"].get("total-pages", 1))
        except (TypeError, ValueError) as exc:
            raise FormatFailure("Treasury MTS meta.total-pages 非整数",
                                payload=payload, raw=_raw) from exc
        if total_pages < 1:
            raise FormatFailure("Treasury MTS meta.total-pages 小于 1",
                                payload=payload, raw=_raw)
        page_number += 1

    try:
        total_count = int(pages[0]["meta"]["total-count"])
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        evidence = json.dumps(pages, ensure_ascii=False).encode("utf-8")
        raise FormatFailure("Treasury MTS meta.total-count 缺失或非整数",
                            payload=pages, raw=evidence) from exc
    if total_count != len(rows):
        evidence = json.dumps(pages, ensure_ascii=False).encode("utf-8")
        raise FormatFailure(
            f"Treasury MTS 分页不完整: meta={total_count}, fetched={len(rows)}",
            payload=pages, raw=evidence)
    return {"data": rows, "meta": pages[0]["meta"]}, json.dumps(
        pages, ensure_ascii=False).encode("utf-8")


def _date(value, index: int) -> str:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValidationFailure(f"第 {index} 条 record_date 非法: {value!r}")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValidationFailure(f"第 {index} 条 record_date 不存在: {value!r}") from exc
    if parsed.isoformat() > date.today().isoformat():
        raise ValidationFailure(f"record_date 位于未来: {value}")
    if parsed.day != calendar.monthrange(parsed.year, parsed.month)[1]:
        raise ValidationFailure(f"record_date 不是 calendar month-end: {value}")
    return value


def _amount(value, field: str, record_date: str, *, allow_negative: bool = False) -> Decimal:
    if value is None or isinstance(value, bool) or value in ("", "null"):
        raise ValidationFailure(f"{record_date}: {field} 缺失")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationFailure(f"{record_date}: {field} 不是有效金额: {value!r}") from exc
    if not amount.is_finite() or (not allow_negative and amount < 0):
        qualifier = "有限数" if allow_negative else "非负有限数"
        raise ValidationFailure(f"{record_date}: {field} 必须是{qualifier}")
    return amount


def _is_root(parent_id) -> bool:
    return parent_id in (None, "", "null")


def _one(rows: list[dict], predicate, label: str, record_date: str) -> dict:
    matches = [row for row in rows if predicate(row)]
    if len(matches) != 1:
        raise ValidationFailure(
            f"{record_date}: {label} 层级匹配应为 1 条，实际 {len(matches)} 条")
    return matches[0]


def _assert_anchor(row: dict, *, line: str, data_type: str,
                   record_type: str, label: str, record_date: str) -> None:
    actual = (str(row.get("line_code_nbr")), row.get("data_type_cd"),
              row.get("record_type_cd"))
    expected = (line, data_type, record_type)
    if actual != expected:
        raise ValidationFailure(
            f"{record_date}: {label} hierarchy anchor 漂移: {actual!r} != {expected!r}")


def parse_records(payload: dict) -> list[dict]:
    source_rows = payload.get("data")
    if not isinstance(source_rows, list) or not source_rows:
        raise ValidationFailure("MTS Table 9 data 为空")

    by_date: dict[str, list[dict]] = {}
    for index, row in enumerate(source_rows, 1):
        if not isinstance(row, dict):
            raise ValidationFailure(f"第 {index} 条记录不是对象")
        missing = [field for field in FIELDS if field not in row]
        if missing:
            raise ValidationFailure(f"第 {index} 条记录缺字段: {missing}")
        record_date = _date(row.get("record_date"), index)
        by_date.setdefault(record_date, []).append(row)

    records: list[dict] = []
    for record_date in sorted(by_date):
        rows = by_date[record_date]
        receipts_root = _one(
            rows,
            lambda row: _is_root(row.get("parent_id"))
            and row.get("classification_desc") == "Receipts",
            "Receipts root", record_date)
        outlays_root = _one(
            rows,
            lambda row: _is_root(row.get("parent_id"))
            and row.get("classification_desc") == "Net Outlays",
            "Net Outlays root", record_date)
        receipts_parent = str(receipts_root.get("classification_id"))
        outlays_parent = str(outlays_root.get("classification_id"))
        receipts_total = _one(
            rows,
            lambda row: str(row.get("parent_id")) == receipts_parent
            and row.get("classification_desc") == "Total",
            "Receipts/Total", record_date)
        net_interest = _one(
            rows,
            lambda row: str(row.get("parent_id")) == outlays_parent
            and row.get("classification_desc") == "Net Interest",
            "Net Outlays/Net Interest", record_date)
        outlays_total = _one(
            rows,
            lambda row: str(row.get("parent_id")) == outlays_parent
            and row.get("classification_desc") == "Total",
            "Net Outlays/Total", record_date)
        _assert_anchor(receipts_total, line="120", data_type="T", record_type="SL",
                       label="Receipts/Total", record_date=record_date)
        _assert_anchor(net_interest, line="320", data_type="D", record_type="F",
                       label="Net Outlays/Net Interest", record_date=record_date)
        _assert_anchor(outlays_total, line="340", data_type="T", record_type="SL",
                       label="Net Outlays/Total", record_date=record_date)

        def bn(row: dict, label: str, *, allow_negative: bool = False) -> float:
            return float(_amount(row["current_month_rcpt_outly_amt"], label,
                                 record_date, allow_negative=allow_negative) / BILLION)

        records.append({
            "date": record_date,
            "receipts_bn": bn(receipts_total, "receipts"),
            "total_outlays_bn": bn(outlays_total, "total_outlays"),
            "net_interest_bn": bn(net_interest, "net_interest", allow_negative=True),
        })
    return records


def _missing_months(records: list[dict]) -> list[str]:
    if len(records) < 2:
        return []
    missing: list[str] = []
    year, month = map(int, records[0]["date"][:7].split("-"))
    last = records[-1]["date"][:7]
    while f"{year:04d}-{month:02d}" != last:
        month += 1
        if month == 13:
            year += 1
            month = 1
        key = f"{year:04d}-{month:02d}"
        if key != last and all(not row["date"].startswith(key) for row in records):
            missing.append(key)
    return missing


def build_output(records: list[dict], *, revisions: list[str] | None = None) -> dict:
    dates = [row["date"] for row in records]
    missing = _missing_months(records)
    warnings = (["missing_calendar_months=" + ",".join(missing)] if missing else [])
    info = [
        "endpoint=v1/accounting/mts/mts_table_9",
        "units=USD_bn",
        "units_normalized=current_month_usd_divided_by_1e9_once",
        "selection=hierarchy_first_with_line_and_type_schema_guards",
        "receipts=Receipts/Total line=120 type=T/SL",
        "total_outlays=Net Outlays/Total line=340 type=T/SL",
        "net_interest=Net Outlays/Net Interest line=320 type=D/F",
        "amount_basis=current_calendar_month_not_fytd_no_fill",
    ]
    if revisions:
        info.append("revised_months=" + ",".join(revisions))
    return envelope(SOURCE, "monthly", records, dates=dates,
                    warnings=warnings, info=info)


def _revision_dates(existing_rows, records: list[dict]) -> list[str]:
    old = {row.get("date"): row for row in existing_rows if isinstance(row, dict)}
    return [row["date"] for row in records
            if row["date"] in old and old[row["date"]] != row]


def _quarantine(reason: str, payload, raw: bytes, quarantine_dir: Path) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine_write(str(quarantine_dir), "treasury-mts-fiscal", stamp,
                     reason=[reason], payload={"source": SOURCE, "response": payload},
                     raw=raw, raw_ext="raw.json")


def run_once(*, output_path: Path = OUT_PATH, quarantine_dir: Path = QUAR_DIR,
             fetcher=fetch_payload) -> int:
    sweep_stale_tmp(str(output_path))
    try:
        payload, raw = fetcher()
    except FetchFailure as exc:
        print(f"WARNING {exc}；旧 {output_path.name} 保持不变")
        return 2
    except FormatFailure as exc:
        _quarantine(str(exc), exc.payload, exc.raw, quarantine_dir)
        print(f"ERROR {exc}；响应已隔离，旧 {output_path.name} 保持不变")
        return 1
    try:
        records = parse_records(payload)
    except ValidationFailure as exc:
        _quarantine(str(exc), payload, raw, quarantine_dir)
        print(f"ERROR {exc}；响应已隔离，旧 {output_path.name} 保持不变")
        return 1

    existing = read_json_or(str(output_path), None)
    revisions: list[str] = []
    if existing is not None:
        try:
            assert_envelope(existing)
        except ValueError as exc:
            print(f"ERROR 现有 {output_path.name} 不是合法信封：{exc}；拒绝覆盖")
            return 1
        if existing.get("source") != SOURCE or existing.get("freq") != "monthly":
            print(f"ERROR 现有 {output_path.name} source/freq 不匹配；拒绝覆盖")
            return 1
        if existing.get("data") == records:
            print(f"MTS Table 9 无业务变化，跳过写盘：{records[0]['date']}.."
                  f"{records[-1]['date']} ({len(records)} months)")
            return 0
        revisions = _revision_dates(existing.get("data", []), records)

    atomic_write_json(str(output_path), build_output(records, revisions=revisions), compact=False)
    print(f"MTS Table 9 已写入 {output_path}: {records[0]['date']}.."
          f"{records[-1]['date']} ({len(records)} months)")
    return 0


def _fixture_month(record_date: str, receipts="100000000000",
                   outlays="120000000000", interest="10000000000") -> list[dict]:
    def row(parent, cid, desc, line, dtype, rtype, amount="0"):
        return {"record_date": record_date, "parent_id": parent,
                "classification_id": cid, "classification_desc": desc,
                "line_code_nbr": line, "data_type_cd": dtype,
                "record_type_cd": rtype,
                "current_month_rcpt_outly_amt": amount}
    return [
        row("null", "r", "Receipts", "10", "S", "SL"),
        row("r", "rt", "Total", "120", "T", "SL", receipts),
        row("null", "o", "Net Outlays", "130", "S", "SL"),
        row("o", "ni", "Net Interest", "320", "D", "F", interest),
        row("o", "ot", "Total", "340", "T", "SL", outlays),
        row("o", "gross", "Gross Interest", "319", "D", "F", "99000000000"),
    ]


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

    rows = _fixture_month("2026-06-30") + _fixture_month(
        "2026-07-31", receipts="110000000000", outlays="130000000000",
        interest="11000000000")
    fixture = {"data": rows, "meta": {"total-pages": 1, "total-count": len(rows)}}
    parsed = parse_records(fixture)
    check("hierarchy 区分两个 Total", parsed[0]["receipts_bn"] == 100.0
          and parsed[0]["total_outlays_bn"] == 120.0)
    check("只选 Net Interest 不以 gross interest 替代", parsed[0]["net_interest_bn"] == 10.0)
    check("源 USD 只除以 1e9 一次", parsed[1] == {
        "date": "2026-07-31", "receipts_bn": 110.0,
        "total_outlays_bn": 130.0, "net_interest_bn": 11.0})
    output = build_output(parsed)
    check("schema v0 monthly envelope", output["schema_version"] == 0
          and output["freq"] == "monthly" and output["date_field"] == "date")
    check("coverage 记录真实月份", output["coverage"] == {
        "first": "2026-06-30", "last": "2026-07-31", "count": 2})
    check("未使用 FYTD 且单位锚存在", "amount_basis=current_calendar_month_not_fytd_no_fill"
          in output["info"] and "divided_by_1e9_once" in " ".join(output["info"]))

    swapped = json.loads(json.dumps(fixture))
    for row in swapped["data"]:
        if row["classification_desc"] == "Total":
            row["parent_id"] = "o" if row["parent_id"] == "r" else "r"
    try:
        parse_records(swapped)
        rejected = False
    except ValidationFailure:
        rejected = True
    check("交换 receipts/outlays hierarchy 必须拒绝", rejected)

    drift = json.loads(json.dumps(fixture))
    next(row for row in drift["data"] if row["line_code_nbr"] == "320")[
        "line_code_nbr"] = "999"
    try:
        parse_records(drift)
        rejected = False
    except ValidationFailure:
        rejected = True
    check("hierarchy line/type 漂移必须拒绝", rejected)

    gross_only = json.loads(json.dumps(fixture))
    gross_only["data"] = [row for row in gross_only["data"]
                          if row["classification_desc"] != "Net Interest"]
    try:
        parse_records(gross_only)
        rejected = False
    except ValidationFailure:
        rejected = True
    check("缺 Net Interest 时不回退 gross interest", rejected)

    negative_interest = parse_records({
        "data": _fixture_month("2015-09-30", interest="-11344064796.69"),
        "meta": {},
    })
    check("月度 Net Interest 保留官方有符号净额",
          negative_interest[0]["net_interest_bn"] == -11.34406479669)
    midmonth = {"data": _fixture_month("2026-07-15"), "meta": {}}
    try:
        parse_records(midmonth)
        rejected = False
    except ValidationFailure:
        rejected = True
    check("非 calendar month-end 日期拒绝", rejected)

    missing = parse_records({"data": _fixture_month("2026-05-31")
                             + _fixture_month("2026-07-31"), "meta": {}})
    missing_output = build_output(missing)
    check("缺月不补点", len(missing_output["data"]) == 2
          and [row["date"] for row in missing_output["data"]]
          == ["2026-05-31", "2026-07-31"])
    check("缺月显式 warning", missing_output["warnings"] == [
        "missing_calendar_months=2026-06"])

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "treasury_mts_fiscal.json"
        quarantine = root / "quarantine"

        def good_fetcher():
            return fixture, json.dumps(fixture).encode()

        rc = run_once(output_path=target, quarantine_dir=quarantine, fetcher=good_fetcher)
        first_bytes = target.read_bytes()
        first_hash = hashlib.sha256(first_bytes).hexdigest()
        check("成功写盘 exit 0", rc == 0 and target.exists())
        rc = run_once(output_path=target, quarantine_dir=quarantine, fetcher=good_fetcher)
        check("相同业务数据幂等 exit 0", rc == 0)
        check("幂等不改 bytes/SHA-256", target.read_bytes() == first_bytes
              and hashlib.sha256(target.read_bytes()).hexdigest() == first_hash)

        revised_fixture = json.loads(json.dumps(fixture))
        revised_fixture["data"][1]["current_month_rcpt_outly_amt"] = "101000000000"

        def revision_fetcher():
            return revised_fixture, json.dumps(revised_fixture).encode()

        rc = run_once(output_path=target, quarantine_dir=quarantine,
                      fetcher=revision_fetcher)
        revised = json.loads(target.read_bytes())
        check("历史修订触发原子重写", rc == 0 and revised["data"][0]["receipts_bn"] == 101.0)
        check("历史修订月份进入 info", "revised_months=2026-06-30" in revised["info"])
        revised_bytes = target.read_bytes()

        def network_failure():
            raise FetchFailure("fixture network")

        rc = run_once(output_path=target, quarantine_dir=quarantine,
                      fetcher=network_failure)
        check("网络失败 exit 2 且旧文件不覆盖", rc == 2
              and target.read_bytes() == revised_bytes)
        check("网络失败不伪造 quarantine", not list(quarantine.glob("*")))

        def bad_fetcher():
            bad = json.loads(json.dumps(fixture))
            bad["data"] = [row for row in bad["data"]
                           if row["classification_desc"] != "Net Interest"]
            return bad, json.dumps(bad).encode()

        rc = run_once(output_path=target, quarantine_dir=quarantine,
                      fetcher=bad_fetcher)
        payload_files = [path for path in quarantine.glob("treasury-mts-fiscal-*.json")
                         if not path.name.endswith(".raw.json")]
        raw_files = list(quarantine.glob("treasury-mts-fiscal-*.raw.json"))
        check("数据失败 exit 1 且旧文件不覆盖", rc == 1
              and target.read_bytes() == revised_bytes)
        check("数据失败 payload/raw 双证据", len(payload_files) == 1
              and len(raw_files) == 1)

        unrelated = root / "unrelated.json"
        unrelated.write_bytes(b"unrelated-source")
        run_once(output_path=target, quarantine_dir=root / "q2", fetcher=network_failure)
        check("MTS 失败不污染其它数据源", unrelated.read_bytes() == b"unrelated-source")

    workflow = (ROOT / ".github" / "workflows" / "update-cot.yml").read_text(
        encoding="utf-8")
    step_match = re.search(
        r"- name: Run fetch_treasury_fiscal\.py\n(?P<body>.*?)(?=\n\s+- name:)",
        workflow, re.S)
    step_body = step_match.group("body") if step_match else ""
    gate_match = re.search(
        r'case "\$\{\{ steps\.treasury_fiscal\.outputs\.code \}\}" in'
        r'(?P<body>.*?)esac', workflow, re.S)
    gate_body = gate_match.group("body") if gate_match else ""
    check("workflow validation 执行 MTS 离线测试",
          "python fetch_treasury_fiscal.py --test" in workflow)
    check("workflow MTS step 捕获真实退出码", bool(step_match)
          and "id: treasury_fiscal" in step_body
          and 'echo "code=$?" >> "$GITHUB_OUTPUT"' in step_body
          and "exit 0" in step_body)
    check("workflow 周六 COT 专场不重复抓 MTS",
          "if: github.event.schedule != '0 18 * * 6'" in step_body)
    check("workflow raw-first commit 包含 MTS envelope",
          "data/treasury_mts_fiscal.json" in workflow)
    check("workflow MTS gate 明确覆盖 0/1/2/*", bool(gate_match)
          and "1) fail" in gate_body and "2) echo \"::warning" in gate_body
          and '0|"")' in gate_body and "*) fail" in gate_body)

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
