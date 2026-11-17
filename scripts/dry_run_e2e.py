"""
End-to-end dry run of the AccrualFlow pipeline.

The script simulates a full reporting month:

* 87 expected PO lines for one department.
* A reply with 10 problems:
  - 3 missing completion values,
  - 3 ambiguous integer values (45 instead of 45%),
  - 2 decreases without an explanation,
  - 1 decrease with an explanation,
  - 1 unknown PO line.
* The department status and the Master Excel are produced at the end.

Nothing is sent.  The run is completely offline.
"""

import base64
import hashlib
import shutil
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import Workbook, load_workbook

from backend.app.services.completion_pipeline_service import (
    process_inbound_message,
)
from backend.app.services.correction_email_service import (
    prepare_correction_draft,
    write_dry_run_log,
)
from backend.app.services.correction_workbook_service import (
    CorrectionRow,
    ExceptionRow,
    generate_correction_workbook,
)
from backend.app.services.department_status_service import (
    evaluate_department,
    record_status,
)
from backend.app.services.master_excel_service import export_master_excel
from backend.app.services.submission_persistence_service import (
    ExpectedPopulationRow,
    build_accepted_dataset,
    connect,
    freeze_expected_population,
)


REPORTING_MONTH = "2098-11"
ENTITY = "2000"
DEPARTMENT_ID = "2000-100"
DEPARTMENT_NAME = "Manufacturing Engineering"
COST_CENTER = "2000-100"
CONTACT = "alex.chen@example.com"


def banner(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def reset() -> None:
    """Remove every row of the demo month."""
    month = f"{REPORTING_MONTH}-01"

    with connect() as conn:
        with conn.cursor() as cur:
            for statement in (
                "DELETE FROM validation_issues WHERE reporting_month = %s",
                "DELETE FROM submission_lines WHERE reporting_month = %s",
                "DELETE FROM department_submissions WHERE reporting_month = %s",
                "DELETE FROM correction_requests WHERE reporting_month = %s",
                "DELETE FROM department_status_history WHERE reporting_month = %s",
                "DELETE FROM master_export_runs WHERE reporting_month = %s",
                "DELETE FROM expected_po_lines WHERE reporting_month = %s",
                "DELETE FROM monthly_completion_baselines WHERE reporting_month = %s",
                "DELETE FROM reporting_periods WHERE reporting_month = %s",
            ):
                cur.execute(statement, (month,))

            cur.execute(
                """
                DELETE FROM email_attachments
                WHERE gmail_message_id IN (
                    SELECT gmail_message_id
                    FROM email_archives
                    WHERE reporting_month = %s
                )
                """,
                (month,),
            )
            cur.execute(
                "DELETE FROM email_archives WHERE reporting_month = %s",
                (month,),
            )
            cur.execute(
                "DELETE FROM gmail_message_processing WHERE reporting_month = %s",
                (month,),
            )


def seed_population() -> None:
    """Freeze the expected population of 87 PO lines."""
    population = []

    for index in range(1, 88):
        population.append(
            ExpectedPopulationRow(
                po_number=f"457{index:05d}",
                po_line=1,
                legal_entity=ENTITY,
                department_id=DEPARTMENT_ID,
                department_name=DEPARTMENT_NAME,
                cost_center=COST_CENTER,
                department_owner=CONTACT,
                authorized_email=CONTACT,
                po_description=f"Capital equipment line {index}",
                po_line_amount=Decimal("113350.00"),
                cumulative_gr=Decimal("34005.00"),
                currency="USD",
                previous_completion_pct=Decimal("0.40"),
                previous_baseline_period=None,
                baseline_status="PRESENT",
            )
        )

    frozen = freeze_expected_population(
        REPORTING_MONTH, population, source="Dry Run"
    )

    print(f"Frozen expected population: {frozen} PO lines")


def build_reply_workbook(path: Path) -> Path:
    """Build a reply with 10 problems across the 87 expected lines."""
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "PO Completion"

    worksheet.append([
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
    ])

    def add(po_number, raw_value, number_format, remarks=None):
        worksheet.append([
            po_number,
            1,
            "alex.chen",
            COST_CENTER,
            "SUP-200005",
            "Cognex Corporation",
            "Capital equipment line",
            "USD",
            113350.0,
            34005.0,
            0.4,
            raw_value,
            remarks,
        ])
        worksheet.cell(
            row=worksheet.max_row, column=12
        ).number_format = number_format

    # 76 clean lines: a percent formatted 45%.
    for index in range(1, 77):
        add(f"457{index:05d}", 0.45, "0%")

    # 3 missing values.
    for index in range(77, 80):
        add(f"457{index:05d}", None, "0%")

    # 3 ambiguous integers without percent formatting.
    for index in range(80, 83):
        add(f"457{index:05d}", 45, "General")

    # 2 decreases without an explanation.
    add("45700083", 0.30, "0%")
    add("45700084", 0.25, "0%")

    # 1 decrease with an explanation.
    add("45700085", 0.35, "0%", "Supplier rescheduled the installation.")

    # 1 unknown PO line.
    add("99999999", 0.50, "0%")

    workbook.save(path)
    return path


def fake_gmail_message(message_id: str, workbook_path: Path):
    payload = workbook_path.read_bytes()

    return {
        "id": message_id,
        "threadId": f"thread_{message_id}",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Re: CapEx Completion Update"},
                {"name": "From", "value": CONTACT},
                {"name": "To", "value": "finance@example.com"},
                {"name": "Date", "value": "Wed, 11 Nov 2026 09:00:00 +0000"},
                {"name": "In-Reply-To", "value": "<request@example.com>"},
            ],
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "",
                    "body": {"size": 10},
                },
                {
                    "mimeType": (
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                    "filename": workbook_path.name,
                    "body": {
                        "size": len(payload),
                        "attachmentId": "att_1",
                        "data": base64.urlsafe_b64encode(payload).decode("ascii"),
                    },
                },
            ],
        },
    }


def main() -> int:
    reset()
    seed_population()

    workdir = Path("/tmp/accrualflow_dry_run")
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    reply_path = build_reply_workbook(workdir / "reply.xlsx")
    message = fake_gmail_message("dry_run_msg_1", reply_path)

    banner("1. PROCESSING THE INBOUND REPLY")

    result = process_inbound_message(
        gmail_message_id="dry_run_msg_1",
        reporting_month=REPORTING_MONTH,
        raw_bytes=(
            b"-----BEGIN ORIGINAL RAW MESSAGE-----\r\n"
            b"Subject: Re: CapEx Completion Update\r\n\r\nBody\r\n"
            b"-----END-----"
        ),
        message=message,
        subject="Re: CapEx Completion Update",
        legal_entity=ENTITY,
    )

    print(f"Status              : {result.status}")
    print(f"Department          : {result.department_id}")
    print(f"Submission id       : {result.submission_id}")
    print(f"Version             : {result.version}")
    print(f"Expected lines      : {result.expected_lines}")
    print(f"Accepted lines      : {result.accepted_lines}")
    print(f"Unresolved lines    : {result.unresolved_lines}")
    print(f"Raw EML archived    : {result.eml_path}")
    print(f"Workbook archived   : {result.attachment_path}")

    banner("2. THE ARCHIVED EML IS THE ORIGINAL RAW PAYLOAD")

    eml_path = Path(result.eml_path)
    print(f"Exists              : {eml_path.exists()}")
    print(f"SHA-256             : {hashlib.sha256(eml_path.read_bytes()).hexdigest()}")
    print(f"File name           : {eml_path.name}")

    banner("3. THE VALIDATION EXCEPTIONS")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT exception_code, severity, COUNT(*)
                FROM validation_issues
                WHERE reporting_month = %s
                GROUP BY exception_code, severity
                ORDER BY exception_code
                """,
                (f"{REPORTING_MONTH}-01",),
            )
            for row in cur.fetchall():
                print(
                    f"  {row['exception_code']:<38} "
                    f"{row['severity']:<8} {row['count']}"
                )

    banner("4. THE CORRECTION WORKBOOK CONTAINS ONLY THE UNRESOLVED LINES")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    sl.reporting_month,
                    sl.legal_entity,
                    sl.department_id,
                    sl.po_number,
                    sl.po_line,
                    sl.previous_pct,
                    sl.raw_value,
                    sl.raw_number_format,
                    sl.source_row,
                    sl.validation_status,
                    sl.reason_status,
                    sl.exception_codes,
                    sl.remarks,
                    e.department_name,
                    e.cost_center,
                    e.po_description,
                    COALESCE(e.authorized_email, s.authorized_email)
                        AS authorized_email
                FROM submission_lines sl
                LEFT JOIN expected_po_lines e
                  ON e.reporting_month = sl.reporting_month
                 AND e.legal_entity   = sl.legal_entity
                 AND e.po_number      = sl.po_number
                 AND e.po_line        = sl.po_line
                    LEFT JOIN department_submissions ds
                      ON ds.submission_id = sl.submission_id
                    LEFT JOIN LATERAL (
                        SELECT c.authorized_email
                        FROM department_contacts c
                        WHERE c.legal_entity = sl.legal_entity
                          AND c.cost_center = ds.cost_center
                          AND c.active = TRUE
                        ORDER BY c.contact_id
                        LIMIT 1
                    ) s ON TRUE
                WHERE sl.reporting_month = %s
                  AND sl.is_current_version
                  AND sl.accepted_current_pct IS NULL
                ORDER BY sl.po_number
                """,
                (f"{REPORTING_MONTH}-01",),
            )
            unresolved = list(cur.fetchall())

    detail_rows = []
    exception_rows = []

    for row in unresolved:
        codes = list(row["exception_codes"] or [])
        editable = [
            code
            for code in codes
            if code
            not in (
                "UNKNOWN_PO_LINE",
                "DUPLICATE_PO_LINE",
                "DEPARTMENT_MISMATCH",
                "PERIOD_MISMATCH",
            )
        ]

        # A line that is not part of the expected population can never be
        # corrected inline.  It always goes to the exception sheet.
        is_unknown = (
            row["validation_status"] == "UNKNOWN_PO_LINE"
            or "UNKNOWN_PO_LINE" in codes
        )

        if editable and not is_unknown:
            detail_rows.append(
                CorrectionRow(
                    po_number=row["po_number"],
                    po_line=row["po_line"],
                    po_description=row["po_description"],
                    cost_center=COST_CENTER,
                    previous_pct=(
                        float(row["previous_pct"])
                        if row["previous_pct"] is not None
                        else None
                    ),
                    submitted_raw_value=row["raw_value"],
                    submitted_number_format=row["raw_number_format"],
                    issue_codes=codes,
                    issue_detail="; ".join(codes),
                    remarks=row["remarks"],
                )
            )
        else:
            exception_rows.append(
                ExceptionRow(
                    po_number=row["po_number"],
                    po_line=row["po_line"],
                    po_description=row["po_description"],
                    cost_center=COST_CENTER,
                    issue_codes=codes,
                    issue_detail="; ".join(codes),
                    submitted_raw_value=row["raw_value"],
                    submitted_number_format=row["raw_number_format"],
                )
            )

    workbook = generate_correction_workbook(
        reporting_month=REPORTING_MONTH,
        department_id=DEPARTMENT_ID,
        department_name=DEPARTMENT_NAME,
        cost_center=COST_CENTER,
        rows=detail_rows,
        exception_rows=exception_rows,
        attempt=1,
    )

    print(f"Correction workbook : {workbook.file_path}")
    print(f"Editable lines      : {workbook.line_count}")
    print(f"Exception lines     : {workbook.exception_count}")

    draft = prepare_correction_draft(
        reporting_month=REPORTING_MONTH,
        department_id=DEPARTMENT_ID,
        department_name=DEPARTMENT_NAME,
        cost_center=COST_CENTER,
        recipient=CONTACT,
        workbook_path=workbook.file_path,
        workbook_sha256=workbook.sha256,
        unresolved_count=workbook.line_count,
        exception_count=workbook.exception_count,
        attempt=1,
        dry_run=True,
    )

    log_path = write_dry_run_log(draft)

    print(f"Dry run draft       : {draft.draft_path}")
    print(f"Dry run log         : {log_path}")
    print("Emails sent         : 0 (DRY_RUN)")

    banner("5. THE DEPARTMENT STATUS")

    accepted_rows = build_accepted_dataset(REPORTING_MONTH, ENTITY)
    department_rows = [
        row for row in accepted_rows
        if row["department_id"] == DEPARTMENT_ID
    ]

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT exception_code, po_number, po_line
                FROM validation_issues
                WHERE reporting_month = %s
                  AND department_id = %s
                  AND status = 'OPEN'
                """,
                (f"{REPORTING_MONTH}-01", DEPARTMENT_ID),
            )
            open_issues = list(cur.fetchall())

    evaluation = evaluate_department(
        reporting_month=REPORTING_MONTH,
        legal_entity=ENTITY,
        department_id=DEPARTMENT_ID,
        accepted_rows=department_rows,
        open_issues=open_issues,
        latest_correction_status="PREPARED",
        has_evidence=True,
        master_export_succeeded=False,
    )

    record_status(evaluation)

    print(f"Status              : {evaluation.status}")
    print(f"Reason              : {evaluation.reason}")
    print(f"Expected            : {evaluation.expected_line_count}")
    print(f"Accepted            : {evaluation.accepted_line_count}")
    print(f"Missing             : {evaluation.missing_line_count}")
    print(f"Invalid             : {evaluation.invalid_line_count}")
    print(f"Format confirmation : {evaluation.format_confirmation_count}")
    print(f"Review required     : {evaluation.review_line_count}")

    banner("6. THE MASTER EXCEL")

    export = export_master_excel(REPORTING_MONTH, ENTITY)

    print(f"File                : {export.file_path}")
    print(f"SHA-256             : {export.sha256}")
    print(f"Expected lines      : {export.expected_line_count}")
    print(f"Accepted lines      : {export.accepted_line_count}")
    print(f"Exceptions          : {export.exception_count}")
    print(f"Departments         : {export.department_count}")

    master = load_workbook(export.file_path, read_only=True)
    print(f"Sheets              : {master.sheetnames}")
    master.close()

    banner("7. THE POWER BI VIEWS")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    department_id,
                    expected_line_count,
                    accepted_line_count,
                    missing_line_count,
                    invalid_line_count,
                    format_confirmation_count,
                    review_required_count,
                    line_acceptance_rate_pct,
                    department_status
                FROM vw_pbi_department_submission_overview
                WHERE reporting_month = %s
                """,
                (f"{REPORTING_MONTH}-01",),
            )
            for row in cur.fetchall():
                for key, value in row.items():
                    print(f"  {key:<32} {value}")
                print()

            cur.execute(
                """
                SELECT exception_code, COUNT(*)
                FROM vw_pbi_validation_exceptions
                WHERE reporting_month = %s
                GROUP BY exception_code
                ORDER BY exception_code
                """,
                (f"{REPORTING_MONTH}-01",),
            )
            print("  Exceptions reported to Power BI:")
            for row in cur.fetchall():
                print(f"    {row['exception_code']:<38} {row['count']}")

    banner("8. IDEMPOTENCY: THE SAME MESSAGE IS PROCESSED AGAIN")

    second = process_inbound_message(
        gmail_message_id="dry_run_msg_1",
        reporting_month=REPORTING_MONTH,
        raw_bytes=b"duplicate",
        message=message,
        subject="Re: CapEx Completion Update",
        legal_entity=ENTITY,
    )

    print(f"Status              : {second.status}")
    print(f"Submission id       : {second.submission_id}")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM department_submissions
                WHERE gmail_message_id = 'dry_run_msg_1'
                """
            )
            print(f"Submissions created : {cur.fetchone()['count']}")

    banner("DRY RUN COMPLETE - NO EMAIL WAS SENT")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())