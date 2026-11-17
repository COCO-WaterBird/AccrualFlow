"""
Excel submission parser for CapEx completion workbooks.

The parser is deliberately lossless.  For every PO line it records:

* the raw cell value,
* the raw Excel number format,
* the raw Excel data type,
* the source sheet name and source row number,

and it derives the canonical percentage through the deterministic
:mod:`backend.app.services.percentage_validation_service` module.  No LLM and
no silent repair takes place here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from backend.app.services.percentage_validation_service import (
    ColumnProfile,
    ExceptionCode,
    FormatStatus,
    PercentageInterpretation,
    build_column_profile,
    interpret_percentage,
)


SHEET_NAME = "PO Completion"

REQUIRED_COLUMNS = [
    "PO Number",
    "PO Line",
    "Cost Center",
    "PO Line Amount",
    "Cumulative GR",
    "Last Completion %",
    "Current Estimated Completion %",
    "Comments / Explanation",
]

# Columns inspected by the parser in addition to the required ones.
OPTIONAL_COLUMNS = [
    "Requester",
    "Supplier Code",
    "Supplier",
    "PO Description",
    "Currency",
]


class SubmissionParseError(Exception):
    """Raised when a workbook cannot be parsed at all."""


@dataclass
class ParsedLine:
    """One PO line as submitted, with its raw and normalized values."""

    source_sheet: str
    source_row: int

    po_number: str
    po_line: Any
    cost_center: str | None = None
    requester: str | None = None
    po_description: str | None = None
    supplier: str | None = None

    po_line_amount: Any = None
    cumulative_gr: Any = None

    previous_pct: Decimal | None = None
    """Last Completion % as written in the submitted file (not a baseline)."""

    raw_value: Any = None
    raw_number_format: str | None = None
    raw_data_type: str | None = None

    interpretation: PercentageInterpretation | None = None

    remarks: str | None = None

    def to_dict(self) -> dict:
        return {
            "source_sheet": self.source_sheet,
            "source_row": self.source_row,
            "po_number": self.po_number,
            "po_line": self.po_line,
            "cost_center": self.cost_center,
            "requester": self.requester,
            "po_description": self.po_description,
            "supplier": self.supplier,
            "po_line_amount": self.po_line_amount,
            "cumulative_gr": self.cumulative_gr,
            "previous_pct": (
                str(self.previous_pct)
                if self.previous_pct is not None
                else None
            ),
            "raw_value": self.raw_value,
            "raw_number_format": self.raw_number_format,
            "raw_data_type": self.raw_data_type,
            "remarks": self.remarks,
            "interpretation": (
                self.interpretation.to_dict() if self.interpretation else None
            ),
        }


@dataclass
class ParsedSubmission:
    """The full result of parsing one submitted workbook."""

    file_path: str
    file_name: str
    sheet_name: str
    headers: list[str] = field(default_factory=list)
    lines: list[ParsedLine] = field(default_factory=list)
    column_profile: ColumnProfile | None = None

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {status.value: 0 for status in FormatStatus}
        for line in self.lines:
            if line.interpretation is not None:
                counts[line.interpretation.status.value] += 1
        return counts


def _to_decimal(value: Any) -> Decimal | None:
    """Best-effort conversion to Decimal; returns None when not numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _load_workbook(file_path: Path):
    try:
        return load_workbook(file_path, read_only=False, data_only=False)
    except Exception as exc:  # noqa: BLE001 - corrupt workbook of any kind
        raise SubmissionParseError(
            f"Cannot read workbook {file_path.name}: {type(exc).__name__}"
        ) from exc


def _resolve_sheet(workbook, file_path: Path):
    if SHEET_NAME in workbook.sheetnames:
        return workbook[SHEET_NAME]

    if len(workbook.sheetnames) == 1:
        return workbook[workbook.sheetnames[0]]

    raise SubmissionParseError(
        f"Workbook {file_path.name} does not contain the expected "
        f"'{SHEET_NAME}' sheet."
    )


def parse_completion_workbook(file_path: str | Path) -> ParsedSubmission:
    """Parse a CapEx completion workbook into a :class:`ParsedSubmission`.

    Raises :class:`SubmissionParseError` when the workbook is unreadable or
    does not carry the required columns.
    """
    path = Path(file_path)

    if not path.is_file():
        raise SubmissionParseError(f"Workbook not found: {path}")

    if path.stat().st_size == 0:
        raise SubmissionParseError(f"Workbook is empty: {path.name}")

    workbook = _load_workbook(path)

    try:
        worksheet = _resolve_sheet(workbook, path)

        headers = [
            cell.value for cell in worksheet[1]
        ]

        header_names = [
            str(value).strip() if value is not None else ""
            for value in headers
        ]

        missing = [
            column for column in REQUIRED_COLUMNS if column not in header_names
        ]
        if missing:
            raise SubmissionParseError(
                f"Workbook {path.name} is missing required columns: {missing}"
            )

        column_index = {
            name: header_names.index(name)
            for name in REQUIRED_COLUMNS
            if name in header_names
        }

        current_index = column_index["Current Estimated Completion %"]
        current_column_letter = worksheet.cell(
            row=1, column=current_index + 1
        ).column_letter

        # ---------------------------------------------------------- profile
        profile_cells = []
        for row_number in range(2, worksheet.max_row + 1):
            cell = worksheet.cell(row=row_number, column=current_index + 1)
            profile_cells.append((cell.value, cell.number_format))

        column_profile = build_column_profile(profile_cells)

        # ------------------------------------------------------------ lines
        lines: list[ParsedLine] = []

        for row_number in range(2, worksheet.max_row + 1):
            row_values = [
                worksheet.cell(row=row_number, column=index + 1).value
                for index in range(len(header_names))
            ]

            po_number = _as_text(row_values[column_index["PO Number"]])
            po_line = row_values[column_index["PO Line"]]

            # Skip a completely empty row.
            if po_number is None and po_line is None:
                continue

            current_cell = worksheet.cell(
                row=row_number, column=current_index + 1
            )

            raw_value = current_cell.value
            raw_number_format = current_cell.number_format
            raw_data_type = current_cell.data_type

            interpretation = interpret_percentage(
                value=raw_value,
                number_format=raw_number_format,
                data_type=raw_data_type,
                column_profile=column_profile,
            )

            previous_raw = row_values[column_index["Last Completion %"]]

            line = ParsedLine(
                source_sheet=worksheet.title,
                source_row=row_number,
                po_number=po_number or "",
                po_line=po_line,
                cost_center=_as_text(row_values[column_index["Cost Center"]]),
                requester=(
                    _as_text(row_values[column_index["Requester"]])
                    if "Requester" in column_index
                    else None
                ),
                po_description=(
                    _as_text(row_values[column_index["PO Description"]])
                    if "PO Description" in column_index
                    else None
                ),
                supplier=(
                    _as_text(row_values[column_index["Supplier"]])
                    if "Supplier" in column_index
                    else None
                ),
                po_line_amount=row_values[column_index["PO Line Amount"]],
                cumulative_gr=row_values[column_index["Cumulative GR"]],
                previous_pct=_to_decimal(previous_raw),
                raw_value=raw_value,
                raw_number_format=raw_number_format,
                raw_data_type=raw_data_type,
                interpretation=interpretation,
                remarks=_as_text(
                    row_values[column_index["Comments / Explanation"]]
                ),
            )

            lines.append(line)

        return ParsedSubmission(
            file_path=str(path),
            file_name=path.name,
            sheet_name=worksheet.title,
            headers=header_names,
            lines=lines,
            column_profile=column_profile,
        )
    finally:
        workbook.close()


def parse_completion_excel(file_path: str | Path) -> list[dict]:
    """Backward compatible wrapper returning the legacy record shape.

    The legacy shape exposed a ``status`` key with the values VALID, MISSING,
    INVALID and REVIEW_REQUIRED.  That vocabulary is preserved here so that
    existing callers keep working, while the richer structured result is
    available through :func:`parse_completion_workbook`.
    """
    submission = parse_completion_workbook(file_path)

    records = []
    for line in submission.lines:
        interpretation = line.interpretation

        status = "MISSING"
        if interpretation is not None:
            if interpretation.status is FormatStatus.ACCEPTED:
                status = "VALID"
            elif interpretation.status is FormatStatus.MISSING:
                status = "MISSING"
            elif interpretation.status is FormatStatus.INVALID:
                status = "INVALID"
            else:
                status = "REVIEW_REQUIRED"

        records.append({
            "excel_row": line.source_row,
            "po_number": line.po_number,
            "po_line": line.po_line,
            "cost_center": line.cost_center,
            "po_line_amount": line.po_line_amount,
            "cumulative_gr": line.cumulative_gr,
            "last_completion": (
                float(line.previous_pct)
                if line.previous_pct is not None
                else None
            ),
            "current_completion": line.raw_value,
            "comment": line.remarks,
            "status": status,
        })

    return records


def detect_duplicate_po_lines(
    lines: list[ParsedLine],
) -> list[tuple[str, Any, list[int]]]:
    """Return PO lines that appear more than once in one workbook.

    Returns a list of ``(po_number, po_line, [row numbers])`` tuples.
    """
    seen: dict[tuple[str, Any], list[int]] = {}

    for line in lines:
        key = (line.po_number, line.po_line)
        seen.setdefault(key, []).append(line.source_row)

    return [
        (po_number, po_line, rows)
        for (po_number, po_line), rows in seen.items()
        if len(rows) > 1
    ]


def mark_duplicate_lines(
    lines: list[ParsedLine],
) -> dict[tuple[str, Any], list[int]]:
    """Attach a duplicate exception code to every duplicated PO line."""
    duplicates = detect_duplicate_po_lines(lines)

    for po_number, po_line, rows in duplicates:
        for line in lines:
            if line.source_row in rows and line.interpretation is not None:
                code = ExceptionCode.DUPLICATE_PO_LINE.value
                if code not in line.interpretation.exception_codes:
                    line.interpretation.exception_codes.append(code)

    return {
        (po_number, po_line): rows
        for po_number, po_line, rows in duplicates
    }


if __name__ == "__main__":
    import sys

    target = (
        sys.argv[1]
        if len(sys.argv) > 1
        else (
            "backend/output/reply_attachments/1a1c6bf760e71881/"
            "2026-11_2000_2000-100_Manufacturing_Engineering.xlsx"
        )
    )

    parsed = parse_completion_workbook(target)

    print("Workbook:", parsed.file_name)
    print("Total PO Lines:", parsed.line_count)
    print("Status Summary:", parsed.status_counts())
    print("Column Profile:", parsed.column_profile.to_dict())
