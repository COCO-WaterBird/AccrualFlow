"""
Correction workbook generation.

A correction workbook contains **only the unresolved PO lines** of one
department.  The already accepted lines are never requested again.

Sheets
------
``Correction_Detail``
    One row per unresolved PO line, with the originally submitted value,
    the previous completion, the issue code and two editable fields:
    the corrected completion percentage and the explanation.
``Exception_Detail``
    The unknown, duplicate or conflicting PO lines that cannot be edited
    inline.  They are never silently discarded.
``Instructions``
    The instructions that accompany the workbook.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from backend.app.core.config import CORRECTION_DIR, ensure_runtime_dirs
from backend.app.core.deterministic_xlsx import canonicalize_xlsx


HEADER_FILL = PatternFill("solid", fgColor="17365D")
HEADER_FONT = Font(color="FFFFFF", bold=True)
EDITABLE_FILL = PatternFill("solid", fgColor="FFF2CC")
TITLE_FONT = Font(bold=True, size=12)

DETAIL_HEADERS = [
    "PO Number",
    "PO Line",
    "PO Description",
    "Cost Center",
    "Previous Completion %",
    "Submitted Value (raw)",
    "Submitted Number Format",
    "Issue Code",
    "Issue Detail",
    "Corrected Completion %",
    "Corrected Explanation",
]

EXCEPTION_HEADERS = [
    "PO Number",
    "PO Line",
    "PO Description",
    "Cost Center",
    "Issue Code",
    "Issue Detail",
    "Submitted Value (raw)",
    "Submitted Number Format",
    "Source Row",
]


class CorrectionWorkbookError(Exception):
    """Raised when a correction workbook cannot be produced."""


@dataclass
class CorrectionRow:
    """One unresolved PO line that needs to be corrected."""

    po_number: str
    po_line: int
    po_description: str | None = None
    cost_center: str | None = None

    previous_pct: float | None = None
    """Previous finalized completion, rendered as a fraction."""

    submitted_raw_value: object = None
    submitted_number_format: str | None = None
    source_sheet: str | None = None
    source_row: int | None = None

    issue_codes: list[str] = field(default_factory=list)
    issue_detail: str | None = None
    remarks: str | None = None

    def to_dict(self) -> dict:
        return {
            "po_number": self.po_number,
            "po_line": self.po_line,
            "po_description": self.po_description,
            "cost_center": self.cost_center,
            "previous_pct": self.previous_pct,
            "submitted_raw_value": self.submitted_raw_value,
            "submitted_number_format": self.submitted_number_format,
            "source_sheet": self.source_sheet,
            "source_row": self.source_row,
            "issue_codes": list(self.issue_codes),
            "issue_detail": self.issue_detail,
            "remarks": self.remarks,
        }


@dataclass
class ExceptionRow:
    """A PO line that cannot be corrected inline."""

    po_number: str
    po_line: int
    po_description: str | None = None
    cost_center: str | None = None
    issue_codes: list[str] = field(default_factory=list)
    issue_detail: str | None = None
    submitted_raw_value: object = None
    submitted_number_format: str | None = None
    source_row: int | None = None


@dataclass
class CorrectionWorkbook:
    """The generated correction workbook and its metadata."""

    file_path: str
    file_name: str
    sha256: str
    size_bytes: int
    line_count: int
    exception_count: int

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "file_name": self.file_name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "line_count": self.line_count,
            "exception_count": self.exception_count,
        }


def _safe_filename_component(value: str) -> str:
    """Return a safe file name component."""
    import re

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "")
    return cleaned.strip("._-") or "unknown"


def generate_correction_workbook(
    reporting_month: str,
    department_id: str,
    department_name: str,
    cost_center: str | None,
    rows: list[CorrectionRow],
    exception_rows: list[ExceptionRow] | None = None,
    attempt: int = 1,
    output_dir: Path | None = None,
) -> CorrectionWorkbook:
    """Generate the correction-only workbook for one department.

    Raises :class:`CorrectionWorkbookError` when there is nothing to correct.
    """
    if not rows and not exception_rows:
        raise CorrectionWorkbookError(
            "No unresolved PO lines to correct for "
            f"{department_id} / {reporting_month}"
        )

    ensure_runtime_dirs()

    target_dir = output_dir or CORRECTION_DIR
    month_dir = target_dir / reporting_month
    month_dir.mkdir(parents=True, exist_ok=True)

    file_name = (
        f"{reporting_month}_"
        f"{_safe_filename_component(cost_center or department_id)}_"
        f"{_safe_filename_component(department_name)}_"
        f"correction_v{attempt}.xlsx"
    )
    file_path = month_dir / file_name

    workbook = Workbook()

    # ------------------------------------------------------ detail sheet
    worksheet = workbook.active
    worksheet.title = "Correction_Detail"

    worksheet.append(DETAIL_HEADERS)

    for row in rows:
        worksheet.append([
            row.po_number,
            row.po_line,
            row.po_description,
            row.cost_center,
            row.previous_pct,
            row.submitted_raw_value,
            row.submitted_number_format,
            ", ".join(row.issue_codes),
            row.issue_detail,
            None,
            row.remarks,
        ])

    _style_header(worksheet)

    last_row = max(2, worksheet.max_row)

    for index in range(2, last_row + 1):
        for column in (10, 11):
            worksheet.cell(index, column).fill = EDITABLE_FILL

        # Previous completion is a fraction rendered as a percent.
        worksheet.cell(index, 5).number_format = "0.00%"

        # The corrected completion cell is a percent-formatted input.
        worksheet.cell(index, 10).number_format = "0.00%"

    validation = DataValidation(
        type="decimal",
        operator="between",
        formula1=0,
        formula2=1,
        allow_blank=True,
    )
    validation.error = "Enter a percentage between 0% and 100%."
    validation.errorTitle = "Invalid Completion Percentage"
    validation.showErrorMessage = True
    worksheet.add_data_validation(validation)
    validation.add(f"J2:J{last_row}")

    _set_widths(worksheet, [18, 10, 40, 16, 20, 22, 22, 26, 50, 24, 40])
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions

    # -------------------------------------------------- exception sheet
    exceptions = exception_rows or []

    exception_sheet = workbook.create_sheet("Exception_Detail")
    exception_sheet.append(EXCEPTION_HEADERS)

    for row in exceptions:
        exception_sheet.append([
            row.po_number,
            row.po_line,
            row.po_description,
            row.cost_center,
            ", ".join(row.issue_codes),
            row.issue_detail,
            row.submitted_raw_value,
            row.submitted_number_format,
            row.source_row,
        ])

    _style_header(exception_sheet)
    _set_widths(exception_sheet, [18, 10, 40, 16, 26, 50, 22, 22, 12])
    exception_sheet.freeze_panes = "A2"

    # ----------------------------------------------- instruction sheet
    instruction_sheet = workbook.create_sheet("Instructions")
    instruction_sheet.column_dimensions["A"].width = 110

    instructions = [
        f"CapEx Completion Correction - {reporting_month}",
        "",
        f"Department: {department_name}",
        f"Cost Center: {cost_center or 'n/a'}",
        f"Reporting Period: {reporting_month}",
        f"Lines requiring correction: {len(rows)}",
        f"Additional exceptions to review: {len(exceptions)}",
        "",
        "How to complete this workbook:",
        "1. Open the 'Correction_Detail' sheet.",
        "2. Enter the corrected cumulative completion percentage in the "
        "'Corrected Completion %' column (column J).",
        "   The cell is formatted as a percentage: type 45% or 0.45.",
        "3. Enter a short explanation in the 'Corrected Explanation' column "
        "(column K).",
        "   An explanation is mandatory whenever the completion decreased.",
        "4. Do not modify any other column. The PO identity, the previously "
        "reported value and the issue code are read-only evidence.",
        "",
        "Important:",
        "- Only the PO lines listed here are being requested again. Every "
        "other line of the original report has already been accepted.",
        "- Leave a cell blank if the PO line is not applicable anymore and "
        "explain why in the explanation column.",
        "",
    ]

    for index, line in enumerate(instructions, start=1):
        cell = instruction_sheet.cell(row=index, column=1, value=line)
        if index == 1:
            cell.font = TITLE_FONT

    # ------------------------------------------------------- atomic write
    handle, temp_name = tempfile.mkstemp(
        prefix=".correction_", suffix=".xlsx", dir=str(month_dir)
    )
    os.close(handle)
    temp_path = Path(temp_name)

    try:
        workbook.save(temp_path)

        # Repackage the workbook with fixed timestamps so that the
        # checksum is a pure function of the exported content.
        payload = canonicalize_xlsx(temp_path)
        checksum = hashlib.sha256(payload).hexdigest()

        with open(file_path, "xb") as dst:
            dst.write(payload)

        size = file_path.stat().st_size
    except FileExistsError:
        # A workbook with the same name already exists. Keep the original.
        payload = file_path.read_bytes()
        checksum = hashlib.sha256(payload).hexdigest()
        size = file_path.stat().st_size
    finally:
        temp_path.unlink(missing_ok=True)
        workbook.close()

    return CorrectionWorkbook(
        file_path=str(file_path),
        file_name=file_name,
        sha256=checksum,
        size_bytes=size,
        line_count=len(rows),
        exception_count=len(exceptions),
    )


def _style_header(worksheet) -> None:
    for cell in worksheet[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")


def _set_widths(worksheet, widths: list[int]) -> None:
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width