#!/usr/bin/env python3
"""Fetch U.S. Treasury Fiscal Data "Debt to the Penny" into a schema v0 envelope."""
from __future__ import annotations

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
OUT_PATH = ROOT / "data" / "treasury_debt_daily.json"
QUAR_DIR = ROOT / "data" / "quarantine"
API_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
    "v2/accounting/od/debt_to_penny"
)
SOURCE = "treasury_fiscal_data_debt_to_penny"
FIELDS = (
    "record_date",
    "tot_pub_debt_out_amt",
    "debt_held_public_amt",
    "intragov_hold_amt",
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PAGE_SIZE = 10_000
TEMP_HTTP_CODES = frozenset({403, 408, 425, 429, 500, 502, 503, 504})


class FetchFailure(RuntimeError):
    """Temporary network/upstream failure: keep the previous file and return exit 2."""


class FormatFailure(RuntimeError):
    """Deterministic response/schema failure: quarantine evidence and return exit 1."""

    def __init__(self, message: str, *, payload=None, raw: bytes = b""):
        super().__init__(message)
        self.payload = payload
        self.raw = raw


class ValidationFailure(RuntimeError):
    """Parsed response violates the Debt to the Penny business contract."""


def _request_url(page_number: int) -> str:
    return API_URL + "?" + urlencode({
        "fields": ",".join(FIELDS),
        "sort": "record_date",
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
            raise FetchFailure(f"Treasury Fiscal Data HTTP {exc.code}") from exc
        raise FormatFailure(
            f"Treasury Fiscal Data HTTP {exc.code}", raw=raw) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise FetchFailure(f"Treasury Fiscal Data 下载失败: {exc}") from exc

    if status in TEMP_HTTP_CODES:
        raise FetchFailure(f"Treasury Fiscal Data HTTP {status}")
    if status != 200:
        raise FormatFailure(f"Treasury Fiscal Data HTTP {status}", raw=raw)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FormatFailure("Treasury Fiscal Data 非 JSON 响应", raw=raw) from exc
    if not isinstance(payload, dict):
        raise FormatFailure("Treasury Fiscal Data 顶层格式非法", payload=payload, raw=raw)
    if not isinstance(payload.get("data"), list) or not isinstance(payload.get("meta"), dict):
        raise FormatFailure("Treasury Fiscal Data 缺少 data/meta", payload=payload, raw=raw)
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
        meta = payload["meta"]
        try:
            total_pages = int(meta.get("total-pages", 1))
        except (TypeError, ValueError) as exc:
            raise FormatFailure(
                "Treasury Fiscal Data meta.total-pages 非整数",
                payload=payload,
                raw=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            ) from exc
        if total_pages < 1:
            raise FormatFailure(
                "Treasury Fiscal Data meta.total-pages 小于 1",
                payload=payload,
                raw=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
        page_number += 1

    aggregate_meta = pages[0]["meta"] if pages else {}
    try:
        total_count = int(aggregate_meta["total-count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FormatFailure(
            "Treasury Fiscal Data meta.total-count 缺失或非整数",
            payload=pages,
            raw=json.dumps(pages, ensure_ascii=False).encode("utf-8"),
        ) from exc
    if total_count != len(rows):
        raise FormatFailure(
            f"Treasury Fiscal Data 分页不完整: meta={total_count}, fetched={len(rows)}",
            payload=pages,
            raw=json.dumps(pages, ensure_ascii=False).encode("utf-8"),
        )
    aggregate = {"data": rows, "meta": aggregate_meta}
    raw_evidence = json.dumps(pages, ensure_ascii=False).encode("utf-8")
    return aggregate, raw_evidence


def _money(value, field: str, record_date: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValidationFailure(f"{record_date}: {field} 缺失或类型非法")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationFailure(f"{record_date}: {field} 不是有效金额: {value!r}") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValidationFailure(f"{record_date}: {field} 必须为正数")
    return amount


def _optional_money(value, field: str, record_date: str) -> Decimal | None:
    # Fiscal Data uses the literal string "null" for the early part of this
    # table. Preserve that source gap as JSON null; it is not zero and must not
    # be forward-filled.
    if value is None or value == "null" or value == "":
        return None
    return _money(value, field, record_date)


def parse_records(payload: dict) -> tuple[list[dict], list[str]]:
    source_rows = payload.get("data")
    if not isinstance(source_rows, list) or not source_rows:
        raise ValidationFailure("Debt to the Penny data 为空")

    records: list[dict] = []
    warnings: list[str] = []
    seen: set[str] = set()
    previous = None
    today = date.today().isoformat()
    for i, row in enumerate(source_rows):
        if not isinstance(row, dict):
            raise ValidationFailure(f"第 {i + 1} 条记录不是对象")
        record_date = row.get("record_date")
        if not isinstance(record_date, str) or not DATE_RE.fullmatch(record_date):
            raise ValidationFailure(f"第 {i + 1} 条 record_date 非法: {record_date!r}")
        try:
            datetime.strptime(record_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValidationFailure(f"第 {i + 1} 条 record_date 不存在: {record_date!r}") from exc
        if record_date > today:
            raise ValidationFailure(f"record_date 位于未来: {record_date}")
        if record_date in seen:
            raise ValidationFailure(f"record_date 重复: {record_date}")
        if previous is not None and record_date <= previous:
            raise ValidationFailure("Debt to the Penny 记录未按日期严格升序")

        total = _money(row.get("tot_pub_debt_out_amt"), "tot_pub_debt_out_amt", record_date)
        public = _optional_money(
            row.get("debt_held_public_amt"), "debt_held_public_amt", record_date)
        intragov = _optional_money(
            row.get("intragov_hold_amt"), "intragov_hold_amt", record_date)
        if (public is None) != (intragov is None):
            raise ValidationFailure(
                f"{record_date}: public/intragov 只能同时有值或同时缺失")
        if public is not None and intragov is not None:
            identity_gap = abs(total - public - intragov)
            # The official table has cent-level component rounding (historical
            # maximum $0.10). A materially inconsistent component pair is not
            # allowed through: preserve total, null both components for that
            # date, and expose a warning instead of fabricating an identity.
            if identity_gap > Decimal("0.10"):
                warnings.append(
                    f"{record_date}: public+intragov 与 total 相差 {identity_gap} USD；"
                    "该日 public/intragov 已置 null")
                public = None
                intragov = None

        billion = Decimal("1000000000")
        records.append({
            "date": record_date,
            "total_bn": float(total / billion),
            "public_bn": None if public is None else float(public / billion),
            "intragov_bn": None if intragov is None else float(intragov / billion),
        })
        seen.add(record_date)
        previous = record_date
    return records, warnings


def build_output(records: list[dict], warnings: list[str] | None = None) -> dict:
    dates = [row["date"] for row in records]
    public_dates = [row["date"] for row in records if row["public_bn"] is not None]
    intragov_dates = [row["date"] for row in records if row["intragov_bn"] is not None]
    return envelope(
        SOURCE,
        "daily",
        records,
        dates=dates,
        warnings=warnings or [],
        info=[
            "endpoint=v2/accounting/od/debt_to_penny",
            "fields=" + ",".join(FIELDS),
            "units=USD_bn",
            "units_normalized=source_usd_divided_by_1e9",
            "identity=total_bn==public_bn+intragov_bn source_rounding_tolerance_usd=0.10",
            "source_frequency=daily_business_days_no_fill",
            "total_coverage=" + f"{dates[0]}..{dates[-1]} count={len(dates)}",
            "public_coverage=" + (
                f"{public_dates[0]}..{public_dates[-1]} count={len(public_dates)}"
                if public_dates else "none"),
            "intragov_coverage=" + (
                f"{intragov_dates[0]}..{intragov_dates[-1]} count={len(intragov_dates)}"
                if intragov_dates else "none"),
        ],
    )


def _quarantine(reason: str, payload, raw: bytes, quarantine_dir: Path) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine_write(
        str(quarantine_dir),
        "treasury-debt",
        stamp,
        reason=[reason],
        payload={"source": SOURCE, "response": payload},
        raw=raw,
        raw_ext="raw.json",
    )


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
        records, warnings = parse_records(payload)
    except ValidationFailure as exc:
        _quarantine(str(exc), payload, raw, quarantine_dir)
        print(f"ERROR {exc}；响应已隔离，旧 {output_path.name} 保持不变")
        return 1

    existing = read_json_or(str(output_path), None)
    if existing is not None:
        try:
            assert_envelope(existing)
        except ValueError as exc:
            print(f"ERROR 现有 {output_path.name} 不是合法信封：{exc}；拒绝覆盖")
            return 1
        if existing.get("source") != SOURCE or existing.get("freq") != "daily":
            print(f"ERROR 现有 {output_path.name} source/freq 不匹配；拒绝覆盖")
            return 1
        if existing.get("data") == records:
            print(
                f"Debt to the Penny 无业务变化，跳过写盘："
                f"{records[0]['date']}..{records[-1]['date']} ({len(records)} records)"
            )
            return 0

    output = build_output(records, warnings)
    atomic_write_json(str(output_path), output, compact=False)
    print(
        f"Debt to the Penny 已写入 {output_path}: "
        f"{records[0]['date']}..{records[-1]['date']} ({len(records)} records)"
    )
    return 0


def run_tests() -> None:
    passed = 0
    failed = 0

    def check(name: str, ok: bool, detail="") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"PASS {name}" + (f"  {detail}" if detail else ""))
        else:
            failed += 1
            print(f"FAIL {name}  {detail}")

    fixture_rows = [
        {
            "record_date": "1993-04-01",
            "tot_pub_debt_out_amt": "4334000000000.00",
            "debt_held_public_amt": "null",
            "intragov_hold_amt": "null",
        },
        {
            "record_date": "2026-08-20",
            "tot_pub_debt_out_amt": "40000000000000.00",
            "debt_held_public_amt": "32250000000000.00",
            "intragov_hold_amt": "7750000000000.00",
        },
        {
            "record_date": "2026-08-21",
            "tot_pub_debt_out_amt": "40010000000000.00",
            "debt_held_public_amt": "32260000000000.00",
            "intragov_hold_amt": "7750000000000.00",
        },
    ]
    fixture = {"data": fixture_rows, "meta": {"total-pages": 1, "total-count": 3}}
    records, warnings = parse_records(fixture)
    check("美元在采集层除以 1e9", records[1] == {
        "date": "2026-08-20", "total_bn": 40000.0,
        "public_bn": 32250.0, "intragov_bn": 7750.0})
    check("早期 public/intragov 缺失保持 null", records[0] == {
        "date": "1993-04-01", "total_bn": 4334.0,
        "public_bn": None, "intragov_bn": None})
    check("真实日期与三条记录原样保留", [r["date"] for r in records]
          == ["1993-04-01", "2026-08-20", "2026-08-21"])
    output = build_output(records, warnings)
    check("schema v0 daily envelope", output["schema_version"] == 0
          and output["freq"] == "daily" and output["date_field"] == "date")
    check("coverage first/last/count", output["coverage"]
          == {"first": "1993-04-01", "last": "2026-08-21", "count": 3})
    check("信封记录单位与无填充原则", "units=USD_bn" in output["info"]
          and "source_frequency=daily_business_days_no_fill" in output["info"])
    check("分字段 coverage 如实记录", "public_coverage=2026-08-20..2026-08-21 count=2"
          in output["info"] and "total_coverage=1993-04-01..2026-08-21 count=3"
          in output["info"])

    bad_cases = [
        ("空数据拒绝", {"data": [], "meta": {}}),
        ("重复日期拒绝", {"data": fixture_rows + [fixture_rows[-1]], "meta": {}}),
        ("乱序日期拒绝", {"data": list(reversed(fixture_rows)), "meta": {}}),
        ("单侧缺金额拒绝", {"data": [{**fixture_rows[1], "debt_held_public_amt": None}], "meta": {}}),
        ("非正金额拒绝", {"data": [{**fixture_rows[1], "intragov_hold_amt": "0"}], "meta": {}}),
    ]
    for name, bad in bad_cases:
        try:
            parse_records(bad)
            rejected = False
        except ValidationFailure:
            rejected = True
        check(name, rejected)

    anomaly_fixture = {"data": [{
        **fixture_rows[1],
        "tot_pub_debt_out_amt": "39990000000000.00",
    }], "meta": {}}
    anomaly_records, anomaly_warnings = parse_records(anomaly_fixture)
    check("物质性恒等式异常不伪造分项", anomaly_records[0]["total_bn"] == 39990.0
          and anomaly_records[0]["public_bn"] is None
          and anomaly_records[0]["intragov_bn"] is None)
    check("物质性恒等式异常显式 warning", len(anomaly_warnings) == 1
          and "已置 null" in anomaly_warnings[0])

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "data" / "treasury_debt_daily.json"
        quarantine = root / "quarantine"

        def good_fetcher():
            return fixture, json.dumps(fixture).encode()

        rc = run_once(output_path=target, quarantine_dir=quarantine, fetcher=good_fetcher)
        first_bytes = target.read_bytes()
        first_hash = hashlib.sha256(first_bytes).hexdigest()
        check("成功写盘 exit 0", rc == 0 and target.exists())
        check("成功文件可 strict envelope", json.loads(first_bytes)["data"] == records)
        rc = run_once(output_path=target, quarantine_dir=quarantine, fetcher=good_fetcher)
        check("相同业务数据幂等 exit 0", rc == 0)
        check("幂等不重写 bytes/SHA-256", target.read_bytes() == first_bytes
              and hashlib.sha256(target.read_bytes()).hexdigest() == first_hash)

        def network_failure():
            raise FetchFailure("fixture network")

        rc = run_once(output_path=target, quarantine_dir=quarantine, fetcher=network_failure)
        check("网络失败 exit 2", rc == 2)
        check("网络失败旧文件逐字节不覆盖", target.read_bytes() == first_bytes)
        check("网络失败不伪造 quarantine", not list(quarantine.glob("*")))

        def format_failure():
            raise FormatFailure("fixture format", payload={"bad": True}, raw=b"bad-json")

        rc = run_once(output_path=target, quarantine_dir=quarantine, fetcher=format_failure)
        format_payloads = [p for p in quarantine.glob("treasury-debt-*.json")
                           if not p.name.endswith(".raw.json")]
        format_raw = list(quarantine.glob("treasury-debt-*.raw.json"))
        check("格式失败 exit 1 且旧文件不覆盖", rc == 1 and target.read_bytes() == first_bytes)
        check("格式失败 payload/raw 两份证据", len(format_payloads) == 1 and len(format_raw) == 1)

        def data_failure():
            bad = {"data": [{**fixture_rows[1], "tot_pub_debt_out_amt": "0"}],
                   "meta": {"total-pages": 1, "total-count": 1}}
            return bad, json.dumps(bad).encode()

        data_quarantine = root / "quarantine-data"
        rc = run_once(output_path=target, quarantine_dir=data_quarantine, fetcher=data_failure)
        data_payloads = [p for p in data_quarantine.glob("treasury-debt-*.json")
                         if not p.name.endswith(".raw.json")]
        data_raw = list(data_quarantine.glob("treasury-debt-*.raw.json"))
        check("数据失败 exit 1 且旧文件不覆盖", rc == 1 and target.read_bytes() == first_bytes)
        check("数据失败 payload/raw 两份证据", len(data_payloads) == 1 and len(data_raw) == 1)

    workflow = (ROOT / ".github" / "workflows" / "update-cot.yml").read_text(
        encoding="utf-8")
    step_match = re.search(
        r"- name: Run fetch_treasury_debt\.py\n(?P<body>.*?)(?=\n\s+- name:)",
        workflow,
        re.S,
    )
    step_body = step_match.group("body") if step_match else ""
    gate_match = re.search(
        r'case "\$\{\{ steps\.treasury_debt\.outputs\.code \}\}" in(?P<body>.*?)esac',
        workflow,
        re.S,
    )
    gate_body = gate_match.group("body") if gate_match else ""
    check("workflow 每日 UTC 22:00 调度", "- cron: '0 22 * * *'" in workflow)
    check("workflow validation 执行 Treasury 离线测试",
          "python fetch_treasury_debt.py --test" in workflow)
    check("workflow Treasury step 捕获真实退出码", bool(step_match)
          and "id: treasury_debt" in step_body
          and 'echo "code=$?" >> "$GITHUB_OUTPUT"' in step_body
          and "exit 0" in step_body)
    check("workflow 周六 COT 专场不重复抓 Treasury",
          "if: github.event.schedule != '0 18 * * 6'" in step_body)
    check("workflow commit 清单包含日频债务信封",
          "data/treasury_debt_daily.json" in workflow)
    check("workflow Treasury gate 明确覆盖 0/1/2/*", bool(gate_match)
          and "1) fail" in gate_body and "2) echo \"::warning" in gate_body
          and '0|"")' in gate_body and "*) fail" in gate_body)
    check("workflow Treasury 网络失败只 warning",
          "2) echo" in gate_body and "2) fail" not in gate_body)

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
