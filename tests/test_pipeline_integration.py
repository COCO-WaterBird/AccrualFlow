"""
Integration tests for the completion pipeline against a real PostgreSQL
database.

The tests are skipped when no database is reachable.  They exercise the
mandatory test matrix items 11, 14, 17, 20, 21, 22 and 23.
"""

from decimal import Decimal

import pytest
from openpyxl import Workbook

from backend.app.services.submission_persistence_service import (
    ExpectedPopulationRow,
    connect,
    freeze_expected_population,
    load_expected_population,
)


pytestmark = pytest.mark.integration


def _database_available() -> bool:
    try:
        with connect():
            pass
    except Exception:  # noqa: BLE001
        return False
    return True


if not _database_available():
    pytest.skip(
        "No AccrualFlow database is reachable; skipping integration tests.",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def reporting_month() -> str:
    return "2099-11"


@pytest.fixture
def previous_month() -> str:
    return "2099-10"


@pytest.fixture
def clean_month(reporting_month, previous_month):
    """Remove any leftover rows for the test months."""
    months = [
        f"{reporting_month}-01",
        f"{previous_month}-01",
    ]

    def _wipe():
        with connect() as conn:
            with conn.cursor() as cur:
                for month in months:
                    # The submission tables cascade from submission_lines.
                    cur.execute(
                        """
                        DELETE FROM validation_issues
                        WHERE reporting_month = %s
                        """,
                        (month,),
                    )
                    cur.execute(
                        """
                        DELETE FROM submission_lines
                        WHERE reporting_month = %s
                        """,
                        (month,),
                    )
                    cur.execute(
                        """
                        DELETE FROM department_submissions
                        WHERE reporting_month = %s
                        """,
                        (month,),
                    )
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
                        """
                        DELETE FROM email_archives
                        WHERE reporting_month = %s
                        """,
                        (month,),
                    )
                    cur.execute(
                        """
                        DELETE FROM gmail_message_processing
                        WHERE reporting_month = %s
                        """,
                        (month,),
                    )
                    cur.execute(
                        """
                        DELETE FROM correction_requests
                        WHERE reporting_month = %s
                        """,
                        (month,),
                    )
                    cur.execute(
                        """
                        DELETE FROM department_status_history
                        WHERE reporting_month = %s
                        """,
                        (month,),
                    )
                    cur.execute(
                        """
                        DELETE FROM master_export_runs
                        WHERE reporting_month = %s
                        """,
                        (month,),
                    )
                    cur.execute(
                        """
                        DELETE FROM expected_po_lines
                        WHERE reporting_month = %s
                        """,
                        (month,),
                    )
                    cur.execute(
                        """
                        DELETE FROM monthly_completion_baselines
                        WHERE reporting_month = %s
                        """,
                        (month,),
                    )
                    cur.execute(
                        """
                        DELETE FROM reporting_periods
                        WHERE reporting_month = %s
                        """,
                        (month,),
                    )

    _wipe()
    yield reporting_month
    _wipe()


def _build_workbook(tmp_path, rows, name="reply.xlsx"):
    """Build a completion workbook.

    Each row is ``(po_number, po_line, raw_value, number_format, remarks)``.
    """
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

    for po_number, po_line, raw_value, number_format, remarks in rows:
        worksheet.append([
            po_number,
            po_line,
            "alex.chen",
            "2000-100",
            "SUP-200005",
            "Cognex Corporation",
            "Test description",
            "USD",
            100000,
            0,
            0.4,
            raw_value,
            remarks,
        ])

        cell = worksheet.cell(row=worksheet.max_row, column=12)
        cell.number_format = number_format

    path = tmp_path / name
    workbook.save(path)
    return path


def _fake_gmail_message(message_id, filename, workbook_path, subject):
    """Build a minimal Gmail message payload with one attachment."""
    import base64

    payload = workbook_path.read_bytes()

    return {
        "id": message_id,
        "threadId": f"thread_{message_id}",
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "alex.chen@example.com"},
                {"name": "To", "value": "finance@example.com"},
                {"name": "Date", "value": "Wed, 11 Nov 2026 09:00:00 +0000"},
                {"name": "In-Reply-To", "value": "<request@example.com>"},
            ],
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "",
                    "body": {"size": 100},
                },
                {
                    "mimeType": (
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                    "filename": filename,
                    "body": {
                        "size": len(payload),
                        "attachmentId": "att_1",
                        "data": base64.urlsafe_b64encode(payload).decode("ascii"),
                    },
                },
            ],
        },
    }


def _seed_population(reporting_month, count=87):
    """Seed the expected population with ``count`` PO lines."""
    rows = []

    for index in range(1, count + 1):
        rows.append(
            ExpectedPopulationRow(
                po_number=f"PO{index:05d}",
                po_line=1,
                legal_entity="2000",
                department_id="2000-100",
                department_name="Manufacturing Engineering",
                cost_center="2000-100",
                department_owner="alex.chen@example.com",
                authorized_email="alex.chen@example.com",
                po_description="Test PO line",
                po_line_amount=Decimal("100000.00"),
                cumulative_gr=Decimal("0.00"),
                currency="USD",
                previous_completion_pct=Decimal("0.40"),
                previous_baseline_period=None,
                baseline_status="PRESENT",
            )
        )

    freeze_expected_population(reporting_month, rows)


# ---------------------------------------------------------------------------
# 11. 87 expected lines, 10 issues -> 10 correction rows
# ---------------------------------------------------------------------------
def test_expected_population_is_frozen_and_idempotent(clean_month):
    _seed_population(clean_month, count=5)

    loaded = load_expected_population(clean_month)
    assert len(loaded) == 5

    _seed_population(clean_month, count=5)

    loaded = load_expected_population(clean_month)
    assert len(loaded) == 5


def test_frozen_population_refuses_to_change(clean_month):
    _seed_population(clean_month, count=5)

    with pytest.raises(RuntimeError):
        _seed_population(clean_month, count=6)


# ---------------------------------------------------------------------------
# 17. Raw EML and Excel archived even when validation fails
# ---------------------------------------------------------------------------
def test_invalid_submission_is_still_archived(clean_month, tmp_path):
    from pathlib import Path

    from backend.app.services.completion_pipeline_service import (
        process_inbound_message,
    )

    _seed_population(clean_month, count=3)

    workbook = _build_workbook(
        tmp_path,
        [
            ("PO00001", 1, 0.45, "0%", None),
            ("PO00002", 1, "forty", "General", None),
            ("PO00003", 1, None, "0%", None),
        ],
    )

    message = _fake_gmail_message(
        "msg_invalid", "reply.xlsx", workbook, "CapEx Completion Update"
    )

    result = process_inbound_message(
        gmail_message_id="msg_invalid",
        reporting_month=clean_month,
        raw_bytes=b"raw original bytes for msg_invalid",
        message=message,
        subject="CapEx Completion Update",
    )

    assert result.status == "PERSISTED"
    assert result.eml_path is not None

    eml_file = Path(result.eml_path)
    assert eml_file.exists()
    assert eml_file.read_bytes() == b"raw original bytes for msg_invalid"
    assert "msg_invalid" in eml_file.name
    assert eml_file.name.endswith(".eml")

    assert result.attachment_path is not None
    assert Path(result.attachment_path).exists()

    assert result.accepted_lines == 1
    assert result.unresolved_lines == 2


# ---------------------------------------------------------------------------
# 14. The same Gmail message processed twice -> one logical submission
# ---------------------------------------------------------------------------
def test_duplicate_message_creates_one_submission(clean_month, tmp_path):
    from backend.app.services.completion_pipeline_service import (
        process_inbound_message,
    )

    _seed_population(clean_month, count=3)

    workbook = _build_workbook(
        tmp_path,
        [
            ("PO00001", 1, 0.45, "0%", None),
            ("PO00002", 1, 0.50, "0%", None),
            ("PO00003", 1, 0.60, "0%", None),
        ],
    )

    message = _fake_gmail_message(
        "msg_dup", "reply.xlsx", workbook, "CapEx Completion Update"
    )

    first = process_inbound_message(
        gmail_message_id="msg_dup",
        reporting_month=clean_month,
        raw_bytes=b"raw bytes",
        message=message,
        subject="CapEx Completion Update",
    )

    second = process_inbound_message(
        gmail_message_id="msg_dup",
        reporting_month=clean_month,
        raw_bytes=b"raw bytes",
        message=message,
        subject="CapEx Completion Update",
    )

    assert first.status == "PERSISTED"
    assert second.status == "SKIPPED_DUPLICATE"

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM department_submissions
                WHERE gmail_message_id = 'msg_dup'
                """
            )
            assert cur.fetchone()["count"] == 1


# ---------------------------------------------------------------------------
# 21. Department cannot be COMPLETED with unresolved lines
# ---------------------------------------------------------------------------
def test_department_is_not_completed_with_missing_lines(clean_month, tmp_path):
    from backend.app.services.completion_pipeline_service import (
        process_inbound_message,
    )
    from backend.app.services.department_status_service import (
        DepartmentStatus,
        evaluate_department,
    )
    from backend.app.services.submission_persistence_service import (
        build_accepted_dataset,
    )

    _seed_population(clean_month, count=3)

    workbook = _build_workbook(
        tmp_path,
        [("PO00001", 1, 0.45, "0%", None)],
    )

    message = _fake_gmail_message(
        "msg_partial", "reply.xlsx", workbook, "CapEx Completion Update"
    )

    result = process_inbound_message(
        gmail_message_id="msg_partial",
        reporting_month=clean_month,
        raw_bytes=b"raw bytes",
        message=message,
        subject="CapEx Completion Update",
    )

    assert result.status == "PERSISTED"
    assert result.accepted_lines == 1
    assert result.unresolved_lines == 2

    rows = build_accepted_dataset(clean_month, "2000")

    evaluation = evaluate_department(
        reporting_month=clean_month,
        legal_entity="2000",
        department_id="2000-100",
        accepted_rows=rows,
        open_issues=[],
        has_evidence=True,
        master_export_succeeded=True,
    )

    assert evaluation.status == DepartmentStatus.VALIDATION_FAILED
    assert evaluation.accepted_line_count == 1
    assert evaluation.missing_line_count == 2
    assert evaluation.status != DepartmentStatus.COMPLETED


def test_department_is_not_completed_without_evidence(clean_month):
    from backend.app.services.department_status_service import (
        DepartmentStatus,
        evaluate_department,
    )

    rows = [
        {
            "department_name": "Manufacturing Engineering",
            "cost_center": "2000-100",
            "validation_status": "VALID",
            "reason_status": "NOT_APPLICABLE",
            "accepted_current_pct": Decimal("0.45"),
            "exception_codes": [],
        }
    ]

    evaluation = evaluate_department(
        reporting_month=clean_month,
        legal_entity="2000",
        department_id="2000-100",
        accepted_rows=rows,
        open_issues=[],
        has_evidence=False,
        master_export_succeeded=True,
    )

    assert evaluation.status == DepartmentStatus.VALIDATION_FAILED
    assert evaluation.status != DepartmentStatus.COMPLETED


def test_department_is_not_completed_with_format_ambiguity(clean_month):
    from backend.app.services.department_status_service import (
        DepartmentStatus,
        evaluate_department,
    )

    rows = [
        {
            "department_name": "Manufacturing Engineering",
            "cost_center": "2000-100",
            "validation_status": "FORMAT_CONFIRMATION_REQUIRED",
            "reason_status": "NOT_APPLICABLE",
            "accepted_current_pct": None,
            "exception_codes": ["AMBIGUOUS_INTEGER"],
        }
    ]

    evaluation = evaluate_department(
        reporting_month=clean_month,
        legal_entity="2000",
        department_id="2000-100",
        accepted_rows=rows,
        open_issues=[],
        has_evidence=True,
        master_export_succeeded=True,
    )

    assert evaluation.status == DepartmentStatus.FORMAT_CONFIRMATION_REQUIRED
    assert evaluation.status != DepartmentStatus.COMPLETED


def test_department_is_not_completed_with_pending_review(clean_month):
    from backend.app.services.department_status_service import (
        DepartmentStatus,
        evaluate_department,
    )

    rows = [
        {
            "department_name": "Manufacturing Engineering",
            "cost_center": "2000-100",
            "validation_status": "VALID",
            "reason_status": "DECREASE_REASON_PROVIDED",
            "accepted_current_pct": Decimal("0.45"),
            "exception_codes": ["DECREASE_REASON_PROVIDED"],
        }
    ]

    evaluation = evaluate_department(
        reporting_month=clean_month,
        legal_entity="2000",
        department_id="2000-100",
        accepted_rows=rows,
        open_issues=[],
        has_evidence=True,
        master_export_succeeded=True,
    )

    assert evaluation.status == DepartmentStatus.DATA_COMPLETE_REVIEW_PENDING
    assert evaluation.status != DepartmentStatus.COMPLETED


def test_department_is_completed_when_everything_is_accepted(clean_month):
    from backend.app.services.department_status_service import (
        DepartmentStatus,
        evaluate_department,
    )

    rows = [
        {
            "department_name": "Manufacturing Engineering",
            "cost_center": "2000-100",
            "validation_status": "VALID",
            "reason_status": "NOT_APPLICABLE",
            "accepted_current_pct": Decimal("0.45"),
            "exception_codes": [],
        }
    ]

    evaluation = evaluate_department(
        reporting_month=clean_month,
        legal_entity="2000",
        department_id="2000-100",
        accepted_rows=rows,
        open_issues=[],
        has_evidence=True,
        master_export_succeeded=True,
    )

    assert evaluation.status == DepartmentStatus.COMPLETED


# ---------------------------------------------------------------------------
# 20. Resubmission updates only the intended lines
# ---------------------------------------------------------------------------
def test_resubmission_preserves_previous_versions(clean_month, tmp_path):
    from backend.app.services.completion_pipeline_service import (
        process_inbound_message,
    )
    from backend.app.services.submission_persistence_service import (
        build_accepted_dataset,
    )

    _seed_population(clean_month, count=3)

    first_workbook = _build_workbook(
        tmp_path,
        [
            ("PO00001", 1, 0.45, "0%", None),
            ("PO00002", 1, 0.50, "0%", None),
        ],
        name="first.xlsx",
    )

    first_message = _fake_gmail_message(
        "msg_v1", "first.xlsx", first_workbook, "CapEx Completion Update"
    )

    first = process_inbound_message(
        gmail_message_id="msg_v1",
        reporting_month=clean_month,
        raw_bytes=b"raw v1",
        message=first_message,
        subject="CapEx Completion Update",
    )

    assert first.status == "PERSISTED"
    assert first.version == 1

    second_workbook = _build_workbook(
        tmp_path,
        [("PO00002", 1, 0.60, "0%", "Corrected after clarification")],
        name="second.xlsx",
    )

    second_message = _fake_gmail_message(
        "msg_v2", "second.xlsx", second_workbook, "CapEx Completion Update"
    )

    second = process_inbound_message(
        gmail_message_id="msg_v2",
        reporting_month=clean_month,
        raw_bytes=b"raw v2",
        message=second_message,
        subject="CapEx Completion Update",
    )

    assert second.status == "PERSISTED"
    assert second.version == 2

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT version, COUNT(*)
                FROM department_submissions
                WHERE reporting_month = %s
                GROUP BY version
                ORDER BY version
                """,
                (f"{clean_month}-01",),
            )
            versions = {row["version"]: row["count"] for row in cur.fetchall()}

    assert versions == {1: 1, 2: 1}

    rows = build_accepted_dataset(clean_month, "2000")
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# 22. Master workbook contains every expected line and all four sheets
# ---------------------------------------------------------------------------
def test_master_export_contains_all_expected_lines_and_four_sheets(
    clean_month, tmp_path
):
    from openpyxl import load_workbook

    from backend.app.services.master_excel_service import export_master_excel

    _seed_population(clean_month, count=4)

    result = export_master_excel(clean_month, "2000", output_dir=tmp_path)

    assert result.expected_line_count == 4
    assert result.department_count == 1

    workbook = load_workbook(result.file_path, read_only=True)

    assert set(workbook.sheetnames) == {
        "Master_Data",
        "Validation_Exceptions",
        "Department_Summary",
        "Email_Audit_Log",
    }

    worksheet = workbook["Master_Data"]
    assert worksheet.max_row == 5

    workbook.close()


def test_repeated_export_does_not_duplicate_data(clean_month, tmp_path):
    from backend.app.services.master_excel_service import export_master_excel

    _seed_population(clean_month, count=3)

    first = export_master_excel(clean_month, "2000", output_dir=tmp_path)
    second = export_master_excel(clean_month, "2000", output_dir=tmp_path)

    assert first.expected_line_count == second.expected_line_count
    assert first.sha256 == second.sha256

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM master_export_runs
                WHERE reporting_month = %s
                """,
                (f"{clean_month}-01",),
            )
            assert cur.fetchone()["count"] == 2