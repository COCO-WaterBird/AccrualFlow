"""
Collection request generation and sending.

This is the **first** step of the monthly cycle: it produces the workbooks
that the departments have to fill in, sends them out and records the
outbound thread ids so that the replies can be correlated later.

The request template is derived from the frozen expected population, so
every department receives exactly the PO lines it owns.

Outbound email is guarded by ``ACCRUALFLOW_DRY_RUN``.  While that switch is
on, the requests are only prepared.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from backend.app.core.config import (
    CORRECTION_DIR,
    DRY_RUN,
    REPORTING_TIMEZONE,
    ensure_runtime_dirs,
)
from backend.app.services.submission_persistence_service import (
    _month_start,
    connect,
)


RESPONSE_DAYS = 7

HEADER_FILL = PatternFill("solid", fgColor="17365D")
HEADER_FONT = Font(color="FFFFFF", bold=True)
TITLE_FONT = Font(bold=True, size=12)

TEMPLATE_HEADERS = [
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


class CollectionRequestError(Exception):
    """Raised when a collection request cannot be prepared or sent."""


@dataclass
class DepartmentRequest:
    """One department's collection request."""

    reporting_month: str
    legal_entity: str
    department_id: str
    department_name: str
    cost_center: str
    recipient: str

    subject: str
    body: str
    workbook_path: str
    workbook_sha256: str
    due_date: str

    gmail_thread_id: str | None = None
    gmail_message_id: str | None = None
    dry_run: bool = True
    sent: bool = False
    error_message: str | None = None

    def to_dict(self) -> dict:
        return {
            "reporting_month": self.reporting_month,
            "legal_entity": self.legal_entity,
            "department_id": self.department_id,
            "department_name": self.department_name,
            "cost_center": self.cost_center,
            "recipient": self.recipient,
            "subject": self.subject,
            "body": self.body,
            "workbook_path": self.workbook_path,
            "workbook_sha256": self.workbook_sha256,
            "due_date": self.due_date,
            "gmail_thread_id": self.gmail_thread_id,
            "gmail_message_id": self.gmail_message_id,
            "dry_run": self.dry_run,
            "sent": self.sent,
            "error_message": self.error_message,
        }


# ---------------------------------------------------------------------------
# Template generation
# ---------------------------------------------------------------------------


def build_template_workbook(
    reporting_month: str,
    department_id: str,
    department_name: str,
    cost_center: str,
    rows: list,
    output_dir: Path | None = None,
) -> tuple[str, str]:
    """Build the empty completion workbook of one department.

    Returns ``(path, sha256)``.
    """
    ensure_runtime_dirs()

    base_dir = output_dir or CORRECTION_DIR
    month_dir = base_dir / reporting_month / "requests"
    month_dir.mkdir(parents=True, exist_ok=True)

    safe_department = _safe_component(cost_center or department_id)
    file_name = (
        f"{reporting_month}_{safe_department}_"
        f"{_safe_component(department_name)}_request.xlsx"
    )
    file_path = month_dir / file_name

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "PO Completion"

    worksheet.append(TEMPLATE_HEADERS)

    for row in rows:
        worksheet.append([
            row["po_number"],
            row["po_line"],
            row.get("requester"),
            row.get("cost_center") or cost_center,
            row.get("supplier_code"),
            row.get("supplier_name"),
            row.get("po_description"),
            row.get("currency"),
            float(row["po_line_amount"])
            if row.get("po_line_amount") is not None
            else None,
            float(row["cumulative_gr"])
            if row.get("cumulative_gr") is not None
            else None,
            float(row["previous_completion_pct"])
            if row.get("previous_completion_pct") is not None
            else None,
            None,
            None,
        ])

        # The completion column is percent formatted so that the department
        # fills in a plain number such as 45 and Excel renders it as 45%.
        worksheet.cell(row=worksheet.max_row, column=12).number_format = "0.00%"

        # The previous completion is a fraction rendered as a percent.
        worksheet.cell(row=worksheet.max_row, column=11).number_format = "0.00%"

    for cell in worksheet[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )

    widths = [16, 10, 22, 16, 16, 30, 40, 10, 16, 16, 20, 26, 40]
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions

    # The instruction sheet.
    instruction_sheet = workbook.create_sheet("Instructions")
    instruction_sheet.column_dimensions["A"].width = 110

    instructions = [
        f"CapEx Completion Update - {reporting_month}",
        "",
        f"Department: {department_name}",
        f"Cost Center: {cost_center}",
        f"PO lines to complete: {len(rows)}",
        "",
        "How to complete this workbook:",
        "1. Fill in the 'Current Estimated Completion %' column (column L).",
        "   The cell is already formatted as a percentage: type 45 or 45%.",
        "2. If the completion decreased compared with the previous month, "
        "add a short explanation in the 'Comments / Explanation' column "
        "(column M).",
        "3. Do not modify the other columns. They are the report as it was "
        "sent to you.",
        "4. Save the file and reply to this email with the workbook "
        "attached.",
        "",
    ]

    for index, line in enumerate(instructions, start=1):
        cell = instruction_sheet.cell(row=index, column=1, value=line)
        if index == 1:
            cell.font = TITLE_FONT

    handle, temp_name = tempfile.mkstemp(
        prefix=".request_", suffix=".xlsx", dir=str(month_dir)
    )
    os.close(handle)
    temp_path = Path(temp_name)

    try:
        workbook.save(temp_path)
        workbook.close()

        payload = temp_path.read_bytes()
        checksum = hashlib.sha256(payload).hexdigest()

        with open(temp_path, "rb") as src:
            with open(file_path, "xb") as dst:
                dst.write(src.read())

        size = file_path.stat().st_size
    except FileExistsError:
        payload = file_path.read_bytes()
        checksum = hashlib.sha256(payload).hexdigest()
        size = file_path.stat().st_size
    finally:
        temp_path.unlink(missing_ok=True)

    return str(file_path), checksum


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


def build_request_subject(reporting_month: str, department_name: str) -> str:
    """Build the collection request subject."""
    month_date = date.fromisoformat(f"{reporting_month}-01")
    label = month_date.strftime("%B %Y")

    return (
        f"Action Required - CapEx Completion Update - {label} - "
        f"{department_name}"
    ).strip()


def build_request_body(
    reporting_month: str,
    department_name: str,
    line_count: int,
    due_date: date,
) -> str:
    """Build the collection request body."""
    month_date = date.fromisoformat(f"{reporting_month}-01")
    label = month_date.strftime("%B %Y")
    due_date_label = due_date.strftime("%B %d, %Y")

    return "\n".join([
        f"Hi {department_name} team,",
        "",
        f"We are collecting the cumulative CapEx completion percentages for "
        f"{label}.",
        "",
        f"You have {line_count} open PO line(s) in your department. The "
        "attached workbook contains exactly those lines.",
        "",
        "Please fill in the 'Current Estimated Completion %' column and "
        "reply to this email with the completed workbook attached by "
        f"{due_date_label}.",
        "",
        "If a completion percentage decreased compared with the previous "
        "month, please add a short explanation in the "
        "'Comments / Explanation' column.",
        "",
        "Best regards,",
        "Finance Operations",
        "",
    ])


def prepare_request(
    reporting_month: str,
    legal_entity: str,
    department_id: str,
    department_name: str,
    cost_center: str,
    recipient: str,
    rows: list,
    output_dir: Path | None = None,
    dry_run: bool | None = None,
) -> DepartmentRequest:
    """Prepare one collection request. Nothing is sent."""
    if not recipient or "@" not in recipient:
        raise CollectionRequestError(
            f"Invalid recipient for {department_id}: {recipient!r}"
        )

    workbook_path, checksum = build_template_workbook(
        reporting_month,
        department_id,
        department_name,
        cost_center,
        rows,
        output_dir=output_dir,
    )

    timezone = ZoneInfo(REPORTING_TIMEZONE)
    today = datetime.now(timezone).date()
    due_date = today + timedelta(days=RESPONSE_DAYS)

    subject = build_request_subject(reporting_month, department_name)
    body = build_request_body(
        reporting_month,
        department_name,
        len(rows),
        due_date,
    )

    return DepartmentRequest(
        reporting_month=reporting_month,
        legal_entity=legal_entity,
        department_id=department_id,
        department_name=department_name,
        cost_center=cost_center,
        recipient=recipient,
        subject=subject,
        body=body,
        workbook_path=workbook_path,
        workbook_sha256=checksum,
        due_date=due_date.isoformat(),
        dry_run=DRY_RUN if dry_run is None else dry_run,
    )


def send_request(
    request: DepartmentRequest,
    gmail_service=None,
) -> DepartmentRequest:
    """Send a prepared collection request through the Gmail API."""
    if request.dry_run:
        raise CollectionRequestError(
            "DRY_RUN is enabled. Refusing to send a collection request."
        )

    if gmail_service is None:
        from backend.app.services.gmail_auth_service import get_gmail_service

        gmail_service = get_gmail_service()

    message = EmailMessage()
    message["To"] = request.recipient
    message["Subject"] = request.subject
    message.set_content(request.body)
    message.add_attachment(
        Path(request.workbook_path).read_bytes(),
        maintype="application",
        subtype=(
            "vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        filename=Path(request.workbook_path).name,
    )

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    try:
        result = gmail_service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()
    except Exception as exc:  # noqa: BLE001 - Gmail transport failure
        request.sent = False
        request.error_message = f"{type(exc).__name__}: {exc}"
        return request

    request.sent = True
    request.gmail_message_id = result.get("id")
    request.gmail_thread_id = result.get("threadId")
    request.error_message = None

    return request


def record_request(request: DepartmentRequest) -> int:
    """Record the outbound request so that the replies can be correlated."""
    month = _month_start(request.reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO email_requests (
                    reporting_month, legal_entity,
                    cost_center, department_id, department_name,
                    recipient, subject, body,
                    template_attachment_path, template_attachment_sha256,
                    gmail_message_id, gmail_thread_id,
                    status, sent_at, due_date
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (reporting_month, legal_entity, cost_center)
                DO UPDATE SET
                    recipient = EXCLUDED.recipient,
                    subject = EXCLUDED.subject,
                    body = EXCLUDED.body,
                    template_attachment_path = EXCLUDED.template_attachment_path,
                    template_attachment_sha256 = EXCLUDED.template_attachment_sha256,
                    gmail_message_id = EXCLUDED.gmail_message_id,
                    gmail_thread_id = EXCLUDED.gmail_thread_id,
                    status = EXCLUDED.status,
                    sent_at = EXCLUDED.sent_at,
                    due_date = EXCLUDED.due_date
                RETURNING request_id
                """,
                (
                    month,
                    request.legal_entity,
                    request.cost_center,
                    request.department_id,
                    request.department_name,
                    request.recipient,
                    request.subject,
                    request.body,
                    request.workbook_path,
                    request.workbook_sha256,
                    request.gmail_message_id,
                    request.gmail_thread_id,
                    "SENT" if request.sent else "DRY_RUN",
                    datetime.now(ZoneInfo(REPORTING_TIMEZONE))
                    if request.sent
                    else None,
                    date.fromisoformat(request.due_date),
                ),
            )
            return cur.fetchone()["request_id"]


def dispatch_collection_requests(
    reporting_month: str,
    legal_entity: str = "2000",
    send: bool | None = None,
) -> list:
    """Prepare and optionally send the collection request of every department.

    The expected population drives the department list, so a department that
    never replies is still visible in the status report.
    """
    month = _month_start(reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    e.department_id,
                    e.department_name,
                    e.cost_center,
                    COALESCE(e.authorized_email, e.department_owner)
                        AS authorized_email,
                    COUNT(*) AS line_count
                FROM expected_po_lines e
                WHERE e.reporting_month = %s
                  AND e.legal_entity = %s
                GROUP BY
                    e.department_id, e.department_name,
                    e.cost_center, e.authorized_email, e.department_owner
                ORDER BY e.department_id
                """,
                (month, legal_entity),
            )
            departments = list(cur.fetchall())

    results = []

    for department in departments:
        if not department["authorized_email"]:
            print(
                f"SKIP {department['department_id']}: no authorized contact."
            )
            continue

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        e.po_number,
                        e.po_line,
                        e.cost_center,
                        e.po_description,
                        e.po_line_amount,
                        e.cumulative_gr,
                        e.currency,
                        e.previous_completion_pct,
                        p.requester,
                        p.supplier_code,
                        p.supplier_name
                    FROM expected_po_lines e
                    LEFT JOIN LATERAL (
                        SELECT
                            cp.requester,
                            cp.supplier_code,
                            cp.supplier_name
                        FROM capex_po cp
                        WHERE cp.po_number = e.po_number
                          AND cp.po_line_number = e.po_line
                        LIMIT 1
                    ) p ON TRUE
                    WHERE e.reporting_month = %s
                      AND e.legal_entity = %s
                      AND e.department_id = %s
                    ORDER BY e.po_number, e.po_line
                    """,
                    (month, legal_entity, department["department_id"]),
                )
                rows = list(cur.fetchall())

        request = prepare_request(
            reporting_month=reporting_month,
            legal_entity=legal_entity,
            department_id=department["department_id"],
            department_name=department["department_name"],
            cost_center=department["cost_center"],
            recipient=department["authorized_email"],
            rows=rows,
        )

        should_send = (not request.dry_run) if send is None else send

        if should_send:
            request = send_request(request)

        record_request(request)

        results.append(request.to_dict())

        print(
            f"REQUEST {department['department_id']}: "
            f"{len(rows)} line(s), sent={request.sent}, "
            f"dry_run={request.dry_run}"
        )

    return results


def _safe_component(value: str) -> str:
    import re

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "")
    return cleaned.strip("._-") or "unknown"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", required=True, help="Reporting month YYYY-MM")
    parser.add_argument("--entity", default="2000")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually send the requests. Without it, only the drafts "
        "are prepared.",
    )
    args = parser.parse_args()

    dispatch_collection_requests(
        args.month,
        args.entity,
        send=args.send or None,
    )