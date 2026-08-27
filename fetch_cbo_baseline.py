#!/usr/bin/env python3
"""Parse one audited CBO budget-baseline workbook into versioned envelopes.

CBO workbooks are forecast vintages, not mutable history.  This parser is
therefore deliberately pinned to the February 2026 workbook schema and hash.
Updating to another release requires a new vintage id and an explicit schema
audit; no sheet-name or row-position guessing is allowed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from data_envelope import assert_envelope, envelope
from io_utils import atomic_write_json, quarantine_write, read_json_or, sweep_stale_tmp

ROOT = Path(__file__).resolve().parent
SOURCE_PATH = ROOT / "data" / "cbo" / "source" / "51118-2026-02-Budget-Projections.xlsx"
VINTAGE_PATH = ROOT / "data" / "cbo" / "baseline-2026-02.json"
LATEST_PATH = ROOT / "data" / "derived" / "cbo_baseline_latest.json"
DIAGNOSTICS_DIR = ROOT / "data" / "cbo" / "diagnostics"

CBO_PARSER_VERSION = "1"
VINTAGE_ID = "2026-02"
PUBLICATION_DATE = "2026-02-11"
SOURCE_DOWNLOADED_AT = "2026-08-27T07:58:05Z"
TITLE = "The Budget and Economic Outlook: 2026 to 2036"
SOURCE_PAGE_URL = "https://www.cbo.gov/publication/61882"
SOURCE_FILE_URL = (
    "https://www.cbo.gov/system/files/2026-02/"
    "51118-2026-02-Budget-Projections.xlsx"
)
SOURCE_FILE_SHA256 = "06593fcc3b8517806994090a6a9ffe748cfdd19514d2f9d2f18a1841b64b33a5"
EXPECTED_SHEETS = (
    "Contents", "Table 1-1", "Table 1-2", "Table 1-3", "Table 1-4",
    "Table 3-1", "Table 3-2", "Table 3-2, unadj", "Table 3-3",
    "Table 3-4", "Table 3-5", "Table 3-6", "Table 3-7", "Table 3-8",
    "Box 3-2 Table", "Table 5-1",
)
EXPECTED_YEARS = list(range(2025, 2037))
ACTUAL_THROUGH_YEAR = 2025
PROJECTION_START_YEAR = 2026
PROJECTION_END_YEAR = 2036


class CboBaselineFailure(RuntimeError):
    """Workbook, data-contract, or publication failure."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if not letters:
        raise CboBaselineFailure(f"非法 XLSX cell reference: {reference!r}")
    value = 0
    for char in letters.group(0):
        value = value * 26 + ord(char) - 64
    return value - 1


def _xlsx_text(node: ET.Element) -> str:
    return "".join(part.text or "" for part in node.iter()
                   if part.tag.endswith("}t"))


def read_xlsx(path: Path) -> tuple[list[str], dict[str, list[list[object]]]]:
    """Read cached XLSX values using only the Python standard library."""
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise CboBaselineFailure(f"无法读取 XLSX: {exc}") from exc
    with archive:
        try:
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [_xlsx_text(item) for item in shared_root]
        except KeyError:
            shared = []
        try:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships = ET.fromstring(
                archive.read("xl/_rels/workbook.xml.rels"))
        except (KeyError, ET.ParseError) as exc:
            raise CboBaselineFailure(f"XLSX workbook schema 损坏: {exc}") from exc

        rel_targets = {
            rel.attrib["Id"]: rel.attrib["Target"] for rel in relationships
        }
        sheets: list[tuple[str, str]] = []
        for sheet in workbook.iter():
            if not sheet.tag.endswith("}sheet"):
                continue
            relationship_id = next((value for key, value in sheet.attrib.items()
                                    if key.endswith("}id")), None)
            if not relationship_id or relationship_id not in rel_targets:
                raise CboBaselineFailure("XLSX sheet relationship 缺失")
            target = rel_targets[relationship_id]
            if target.startswith("/"):
                member = target.lstrip("/")
            else:
                member = str(PurePosixPath("xl") / target)
            sheets.append((sheet.attrib["name"], member))

        result: dict[str, list[list[object]]] = {}
        for name, member in sheets:
            try:
                root = ET.fromstring(archive.read(member))
            except (KeyError, ET.ParseError) as exc:
                raise CboBaselineFailure(f"XLSX sheet {name!r} 损坏: {exc}") from exc
            rows: list[list[object]] = []
            for row_node in (node for node in root.iter()
                             if node.tag.endswith("}row")):
                cells: dict[int, object] = {}
                for cell in (node for node in row_node
                             if node.tag.endswith("}c")):
                    index = _column_index(cell.attrib.get("r", ""))
                    kind = cell.attrib.get("t")
                    value_node = next((child for child in cell
                                       if child.tag.endswith("}v")), None)
                    if kind == "inlineStr":
                        value: object = _xlsx_text(cell)
                    elif value_node is None or value_node.text is None:
                        value = None
                    elif kind == "s":
                        try:
                            value = shared[int(value_node.text)]
                        except (ValueError, IndexError) as exc:
                            raise CboBaselineFailure(
                                f"XLSX shared string index 非法: {value_node.text}") from exc
                    elif kind == "b":
                        value = value_node.text == "1"
                    elif kind == "str":
                        value = value_node.text
                    else:
                        try:
                            number = float(value_node.text)
                        except ValueError as exc:
                            raise CboBaselineFailure(
                                f"XLSX 数值非法: {value_node.text!r}") from exc
                        value = int(number) if number.is_integer() else number
                    cells[index] = value
                if cells:
                    width = max(cells) + 1
                    row = [None] * width
                    for index, value in cells.items():
                        row[index] = value
                    rows.append(row)
                else:
                    rows.append([])
            result[name] = rows
        return [name for name, _ in sheets], result


def _cell(rows: list[list[object]], row: int, column: int):
    try:
        values = rows[row - 1]
    except IndexError as exc:
        raise CboBaselineFailure(f"Table 1-1 缺 row {row}") from exc
    return values[column - 1] if column - 1 < len(values) else None


def _finite(value, *, field: str) -> float:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value)):
        raise CboBaselineFailure(f"{field}: 不是有限数值: {value!r}")
    return float(value)


def normalize_primary_balance(value: float, source_sign: str) -> float:
    """Normalize to C17 semantics: surplus positive, deficit negative."""
    number = _finite(value, field="primary balance")
    if source_sign == "deficit_positive":
        if number < 0:
            raise CboBaselineFailure("deficit_positive 输入不得为负")
        return -number
    if source_sign == "deficit_negative":
        if number > 0:
            raise CboBaselineFailure("deficit_negative 输入不得为正")
        return number
    if source_sign == "balance_surplus_positive":
        return number
    raise CboBaselineFailure(f"未知 primary balance 符号口径: {source_sign}")


def _percent(value, *, field: str, low: float, high: float) -> float:
    number = _finite(value, field=field)
    if number < low or number > high:
        raise CboBaselineFailure(
            f"{field}: {number} 超出已审计百分数范围 [{low}, {high}]；"
            "拒绝 percent/fraction 或 schema 误读")
    return number


def _strict_row(rows: list[list[object]], row: int, label: str) -> list[float]:
    if _cell(rows, row, 1) != label:
        raise CboBaselineFailure(
            f"Table 1-1 A{row} 预期 {label!r}，得到 {_cell(rows, row, 1)!r}")
    return [_finite(_cell(rows, row, column), field=f"{label} {year}")
            for column, year in enumerate(EXPECTED_YEARS, start=2)]


def parse_table_1_1(rows: list[list[object]]) -> dict:
    title = _cell(rows, 5, 1)
    if not isinstance(title, str) or "Table 1-1." not in title \
            or "Baseline Budget Projections, by Category" not in title:
        raise CboBaselineFailure(f"Table 1-1 title 漂移: {title!r}")
    if _cell(rows, 2, 1) != "www.cbo.gov/publication/61882":
        raise CboBaselineFailure("Table 1-1 官方 publication anchor 漂移")
    years = [_cell(rows, 9, column) for column in range(2, 14)]
    if years != EXPECTED_YEARS or len(set(years)) != len(years):
        raise CboBaselineFailure(
            f"forecast year duplicate/missing/non-contiguous: {years!r}")
    actual_label = str(_cell(rows, 8, 2) or "").rstrip(",")
    if actual_label != "Actual" or any(_cell(rows, 8, column) is not None
                                       for column in range(3, 14)):
        raise CboBaselineFailure("actual/projected 分界漂移或重叠")
    if _cell(rows, 10, 2) != "In billions of dollars" \
            or _cell(rows, 34, 2) != "As a percentage of GDP":
        raise CboBaselineFailure("Table 1-1 单位锚点漂移")

    debt_bn = _strict_row(rows, 31, "Debt held by the public")
    nominal_gdp_bn = _strict_row(rows, 33, "GDP")
    receipts_pct = _strict_row(rows, 41, "Total")
    net_interest_pct = _strict_row(rows, 47, "Net interest")
    outlays_pct = _strict_row(rows, 48, "Total")
    overall_balance_pct = _strict_row(rows, 51, "Total deficit (-)")
    primary_official_pct = _strict_row(rows, 54, "Primary deficit (-)")
    debt_pct = _strict_row(rows, 55, "Debt held by the public")

    annual = []
    previous_debt = None
    previous_change = None
    previous_gdp = None
    for index, year in enumerate(years):
        debt_ratio = _percent(debt_pct[index], field=f"debt/GDP {year}",
                              low=50, high=250)
        receipts = _percent(receipts_pct[index], field=f"receipts/GDP {year}",
                            low=5, high=40)
        outlays = _percent(outlays_pct[index], field=f"outlays/GDP {year}",
                          low=5, high=50)
        interest = _percent(net_interest_pct[index],
                            field=f"net interest/GDP {year}", low=0, high=20)
        overall = _percent(overall_balance_pct[index],
                           field=f"overall balance/GDP {year}", low=-30, high=30)
        primary = normalize_primary_balance(primary_official_pct[index],
                                            "deficit_negative")
        if not -30 <= primary <= 30:
            raise CboBaselineFailure(f"primary balance/GDP {year} 单位异常: {primary}")
        debt_amount = _finite(debt_bn[index], field=f"public debt bn {year}")
        gdp_amount = _finite(nominal_gdp_bn[index], field=f"nominal GDP bn {year}")
        if not 1_000 <= debt_amount <= 500_000 or not 1_000 <= gdp_amount <= 500_000:
            raise CboBaselineFailure(f"{year}: billion/million 单位锚点失败")
        nominal_growth = None if previous_gdp is None else (
            gdp_amount / previous_gdp - 1) * 100
        debt_change = None if previous_debt is None else debt_ratio - previous_debt
        acceleration = None if previous_change is None or debt_change is None \
            else debt_change - previous_change
        annual.append({
            "year": year,
            "kind": "actual" if year <= ACTUAL_THROUGH_YEAR else "projection",
            "debt_held_by_public_pct_gdp": debt_ratio,
            "debt_held_by_public_bn": debt_amount,
            "nominal_gdp_bn": gdp_amount,
            "nominal_g_pct": nominal_growth,
            "primary_balance_pct_gdp": primary,
            "net_interest_pct_gdp": interest,
            "receipts_pct_gdp": receipts,
            "outlays_pct_gdp": outlays,
            "overall_balance_pct_gdp": overall,
            "debt_change_pp": debt_change,
            "debt_acceleration_pp": acceleration,
        })
        previous_gdp = gdp_amount
        previous_debt = debt_ratio
        previous_change = debt_change

    actual_years = [row["year"] for row in annual if row["kind"] == "actual"]
    projection_years = [row["year"] for row in annual
                        if row["kind"] == "projection"]
    if actual_years != [ACTUAL_THROUGH_YEAR] \
            or projection_years != list(range(PROJECTION_START_YEAR,
                                              PROJECTION_END_YEAR + 1)):
        raise CboBaselineFailure("actual/projected 分界 overlap/gap")

    projections = [row for row in annual if row["kind"] == "projection"]
    terminal = projections[-1]
    return {
        "annual": annual,
        "summary": {
            "baseline_debt_rising_years": [row["year"] for row in projections
                                            if row["debt_change_pp"] > 0],
            "baseline_primary_deficit_years": [row["year"] for row in projections
                                                if row["primary_balance_pct_gdp"] < 0],
            "terminal_year": terminal["year"],
            "terminal_debt_held_by_public_pct_gdp":
                terminal["debt_held_by_public_pct_gdp"],
            "terminal_net_interest_pct_gdp": terminal["net_interest_pct_gdp"],
            "terminal_primary_balance_pct_gdp": terminal["primary_balance_pct_gdp"],
            "debt_change_from_actual_through_pp":
                terminal["debt_held_by_public_pct_gdp"]
                - annual[0]["debt_held_by_public_pct_gdp"],
            "debt_change_over_projection_pp":
                terminal["debt_held_by_public_pct_gdp"]
                - projections[0]["debt_held_by_public_pct_gdp"],
        },
    }


def parse_workbook(path: Path, *, expected_sha256: str = SOURCE_FILE_SHA256,
                   expected_sheets: tuple[str, ...] = EXPECTED_SHEETS) -> dict:
    if not path.exists():
        raise CboBaselineFailure(f"缺少官方 workbook: {path}")
    source_hash = _sha256(path)
    if source_hash != expected_sha256:
        raise CboBaselineFailure(
            f"workbook SHA-256 不匹配: expected={expected_sha256}, actual={source_hash}")
    sheet_names, sheets = read_xlsx(path)
    if tuple(sheet_names) != expected_sheets:
        raise CboBaselineFailure(
            f"workbook sheet names 漂移: expected={list(expected_sheets)!r}, "
            f"actual={sheet_names!r}")
    parsed = parse_table_1_1(sheets["Table 1-1"])
    parsed["vintage"] = {
        "vintage_id": VINTAGE_ID,
        "publication_date": PUBLICATION_DATE,
        "downloaded_at": SOURCE_DOWNLOADED_AT,
        "title": TITLE,
        "source_url": SOURCE_PAGE_URL,
        "source_page_url": SOURCE_PAGE_URL,
        "source_file_url": SOURCE_FILE_URL,
        "source_file_name": path.name,
        "source_file_sha256": source_hash,
        "workbook_sheets": sheet_names,
        "table_title": _cell(sheets["Table 1-1"], 5, 1),
        "actual_through_year": ACTUAL_THROUGH_YEAR,
        "forecast_start_year": PROJECTION_START_YEAR,
        "forecast_end_year": PROJECTION_END_YEAR,
        "projection_start_year": PROJECTION_START_YEAR,
        "projection_end_year": PROJECTION_END_YEAR,
        "parser_version": CBO_PARSER_VERSION,
    }
    parsed["methodology"] = {
        "time_basis": "federal_fiscal_year",
        "primary_balance_sign": "surplus_positive_deficit_negative",
        "primary_balance_source": "CBO Table 1-1 Primary deficit (-)",
        "official_debt_baseline": "direct_cbo_table_not_frontend_or_model_reconstruction",
        "forward_fiscal_gap_available": False,
        "forward_fiscal_gap_reason": (
            "CBO workbook has market rates and net interest, but no interest-rate "
            "measure strictly bridged to C17 historical effective_r"
        ),
        "trajectory_labels": "descriptive_only_no_crisis_or_loss_of_control_year",
    }
    return parsed


def build_output(path: Path = SOURCE_PATH) -> dict:
    data = parse_workbook(path)
    return envelope(
        "cbo_budget_baseline", "annual", data,
        dates=[str(row["year"]) for row in data["annual"]], date_field="year",
        info=[
            "vintage_is_immutable_and_latest_updates_only_after_full_validation",
            "actual_and_projection_are_explicitly_separated",
            "primary_balance_positive_means_surplus",
            "official_cbo_debt_path_is_not_recomputed",
            "baseline_is_conditional_not_deterministic",
            "no_forward_fiscal_gap_no_crisis_year_no_scenario",
        ],
    )


def _comparable(payload: dict) -> tuple:
    keys = ("source", "freq", "date_field", "coverage", "derived_from",
            "warnings", "info", "data")
    return tuple(payload.get(key) for key in keys)


def publish(output: dict, *, vintage_path: Path = VINTAGE_PATH,
            latest_path: Path = LATEST_PATH, writer=atomic_write_json) -> None:
    assert_envelope(output)
    if output.get("source") != "cbo_budget_baseline" or output.get("freq") != "annual":
        raise CboBaselineFailure("CBO output source/freq 非法")
    existing_vintage = read_json_or(str(vintage_path), None)
    if existing_vintage is not None:
        try:
            assert_envelope(existing_vintage)
        except ValueError as exc:
            raise CboBaselineFailure(f"现有 vintage 不是合法信封: {exc}") from exc
        if _comparable(existing_vintage) != _comparable(output):
            raise CboBaselineFailure(
                f"拒绝覆盖 immutable vintage: {vintage_path.name}")
        canonical = existing_vintage
    else:
        writer(str(vintage_path), output, compact=False)
        canonical = output

    existing_latest = read_json_or(str(latest_path), None)
    if existing_latest is not None:
        try:
            assert_envelope(existing_latest)
        except ValueError as exc:
            raise CboBaselineFailure(f"现有 latest 不是合法信封: {exc}") from exc
        if _comparable(existing_latest) == _comparable(canonical):
            return
    writer(str(latest_path), canonical, compact=False)


def run_once(*, source_path: Path = SOURCE_PATH,
             vintage_path: Path = VINTAGE_PATH,
             latest_path: Path = LATEST_PATH,
             diagnostics_dir: Path = DIAGNOSTICS_DIR) -> int:
    sweep_stale_tmp(str(vintage_path))
    sweep_stale_tmp(str(latest_path))
    try:
        output = build_output(source_path)
        publish(output, vintage_path=vintage_path, latest_path=latest_path)
    except (CboBaselineFailure, ValueError, OSError) as exc:
        try:
            source_hash = _sha256(source_path) if source_path.exists() else None
        except OSError:
            source_hash = None
        try:
            diagnostic = quarantine_write(
                str(diagnostics_dir), "cbo-baseline", VINTAGE_ID,
                reason=[f"{type(exc).__name__}: {exc}"],
                payload={
                    "vintage_id": VINTAGE_ID,
                    "parser_version": CBO_PARSER_VERSION,
                    "source_file": source_path.name,
                    "source_file_exists": source_path.exists(),
                    "source_file_sha256": source_hash,
                    "expected_source_file_sha256": SOURCE_FILE_SHA256,
                    "source_url": SOURCE_FILE_URL,
                })
            print(f"CBO diagnostic: {diagnostic}")
        except (OSError, ValueError) as diagnostic_exc:
            print(f"WARNING CBO diagnostic 写入失败：{diagnostic_exc}")
        print(f"ERROR CBO baseline 未发布：{exc}；旧 latest/vintage 保持不变")
        return 1
    print(f"CBO baseline {VINTAGE_ID} 已验证：{PROJECTION_START_YEAR}.."
          f"{PROJECTION_END_YEAR}; vintage/latest 安全发布")
    return 0


def _fixture_table() -> list[list[object]]:
    rows = [[None] * 14 for _ in range(57)]
    def put(row, column, value):
        rows[row - 1][column - 1] = value
    put(2, 1, "www.cbo.gov/publication/61882")
    put(5, 1, "Table 1-1.\nCBO's Baseline Budget Projections, by Category")
    put(8, 2, "Actual,")
    for column, year in enumerate(EXPECTED_YEARS, start=2):
        put(9, column, year)
    put(10, 2, "In billions of dollars")
    put(34, 2, "As a percentage of GDP")
    fixtures = {
        31: ("Debt held by the public", [30_000 + i * 2_000 for i in range(12)]),
        33: ("GDP", [30_000 + i * 1_000 for i in range(12)]),
        41: ("Total", [17.5] * 12),
        47: ("Net interest", [3.0 + i * 0.1 for i in range(12)]),
        48: ("Total", [23.0 + i * 0.1 for i in range(12)]),
        51: ("Total deficit (-)", [-5.5] * 12),
        54: ("Primary deficit (-)", [-2.5] * 12),
        55: ("Debt held by the public", [99.0 + i * 2.0 for i in range(12)]),
    }
    for row, (label, values) in fixtures.items():
        put(row, 1, label)
        for column, value in enumerate(values, start=2):
            put(row, column, value)
    return rows


def _write_fixture_xlsx(path: Path, table: list[list[object]],
                        sheet_names=EXPECTED_SHEETS) -> None:
    relationships = []
    sheet_entries = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, name in enumerate(sheet_names, start=1):
            sheet_entries.append(
                f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>')
            relationships.append(
                f'<Relationship Id="rId{index}" '
                f'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                f'relationships/worksheet" Target="worksheets/sheet{index}.xml"/>')
            rows_xml = []
            matrix = table if name == "Table 1-1" else []
            for row_index, row in enumerate(matrix, start=1):
                cells = []
                for column_index, value in enumerate(row, start=1):
                    if value is None:
                        continue
                    serial = column_index
                    letters = ""
                    while serial:
                        serial, remainder = divmod(serial - 1, 26)
                        letters = chr(65 + remainder) + letters
                    reference = f"{letters}{row_index}"
                    if isinstance(value, str):
                        escaped = (value.replace("&", "&amp;").replace("<", "&lt;")
                                   .replace(">", "&gt;"))
                        cells.append(f'<c r="{reference}" t="inlineStr"><is><t>{escaped}'
                                     f'</t></is></c>')
                    else:
                        cells.append(f'<c r="{reference}"><v>{value}</v></c>')
                rows_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><sheetData>'
                + "".join(rows_xml) + '</sheetData></worksheet>')
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{"".join(sheet_entries)}</sheets></workbook>')
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(relationships) + '</Relationships>')


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

    table = _fixture_table()
    parsed = parse_table_1_1(table)
    check("official workbook schema fixture", len(parsed["annual"]) == 12)
    check("actual/projected 分界", parsed["annual"][0]["kind"] == "actual"
          and all(row["kind"] == "projection" for row in parsed["annual"][1:]))
    check("primary deficit 符号统一为 surplus-positive",
          parsed["annual"][1]["primary_balance_pct_gdp"] == -2.5
          and normalize_primary_balance(2.5, "deficit_positive") == -2.5)
    check("baseline 描述量不输出危机年份",
          parsed["summary"]["baseline_debt_rising_years"] == list(range(2026, 2037))
          and "crisis_year" not in parsed["summary"])

    fraction = json.loads(json.dumps(table))
    fraction[54][2] = 1.18
    try:
        parse_table_1_1(fraction)
        rejected = False
    except CboBaselineFailure:
        rejected = True
    check("118% 误读为 1.18% 必须拒绝", rejected)

    duplicate = json.loads(json.dumps(table))
    duplicate[8][3] = duplicate[8][2]
    try:
        parse_table_1_1(duplicate)
        rejected = False
    except CboBaselineFailure:
        rejected = True
    check("duplicate/missing forecast year 必须拒绝", rejected)

    overlap = json.loads(json.dumps(table))
    overlap[7][2] = "Projected"
    try:
        parse_table_1_1(overlap)
        rejected = False
    except CboBaselineFailure:
        rejected = True
    check("actual/projected 分界重叠必须拒绝", rejected)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workbook = root / "51118-2026-02-Budget-Projections.xlsx"
        _write_fixture_xlsx(workbook, table)
        fixture_hash = _sha256(workbook)
        output = parse_workbook(workbook, expected_sha256=fixture_hash)
        check("XLSX SHA-256 与 vintage metadata 锁定",
              output["vintage"]["source_file_sha256"] == fixture_hash)
        check("parser version 写入 vintage metadata",
              output["vintage"]["parser_version"] == CBO_PARSER_VERSION)
        check("人工 source artifact 下载时间写入 vintage metadata",
              output["vintage"]["downloaded_at"] == SOURCE_DOWNLOADED_AT)
        check("vintage source/horizon metadata 完整",
              output["vintage"]["source_url"] == SOURCE_PAGE_URL
              and output["vintage"]["forecast_start_year"] == PROJECTION_START_YEAR
              and output["vintage"]["forecast_end_year"] == PROJECTION_END_YEAR)
        broken = root / "broken.xlsx"
        _write_fixture_xlsx(broken, table,
                            tuple(name for name in EXPECTED_SHEETS
                                  if name != "Table 1-1"))
        try:
            parse_workbook(broken, expected_sha256=_sha256(broken))
            rejected = False
        except CboBaselineFailure:
            rejected = True
        check("required sheet 名变化必须 fail", rejected)

        # Publish tests use an envelope built from the already-audited fixture.
        payload = envelope(
            "cbo_budget_baseline", "annual", output,
            dates=[str(row["year"]) for row in output["annual"]], date_field="year")
        vintage = root / "data" / "cbo" / "baseline-2026-02.json"
        latest = root / "data" / "derived" / "cbo_baseline_latest.json"
        publish(payload, vintage_path=vintage, latest_path=latest)
        vintage_bytes = vintage.read_bytes()
        latest_bytes = latest.read_bytes()
        check("vintage 与 latest 完整成功后发布", vintage.exists() and latest.exists())
        publish(payload, vintage_path=vintage, latest_path=latest)
        check("相同 source 幂等且 generated_at 不刷新",
              vintage.read_bytes() == vintage_bytes and latest.read_bytes() == latest_bytes)

        changed = json.loads(json.dumps(payload))
        changed["data"]["annual"][-1]["debt_held_by_public_pct_gdp"] += 1
        try:
            publish(changed, vintage_path=vintage, latest_path=latest)
            rejected = False
        except CboBaselineFailure:
            rejected = True
        check("旧 vintage 永不覆盖", rejected and vintage.read_bytes() == vintage_bytes)

        newer_vintage = root / "data" / "cbo" / "baseline-new.json"
        old_latest = latest.read_bytes()
        def fail_latest(path, value, *, compact=True):
            if Path(path) == latest:
                raise OSError("injected latest failure")
            atomic_write_json(path, value, compact=compact)
        try:
            publish(changed, vintage_path=newer_vintage, latest_path=latest,
                    writer=fail_latest)
            failed_as_expected = False
        except OSError:
            failed_as_expected = True
        check("latest pointer 只在完整成功后更新",
              failed_as_expected and latest.read_bytes() == old_latest)

        diagnostics = root / "data" / "cbo" / "diagnostics"
        bad_source = root / "wrong-source.xlsx"
        bad_source.write_bytes(b"not an official workbook")
        vintage_before = vintage.read_bytes()
        latest_before = latest.read_bytes()
        failure_code = run_once(
            source_path=bad_source, vintage_path=vintage, latest_path=latest,
            diagnostics_dir=diagnostics)
        diagnostic_files = list(diagnostics.glob("cbo-baseline-*.json"))
        check("数据异常不发布且旧 latest/vintage 保持不变",
              failure_code == 1 and vintage.read_bytes() == vintage_before
              and latest.read_bytes() == latest_before)
        diagnostic_payload = (json.loads(diagnostic_files[0].read_text(encoding="utf-8"))
                              if len(diagnostic_files) == 1 else {})
        check("数据异常保存可追溯诊断",
              len(diagnostic_files) == 1
              and diagnostic_payload.get("vintage_id") == VINTAGE_ID
              and diagnostic_payload.get("source_file_sha256") == _sha256(bad_source)
              and diagnostic_payload.get("reason"))

    check("CBO forward Fiscal Gap 未强行构造",
          output["methodology"]["forward_fiscal_gap_available"] is False
          and not any("effective_r" in row for row in output["annual"]))

    workflow = (ROOT / ".github" / "workflows" / "update-cot.yml").read_text(
        encoding="utf-8")
    test_command = "python fetch_cbo_baseline.py --test"
    check("daily workflow 离线运行 CBO parser guard",
          workflow.count(test_command) == 1)
    check("daily workflow 不自动下载或发布 CBO vintage",
          "python fetch_cbo_baseline.py\n" not in workflow
          and "data/cbo/baseline-" not in workflow)
    check("daily workflow 不把 CBO latest 混入自动数据 commit",
          "data/derived/cbo_baseline_latest.json" not in workflow)
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
