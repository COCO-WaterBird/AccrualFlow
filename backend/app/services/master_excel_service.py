"""
Consolidated Master Excel export.

The workbook is published atomically: it is written to a temporary file in
the target directory, validated, and only then moved into place.  A failed
export never replaces a good published workbook.

Sheets
------
``Master_Data``
    Every expected PO line, including the unresolved ones.
``Validation_Exceptions``
    Missing values and lines, format ambiguity, invalid values, decreases
    requiring review, unmatched or duplicate lines, missing baselines.
``Department_Summary``
    Per department counts and the completion rate.
``Email_Audit_Log``
    One row per archived message with the full evidence chain.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import CellIsRule

from backend.app.core.config import (
    ACCRUALFLOW_DB_NAME,
    DB_HOST,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    REPORT_DIR,
    ensure_runtime_dirs,
)
from backend.app.core.deterministic_xlsx import write_canonical_xlsx
from backend.app.services.submission_persistence_service import _month_start


HEADER_FILL = PatternFill("solid", fgColor="17365D")
HEADER_FONT = Font(color="FFFFFF", bold=True)

MISSING_FILL = PatternFill("solid", fgColor="FCE4E4")
INVALID_FILL = PatternFill("solid", fgColor="FFF0E0")
REVIEW_FILL = PatternFill("solid", fgColor="FFF8D6")

MASTER_HEADERS = [
    "Reporting Month",
    "Entity",
    "Department",
    "Cost Center",
    "PO Number",
    "PO Line",
    "PO Description",
    "PO Line Amount",
    "Previous Completion %",
    "Current Completion % (accepted)",
    "Submitted Raw Value",
    "Submitted Number Format",
    "Change (pp)",
    "Remarks",
    "Validation Status",
    "Review Status",
    "Issue Codes",
    "Source Sheet",
    "Source Row",
    "Gmail Message ID",
    "Thread ID",
    "Attachment Path",
    "EML Archive Path",
    "Submission Version",
    "Updated At",
]

EXCEPTION_HEADERS = [
    "Reporting Month",
    "Entity",
    "Department",
    "Cost Center",
    "PO Number",
    "PO Line",
    "PO Description",
    "Issue Code",
    "Severity",
    "Detail",
    "Submitted Raw Value",
    "Submitted Number Format",
    "Suggested % (not accepted)",
    "Accepted %",
    "Previous %",
    "Change (pp)",
    "Remarks",
    "Review Status",
    "Status",
    "Detected At",
]

SUMMARY_HEADERS = [
    "Reporting Month",
    "Entity",
    "Department",
    "Cost Center",
    "Expected Lines",
    "Submitted Lines",
    "Accepted Lines",
    "Missing Lines",
    "Invalid Lines",
    "Format Confirmation Required",
    "Review Required Lines",
    "Completion Rate %",
    "Department Status",
    "Last Submission At",
    "Last Correction At",
    # The columns below make the scope of the population explicit.  A
    # department that was excluded from the frozen population is reported
    # here with the reason, so that finance can see exactly which
    # departments were NOT asked to report.
    "Population Status",
    "Exclusion Reason",
    "PO Lines in ERP",
]

AUDIT_HEADERS = [
    "Reporting Month",
    "Department",
    "Cost Center",
    "Subject",
    "Sender",
    "Recipient",
    "Message Date",
    "Gmail Message ID",
    "Thread ID",
    "EML Archive Path",
    "Attachment Path",
    "Attachment SHA-256",
    "Submission Version",
    "Processing Outcome",
]


class MasterExportError(Exception):
    """Raised when the Master Excel cannot be produced."""


@dataclass
class MasterExportResult:
    """Metadata of one published Master Excel workbook."""

    file_path: str
    file_name: str
    sha256: str
    size_bytes: int
    expected_line_count: int
    accepted_line_count: int
    exception_count: int
    department_count: int
    excluded_department_count: int = 0

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "file_name": self.file_name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "expected_line_count": self.expected_line_count,
            "accepted_line_count": self.accepted_line_count,
            "exception_count": self.exception_count,
            "department_count": self.department_count,
            "excluded_department_count": self.excluded_department_count,
        }


def _connect():
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=ACCRUALFLOW_DB_NAME,
        row_factory=dict_row,
    )


def _style_header(worksheet) -> None:
    for cell in worksheet[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )


def _set_widths(worksheet, widths: list[int]) -> None:
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width


def _percent_columns(worksheet, columns: list[int], last_row: int) -> None:
    for index in range(2, last_row + 1):
        for column in columns:
            worksheet.cell(index, column).number_format = "0.00%"


def build_master_workbook(
    reporting_month: str,
    legal_entity: str | None = None,
) -> tuple[Workbook, dict]:
    """Build the Master Excel workbook from the accepted dataset.

    Returns the workbook and a summary dictionary with the counts.
    """
    month = _month_start(reporting_month)

    with _connect() as conn:
        with conn.cursor() as cur:
            params: list = [month]
            entity_param: list = []

            if legal_entity:
                entity_param = [legal_entity]

            clause_e = (
                "AND e.legal_entity = %s" if legal_entity else ""
            )
            clause_vi = (
                "AND vi.legal_entity = %s" if legal_entity else ""
            )
            clause_ds = (
                "AND ds.legal_entity = %s" if legal_entity else ""
            )
            clause_h = (
                "AND h.legal_entity = %s" if legal_entity else ""
            )
            clause_pe = (
                "AND legal_entity = %s" if legal_entity else ""
            )

            cur.execute(
                f"""
                SELECT
                    e.reporting_month,
                    e.legal_entity,
                    e.department_id,
                    e.department_name,
                    e.cost_center,
                    e.po_number,
                    e.po_line,
                    e.po_description,
                    e.po_line_amount,
                    e.previous_completion_pct,
                    e.baseline_status,

                    sl.raw_value,
                    sl.raw_number_format,
                    sl.normalized_pct,
                    sl.suggested_pct,
                    sl.accepted_current_pct,
                    sl.change_percentage_points,
                    sl.remarks,
                    sl.validation_status,
                    sl.reason_status,
                    sl.exception_codes,
                    sl.source_sheet,
                    sl.source_row,
                    sl.accepted_at,

                    ds.gmail_message_id,
                    ds.gmail_thread_id,
                    ds.version,
                    ds.processing_status,
                    ds.source_attachment_path,

                    ea.eml_path
                FROM expected_po_lines e
                LEFT JOIN submission_lines sl
                  ON sl.reporting_month = e.reporting_month
                 AND sl.legal_entity   = e.legal_entity
                 AND sl.po_number      = e.po_number
                 AND sl.po_line        = e.po_line
                 AND sl.is_current_version
                LEFT JOIN department_submissions ds
                  ON ds.submission_id = sl.submission_id
                LEFT JOIN email_archives ea
                  ON ea.archive_id = ds.archive_id
                WHERE e.reporting_month = %s
                  {clause_e}
                ORDER BY
                    e.legal_entity, e.department_id,
                    e.po_number, e.po_line
                """,
                params + entity_param,
            )
            master_rows = list(cur.fetchall())

            cur.execute(
                f"""
                SELECT
                    vi.reporting_month,
                    vi.legal_entity,
                    vi.department_id,
                    e.department_name,
                    e.cost_center,
                    vi.po_number,
                    vi.po_line,
                    e.po_description,
                    vi.exception_code,
                    vi.severity,
                    vi.detail,
                    sl.raw_value,
                    sl.raw_number_format,
                    sl.suggested_pct,
                    sl.accepted_current_pct,
                    sl.previous_pct,
                    sl.change_percentage_points,
                    sl.remarks,
                    sl.reason_status,
                    vi.status,
                    vi.created_at
                FROM validation_issues vi
                LEFT JOIN submission_lines sl
                  ON sl.submission_line_id = vi.submission_line_id
                LEFT JOIN expected_po_lines e
                  ON e.reporting_month = vi.reporting_month
                 AND e.legal_entity   = vi.legal_entity
                 AND e.po_number      = vi.po_number
                 AND e.po_line        = vi.po_line
                WHERE vi.reporting_month = %s
                  {clause_vi}
                ORDER BY
                    vi.legal_entity, vi.department_id,
                    vi.po_number, vi.po_line, vi.exception_code
                """,
                params + entity_param,
            )
            exception_rows = list(cur.fetchall())

            cur.execute(
                f"""
                SELECT
                    ds.reporting_month,
                    ds.legal_entity,
                    ds.department_id,
                    ds.department_name,
                    ds.cost_center,
                    ds.version,
                    ds.processing_status,
                    ds.gmail_message_id,
                    ds.gmail_thread_id,
                    ds.source_attachment_path,
                    ds.source_attachment_sha256,
                    ds.submitted_at,
                    ea.subject,
                    ea.sender_address,
                    ea.recipient_address,
                    ea.message_date,
                    ea.eml_path
                FROM department_submissions ds
                LEFT JOIN email_archives ea
                  ON ea.archive_id = ds.archive_id
                WHERE ds.reporting_month = %s
                  {clause_ds}
                ORDER BY
                    ds.legal_entity, ds.department_id, ds.version
                """,
                params + entity_param,
            )
            audit_rows = list(cur.fetchall())

            cur.execute(
                f"""
                SELECT
                    h.reporting_month,
                    h.legal_entity,
                    h.department_id,
                    h.department_name,
                    h.cost_center,
                    h.status,
                    h.changed_at
                FROM department_status_history h
                WHERE h.reporting_month = %s
                  {clause_h}
                ORDER BY h.changed_at, h.status_id
                """,
                params + entity_param,
            )
            status_rows = list(cur.fetchall())

            # The departments that were excluded from the frozen
            # population.  They are added to the department summary so
            # that finance can see which departments were not asked to
            # report and why.
            cur.execute(
                f"""
                SELECT
                    reporting_month,
                    legal_entity,
                    cost_center,
                    department_name,
                    exclusion_reason,
                    po_line_count
                FROM population_exclusions
                WHERE reporting_month = %s
                  {clause_pe}
                ORDER BY cost_center, exclusion_reason
                """,
                params + entity_param,
            )
            exclusion_rows = list(cur.fetchall())

            # The submitted PO lines that are not part of the expected
            # population.  They are surfaced as exceptions and never
            # inflate the expected line count.
            cur.execute(
                """
                WITH current_lines AS (
                    SELECT DISTINCT ON (sl.po_number, sl.po_line)
                        sl.*
                    FROM submission_lines sl
                    WHERE sl.reporting_month = %s
                      AND sl.legal_entity = %s
                      AND sl.is_current_version
                    ORDER BY sl.po_number, sl.po_line, sl.submission_line_id
                )
                SELECT
                    sl.reporting_month,
                    sl.legal_entity,
                    sl.department_id,
                    sl.po_number,
                    sl.po_line,
                    sl.raw_value,
                    sl.raw_number_format,
                    sl.suggested_pct,
                    sl.accepted_current_pct,
                    sl.previous_pct,
                    sl.change_percentage_points,
                    sl.remarks,
                    sl.reason_status,
                    sl.validation_status,
                    sl.exception_codes,
                    sl.source_sheet,
                    sl.source_row,
                    ds.gmail_message_id,
                    ds.gmail_thread_id,
                    ds.version,
                    ds.source_attachment_path,
                    ea.eml_path
                FROM current_lines sl
                LEFT JOIN expected_po_lines e
                  ON e.reporting_month = sl.reporting_month
                 AND e.legal_entity   = sl.legal_entity
                 AND e.po_number      = sl.po_number
                 AND e.po_line        = sl.po_line
                LEFT JOIN department_submissions ds
                  ON ds.submission_id = sl.submission_id
                LEFT JOIN email_archives ea
                  ON ea.archive_id = ds.archive_id
                WHERE e.po_number IS NULL
                ORDER BY sl.po_number, sl.po_line
                """,
                (month, legal_entity or ""),
            )
            unknown_rows = list(cur.fetchall())

    # The summary is computed from the already loaded master rows so that
    # every department - including the ones that never replied - appears.
    summary = _build_summary(master_rows, status_rows)

    # The departments that were filtered out of the frozen population are
    # added as well, so that the report states the full scope.
    summary = _append_excluded_departments(summary, exclusion_rows)

    workbook = Workbook()

    # ---------------------------------------------------------- Master_Data
    worksheet = workbook.active
    worksheet.title = "Master_Data"
    worksheet.append(MASTER_HEADERS)

    for row in master_rows:
        worksheet.append([
            _as_text(row["reporting_month"]),
            row["legal_entity"],
            row["department_name"],
            row["cost_center"],
            row["po_number"],
            row["po_line"],
            row["po_description"],
            float(row["po_line_amount"])
            if row["po_line_amount"] is not None
            else None,
            float(row["previous_completion_pct"])
            if row["previous_completion_pct"] is not None
            else None,
            float(row["accepted_current_pct"])
            if row["accepted_current_pct"] is not None
            else None,
            row["raw_value"],
            row["raw_number_format"],
            float(row["change_percentage_points"])
            if row["change_percentage_points"] is not None
            else None,
            row["remarks"],
            row["validation_status"],
            row["reason_status"],
            ", ".join(row["exception_codes"] or []),
            row["source_sheet"],
            row["source_row"],
            row["gmail_message_id"],
            row["gmail_thread_id"],
            row["source_attachment_path"],
            row["eml_path"],
            row["version"],
            _as_text(row["accepted_at"]),
        ])

    _style_header(worksheet)
    master_last_row = max(2, worksheet.max_row)
    _percent_columns(worksheet, [9, 10], master_last_row)
    _set_widths(worksheet, [16, 10, 26, 16, 16, 10, 38, 16, 20, 22, 18, 20, 14, 40, 26, 26, 34, 16, 12, 22, 22, 44, 52, 18, 22])
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions

    # Conditional formatting on the validation status column.
    status_column = get_column_letter(MASTER_HEADERS.index("Validation Status") + 1)
    worksheet.conditional_formatting.add(
        f"{status_column}2:{status_column}{master_last_row}",
        CellIsRule(
            operator="equal",
            formula=['"MISSING"'],
            fill=MISSING_FILL,
        ),
    )
    worksheet.conditional_formatting.add(
        f"{status_column}2:{status_column}{master_last_row}",
        CellIsRule(
            operator="equal",
            formula=['"INVALID"'],
            fill=INVALID_FILL,
        ),
    )

    # ------------------------------------------------ Validation_Exceptions
    exception_sheet = workbook.create_sheet("Validation_Exceptions")
    exception_sheet.append(EXCEPTION_HEADERS)

    for row in exception_rows:
        exception_sheet.append([
            _as_text(row["reporting_month"]),
            row["legal_entity"],
            row["department_name"],
            row["cost_center"],
            row["po_number"],
            row["po_line"],
            row["po_description"],
            row["exception_code"],
            row["severity"],
            row["detail"],
            row["raw_value"],
            row["raw_number_format"],
            float(row["suggested_pct"])
            if row["suggested_pct"] is not None
            else None,
            float(row["accepted_current_pct"])
            if row["accepted_current_pct"] is not None
            else None,
            float(row["previous_pct"])
            if row["previous_pct"] is not None
            else None,
            float(row["change_percentage_points"])
            if row["change_percentage_points"] is not None
            else None,
            row["remarks"],
            row["reason_status"],
            row["status"],
            _as_text(row["created_at"]),
        ])

    # The PO lines that are not part of the expected population are
    # reported here so that they are never silently dropped.
    for row in unknown_rows:
        for code in row["exception_codes"] or []:
            exception_sheet.append([
                _as_text(row["reporting_month"]),
                row["legal_entity"],
                row["department_id"],
                None,
                row["po_number"],
                row["po_line"],
                None,
                code,
                "WARNING",
                "The submitted PO line is not part of the expected population.",
                row["raw_value"],
                row["raw_number_format"],
                float(row["suggested_pct"])
                if row["suggested_pct"] is not None
                else None,
                float(row["accepted_current_pct"])
                if row["accepted_current_pct"] is not None
                else None,
                float(row["previous_pct"])
                if row["previous_pct"] is not None
                else None,
                float(row["change_percentage_points"])
                if row["change_percentage_points"] is not None
                else None,
                row["remarks"],
                row["reason_status"],
                "OPEN",
                _as_text(row["source_row"]),
            ])

    _style_header(exception_sheet)
    exception_last_row = max(2, exception_sheet.max_row)
    _percent_columns(exception_sheet, [13, 14, 15], exception_last_row)
    _set_widths(exception_sheet, [16, 10, 26, 16, 16, 10, 38, 28, 12, 52, 18, 20, 22, 16, 16, 14, 40, 26, 14, 22])
    exception_sheet.freeze_panes = "A2"
    exception_sheet.auto_filter.ref = exception_sheet.dimensions

    # ----------------------------------------------------- Department_Summary
    summary_sheet = workbook.create_sheet("Department_Summary")
    summary_sheet.append(SUMMARY_HEADERS)

    for entry in summary:
        worksheet_values = entry["values"]
        summary_sheet.append(worksheet_values)

    _style_header(summary_sheet)
    summary_last_row = max(2, summary_sheet.max_row)
    _set_widths(summary_sheet, [16, 10, 26, 16, 16, 16, 16, 14, 14, 26, 20, 18, 24, 22, 22])
    summary_sheet.freeze_panes = "A2"
    summary_sheet.auto_filter.ref = summary_sheet.dimensions

    # ------------------------------------------------------ Email_Audit_Log
    audit_sheet = workbook.create_sheet("Email_Audit_Log")
    audit_sheet.append(AUDIT_HEADERS)

    for row in audit_rows:
        audit_sheet.append([
            _as_text(row["reporting_month"]),
            row["department_name"],
            row["cost_center"],
            row["subject"],
            row["sender_address"],
            row["recipient_address"],
            _as_text(row["message_date"]),
            row["gmail_message_id"],
            row["gmail_thread_id"],
            row["eml_path"],
            row["source_attachment_path"],
            row["source_attachment_sha256"],
            row["version"],
            row["processing_status"],
        ])

    _style_header(audit_sheet)
    audit_last_row = max(2, audit_sheet.max_row)
    _set_widths(audit_sheet, [16, 26, 16, 46, 30, 30, 22, 22, 22, 52, 52, 34, 18, 20])
    audit_sheet.freeze_panes = "A2"
    audit_sheet.auto_filter.ref = audit_sheet.dimensions

    stats = {
        "expected_line_count": len(master_rows),
        "accepted_line_count": sum(
            1
            for row in master_rows
            if row["accepted_current_pct"] is not None
        ),
        "exception_count": len(exception_rows) + len(unknown_rows),
        # Only the departments that are part of the frozen population
        # count as departments of this report.  The excluded departments
        # are listed in the summary sheet for transparency, but they were
        # never asked to report and therefore never contribute a line.
        "department_count": len(summary) - len(exclusion_rows),
        "excluded_department_count": len(exclusion_rows),
    }

    return workbook, stats


def _build_summary(master_rows: list, status_rows: list | None = None) -> list:
    """Compute the per department summary from the master rows."""
    departments: dict = {}

    for row in master_rows:
        key = (row["legal_entity"], row["department_id"])
        entry = departments.setdefault(
            key,
            {
                "reporting_month": _as_text(row["reporting_month"]),
                "legal_entity": row["legal_entity"],
                "department_name": row["department_name"],
                "cost_center": row["cost_center"],
                "expected": 0,
                "accepted": 0,
                "missing": 0,
                "invalid": 0,
                "format": 0,
                "review": 0,
            },
        )

        entry["expected"] += 1

        if row["accepted_current_pct"] is not None:
            entry["accepted"] += 1

        if row["validation_status"] == "MISSING":
            entry["missing"] += 1
        elif row["validation_status"] == "INVALID":
            entry["invalid"] += 1

        if row["validation_status"] == "FORMAT_CONFIRMATION_REQUIRED":
            entry["format"] += 1

        if row["reason_status"] in (
            "DECREASE_REASON_MISSING",
            "DECREASE_REASON_PROVIDED",
            "REVIEW_REQUIRED",
        ):
            entry["review"] += 1

    # The latest department status wins.
    latest_status: dict = {}
    latest_change: dict = {}

    for row in status_rows or []:
        key = (row["legal_entity"], row["department_id"])
        latest_status[key] = row["status"]
        latest_change[key] = _as_text(row["changed_at"])

    summary = []

    for key in sorted(departments):
        entry = departments[key]
        rate = (
            round(100.0 * entry["accepted"] / entry["expected"], 2)
            if entry["expected"]
            else 0.0
        )
        summary.append({
            "values": [
                entry["reporting_month"],
                entry["legal_entity"],
                entry["department_name"],
                entry["cost_center"],
                entry["expected"],
                entry["accepted"],
                entry["accepted"],
                entry["missing"],
                entry["invalid"],
                entry["format"],
                entry["review"],
                rate,
                latest_status.get(key, "NOT_SUBMITTED"),
                latest_change.get(key),
                None,
                "INCLUDED",
                None,
                None,
            ],
            "entity": entry["legal_entity"],
            "department_id": key[1],
        })

    return summary


def _append_excluded_departments(
    summary: list,
    exclusions: list,
) -> list:
    """Add the excluded departments to the department summary.

    A department that was filtered out of the frozen population never
    appears in the master data, so without this it would simply be
    missing from the report.  Finance has to be able to see that the
    department exists, that it was deliberately left out, and why.
    """
    for row in exclusions:
        summary.append({
            "values": [
                _as_text(row["reporting_month"]),
                row["legal_entity"],
                row["department_name"],
                row["cost_center"],
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0.0,
                "EXCLUDED_FROM_POPULATION",
                None,
                None,
                "EXCLUDED",
                row["exclusion_reason"],
                row["po_line_count"],
            ],
            "entity": row["legal_entity"],
            "department_id": row["cost_center"],
        })

    return summary


def _as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def export_master_excel(
    reporting_month: str,
    legal_entity: str | None = None,
    output_dir: Path | None = None,
) -> MasterExportResult:
    """Publish the Master Excel workbook atomically.

    The workbook is written to a temporary file, re-opened and validated,
    and only then moved into the published location.
    """
    ensure_runtime_dirs()

    workbook, stats = build_master_workbook(reporting_month, legal_entity)

    target_dir = output_dir or REPORT_DIR
    month_dir = target_dir / reporting_month
    month_dir.mkdir(parents=True, exist_ok=True)

    file_name = f"CapEx_Completion_Master_{reporting_month}.xlsx"
    file_path = month_dir / file_name

    handle, temp_name = tempfile.mkstemp(
        prefix=".master_", suffix=".xlsx", dir=str(month_dir)
    )
    os.close(handle)
    temp_path = Path(temp_name)

    try:
        workbook.save(temp_path)
        workbook.close()

        # Validate the generated workbook before publishing it.
        _validate_workbook(temp_path)

        # Repackage the workbook with fixed timestamps so that the
        # checksum is a pure function of the exported content.  The
        # published file is the canonical repackaging, so its checksum can
        # be reproduced with a plain sha256sum.
        payload = write_canonical_xlsx(temp_path)
        checksum = hashlib.sha256(payload).hexdigest()

        shutil.move(str(temp_path), str(file_path))
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    size = file_path.stat().st_size

    result = MasterExportResult(
        file_path=str(file_path),
        file_name=file_name,
        sha256=checksum,
        size_bytes=size,
        expected_line_count=stats["expected_line_count"],
        accepted_line_count=stats["accepted_line_count"],
        exception_count=stats["exception_count"],
        department_count=stats["department_count"],
        excluded_department_count=stats["excluded_department_count"],
    )

    _record_export_run(reporting_month, result)

    return result


def _validate_workbook(path: Path) -> None:
    """Re-open the generated workbook and verify its four sheets."""
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise MasterExportError(
            f"The generated workbook cannot be read back: {type(exc).__name__}"
        ) from exc

    required = {
        "Master_Data",
        "Validation_Exceptions",
        "Department_Summary",
        "Email_Audit_Log",
    }

    try:
        missing = required - set(workbook.sheetnames)
        if missing:
            raise MasterExportError(
                f"The generated workbook is missing sheets: {sorted(missing)}"
            )

        worksheet = workbook["Master_Data"]
        if worksheet.max_row < 1:
            raise MasterExportError("Master_Data sheet is empty")

        header = [
            cell.value
            for cell in next(worksheet.iter_rows(min_row=1, max_row=1))
        ]
        if "PO Number" not in header:
            raise MasterExportError(
                "Master_Data sheet does not contain the expected header row"
            )
    finally:
        workbook.close()


def _record_export_run(reporting_month: str, result: MasterExportResult) -> None:
    """Record the export outcome in PostgreSQL."""
    import psycopg

    with psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=ACCRUALFLOW_DB_NAME,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO master_export_runs (
                    reporting_month, file_name, file_path,
                    file_sha256, file_size_bytes,
                    expected_line_count, accepted_line_count,
                    exception_count, department_count, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'SUCCESS')
                """,
                (
                    _month_start(reporting_month),
                    result.file_name,
                    result.file_path,
                    result.sha256,
                    result.size_bytes,
                    result.expected_line_count,
                    result.accepted_line_count,
                    result.exception_count,
                    result.department_count,
                ),
            )


def record_failed_export(
    reporting_month: str,
    error_message: str,
    legal_entity: str | None = None,
) -> None:
    """Record a failed export attempt."""
    import psycopg

    with psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=ACCRUALFLOW_DB_NAME,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO master_export_runs (
                    reporting_month, file_name, file_path,
                    file_sha256, file_size_bytes, status, error_message
                )
                VALUES (%s, %s, %s, '', 0, 'FAILED', %s)
                """,
                (
                    _month_start(reporting_month),
                    f"CapEx_Completion_Master_{reporting_month}.xlsx",
                    "",
                    error_message[:2000],
                ),
            )