
from collections import defaultdict
from datetime import date
from pathlib import Path
import os
import re

import psycopg
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.utils import get_column_letter

from backend.app.core.config import (
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD
)


OUTPUT_DIR = Path("backend/output/reminders")

HEADERS = [
    "PO Number",
    "PO Line",
    "Requester",
    "Cost Center",
    "Supplier Code",
    "Supplier",
    "PO Description",
    "Currency",
    "PO Line Amount",
    "Cumulative GR",
    "Last Completion %",
    "Current Estimated Completion %",
    "Comments / Explanation",
]


def safe_filename(value):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value)


def fetch_reminder_data():
    with psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname="accrualflow_db",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    p.legal_entity,
                    m.manager,
                    m.cost_center,
                    m.department,
                    p.po_number,
                    p.po_line_number,
                    p.requester,
                    p.supplier_code,
                    p.supplier_name,
                    p.description,
                    p.currency,
                    p.po_line_amount,
                    p.cumulative_gr_amount,
                    p.completion_progress
                FROM vw_po_reminder_list p
                JOIN costcenter_mapping m
                  ON p.legal_entity = m.legal_entity
                 AND p.requester = m.employee_email_prefix
                 AND p.cost_center = m.cost_center
                ORDER BY
                    m.manager,
                    m.cost_center,
                    p.po_number,
                    p.po_line_number
            """)
            return cur.fetchall()


def generate_reminder_excels(reporting_month=None):
    if reporting_month is None:
        reporting_month = date.today().strftime("%Y-%m")

    rows = fetch_reminder_data()

    groups = defaultdict(list)

    for row in rows:
        legal_entity, manager, cost_center, department, *po_data = row
        groups[
            (legal_entity, manager, cost_center, department)
        ].append(po_data)

    output_path = OUTPUT_DIR / reporting_month
    output_path.mkdir(parents=True, exist_ok=True)

    for (entity, manager, cost_center, department), po_lines in groups.items():
        wb = Workbook()
        ws = wb.active
        ws.title = "PO Completion"

        ws.append(HEADERS)

        for po in po_lines:
            (
                po_number,
                po_line,
                requester,
                supplier_code,
                supplier,
                description,
                currency,
                amount,
                gr_amount,
                completion,
            ) = po

            ws.append([
                po_number,
                po_line,
                requester,
                cost_center,
                supplier_code,
                supplier,
                description,
                currency,
                float(amount) if amount is not None else None,
                float(gr_amount) if gr_amount is not None else None,
                float(completion) if completion is not None else None,
                None,
                None,
            ])

        # Header formatting
        for cell in ws[1]:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = PatternFill(
                "solid", fgColor="17365D"
            )
            cell.alignment = Alignment(
                horizontal="center"
            )

        # Highlight editable columns
        for row_idx in range(2, ws.max_row + 1):
            for col_idx in (12, 13):
                ws.cell(row_idx, col_idx).fill = PatternFill(
                    "solid", fgColor="FFF2CC"
                )

            for col_idx in (11, 12):
                ws.cell(row_idx, col_idx).number_format = "0%"

            for col_idx in (9, 10):
                ws.cell(row_idx, col_idx).number_format = '#,##0.00'

        # Completion % validation: enter 0% to 100%
        validation = DataValidation(
            type="decimal",
            operator="between",
            formula1=0,
            formula2=1,
            allow_blank=True,
        )
        validation.error = "Enter a value between 0% and 100%."
        validation.errorTitle = "Invalid Completion Percentage"
        validation.showErrorMessage = True
        ws.add_data_validation(validation)
        validation.add(f"L2:L{max(2, ws.max_row)}")

        # Column widths
        widths = [18, 12, 22, 16, 18, 25, 45, 12, 20, 20, 22, 30, 40]
        for idx, width in enumerate(widths, start=1):
            ws.column_dimensions[
                get_column_letter(idx)
            ].width = width

        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

        filename = (
            f"{reporting_month}_"
            f"{safe_filename(entity)}_"
            f"{safe_filename(cost_center)}_"
            f"{safe_filename(department)}.xlsx"
        )

        filepath = output_path / filename
        wb.save(filepath)

        print(
            f"Generated: {filename} "
            f"({len(po_lines)} PO lines) -> {manager}"
        )

    print(
        f"\nTotal: {len(groups)} Excel files, "
        f"{len(rows)} PO lines"
    )


if __name__ == "__main__":
    generate_reminder_excels()
