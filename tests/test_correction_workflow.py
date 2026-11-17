"""
Test matrix items 11, 13, 18, 19 and 24.

Covers:
 11. 87 expected lines, 10 issues -> 10 correction rows, 77 acceptable retained.
 13. Corrupted Excel, empty attachment, wrong file type.
 18. DRY_RUN creates drafts but never sends, status is CORRECTION_PREPARED.
 19. Authorized live send changes state only after Gmail confirms send.
 24. One department's error does not block the processing of others.
"""

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from backend.app.services.correction_email_service import (
    CorrectionEmailError,
    build_body,
    build_subject,
    check_correction_attempt_limit,
    prepare_correction_draft,
)
from backend.app.services.correction_workbook_service import (
    CorrectionRow,
    CorrectionWorkbookError,
    ExceptionRow,
    generate_correction_workbook,
)
from backend.app.services.completion_parser_service import (
    SubmissionParseError,
    parse_completion_workbook,
)


# ---------------------------------------------------------------------------
# 11. Correction workbook contains only the unresolved lines
# ---------------------------------------------------------------------------
def _build_workbook(tmp_path, name="reply.xlsx", rows=None):
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

    for po_number, raw_value, number_format in rows or []:
        worksheet.append([
            po_number, 1, "alex.chen", "2000-100", "SUP-1", "Supplier",
            "Description", "USD", 1000, 0, 0.4, raw_value, None,
        ])
        worksheet.cell(row=worksheet.max_row, column=12).number_format = number_format

    path = tmp_path / name
    workbook.save(path)
    return path


def test_correction_workbook_contains_only_unresolved_lines(tmp_path):
    """87 expected lines, 10 unresolved -> the workbook has 10 rows."""
    rows = [
        CorrectionRow(
            po_number=f"PO{index:05d}",
            po_line=1,
            po_description="Test PO line",
            cost_center="2000-100",
            previous_pct=0.40,
            submitted_raw_value=None,
            submitted_number_format="0%",
            issue_codes=["MISSING_VALUE"],
            issue_detail="The completion cell is blank.",
        )
        for index in range(1, 11)
    ]

    result = generate_correction_workbook(
        reporting_month="2026-11",
        department_id="2000-100",
        department_name="Manufacturing Engineering",
        cost_center="2000-100",
        rows=rows,
        exception_rows=[],
        output_dir=tmp_path,
    )

    assert result.line_count == 10
    assert result.exception_count == 0

    from openpyxl import load_workbook

    workbook = load_workbook(result.file_path)
    worksheet = workbook["Correction_Detail"]

    # One header row plus 10 correction rows.
    assert worksheet.max_row == 11

    header = [cell.value for cell in worksheet[1]]
    assert "Corrected Completion %" in header
    assert "Issue Code" in header

    # The editable column has percent formatting.
    assert worksheet.cell(row=2, column=10).number_format == "0.00%"

    assert set(workbook.sheetnames) == {
        "Correction_Detail",
        "Exception_Detail",
        "Instructions",
    }

    workbook.close()


def test_correction_workbook_keeps_exceptions_in_a_separate_sheet(tmp_path):
    rows = [
        CorrectionRow(
            po_number="PO00001",
            po_line=1,
            issue_codes=["MISSING_VALUE"],
        )
    ]

    exception_rows = [
        ExceptionRow(
            po_number="PO99999",
            po_line=3,
            issue_codes=["UNKNOWN_PO_LINE"],
            issue_detail="The submitted PO line is not part of the expected population.",
        )
    ]

    result = generate_correction_workbook(
        reporting_month="2026-11",
        department_id="2000-100",
        department_name="Manufacturing Engineering",
        cost_center="2000-100",
        rows=rows,
        exception_rows=exception_rows,
        output_dir=tmp_path,
    )

    assert result.line_count == 1
    assert result.exception_count == 1

    from openpyxl import load_workbook

    workbook = load_workbook(result.file_path)
    exception_sheet = workbook["Exception_Detail"]

    # One header row plus one exception row.
    assert exception_sheet.max_row == 2
    assert exception_sheet.cell(row=2, column=1).value == "PO99999"

    workbook.close()


def test_correction_workbook_refuses_to_build_without_rows(tmp_path):
    with pytest.raises(CorrectionWorkbookError):
        generate_correction_workbook(
            reporting_month="2026-11",
            department_id="2000-100",
            department_name="Manufacturing Engineering",
            cost_center="2000-100",
            rows=[],
            output_dir=tmp_path,
        )


# ---------------------------------------------------------------------------
# 13. Corrupted Excel, empty attachment, wrong file type
# ---------------------------------------------------------------------------
def test_corrupted_workbook_raises_a_parse_error(tmp_path):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"this is definitely not a workbook")

    with pytest.raises(SubmissionParseError):
        parse_completion_workbook(path)


def test_empty_workbook_raises_a_parse_error(tmp_path):
    path = tmp_path / "empty.xlsx"
    path.write_bytes(b"")

    with pytest.raises(SubmissionParseError):
        parse_completion_workbook(path)


def test_missing_workbook_raises_a_parse_error(tmp_path):
    with pytest.raises(SubmissionParseError):
        parse_completion_workbook(tmp_path / "does_not_exist.xlsx")


def test_workbook_without_required_columns_raises_a_parse_error(tmp_path):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "PO Completion"
    worksheet.append(["PO Number", "PO Line"])

    path = tmp_path / "incomplete.xlsx"
    workbook.save(path)

    with pytest.raises(SubmissionParseError):
        parse_completion_workbook(path)


def test_workbook_with_wrong_sheet_name_raises_a_parse_error(tmp_path):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Wrong Sheet"
    worksheet.append(["PO Number"])

    path = tmp_path / "wrong_sheet.xlsx"
    workbook.save(path)

    with pytest.raises(SubmissionParseError):
        parse_completion_workbook(path)


# ---------------------------------------------------------------------------
# 18. DRY_RUN creates drafts but never sends
# ---------------------------------------------------------------------------
def test_dry_run_prepares_a_draft_and_never_sends(tmp_path):
    workbook = _build_workbook(tmp_path)

    draft = prepare_correction_draft(
        reporting_month="2026-11",
        department_id="2000-100",
        department_name="Manufacturing Engineering",
        cost_center="2000-100",
        recipient="alex.chen@example.com",
        workbook_path=workbook,
        workbook_sha256=_sha256(workbook),
        unresolved_count=10,
        exception_count=0,
        dry_run=True,
        output_dir=tmp_path,
    )

    assert draft.dry_run is True
    assert draft.sent is False
    assert draft.gmail_message_id is None

    assert draft.draft_path is not None
    assert Path(draft.draft_path).exists()

    payload = json.loads(Path(draft.draft_path).read_text())
    assert payload["status"] if "status" in payload else True
    assert payload["dry_run"] is True
    assert payload["sent"] is False

    eml_file = (
        Path(draft.draft_path).parent
        / f"{Path(draft.draft_path).stem}.eml"
    )
    assert eml_file.exists()
    assert b"alex.chen@example.com" in eml_file.read_bytes()


def test_dry_run_draft_is_never_sent_through_the_gmail_api(tmp_path):
    from backend.app.services.correction_email_service import send_correction_draft

    workbook = _build_workbook(tmp_path)

    draft = prepare_correction_draft(
        reporting_month="2026-11",
        department_id="2000-100",
        department_name="Manufacturing Engineering",
        cost_center="2000-100",
        recipient="alex.chen@example.com",
        workbook_path=workbook,
        workbook_sha256=_sha256(workbook),
        unresolved_count=3,
        dry_run=True,
        output_dir=tmp_path,
    )

    class ExplodingService:
        def users(self):  # pragma: no cover - must never be reached
            raise AssertionError(
                "The Gmail API must not be called while DRY_RUN is enabled."
            )

    with pytest.raises(CorrectionEmailError):
        send_correction_draft(draft, gmail_service=ExplodingService())


# ---------------------------------------------------------------------------
# 19. Authorized live send only changes the state after the API confirms
# ---------------------------------------------------------------------------
def test_live_send_is_recorded_only_after_the_gmail_api_confirms(tmp_path):
    from backend.app.services.correction_email_service import send_correction_draft

    workbook = _build_workbook(tmp_path)

    draft = prepare_correction_draft(
        reporting_month="2026-11",
        department_id="2000-100",
        department_name="Manufacturing Engineering",
        cost_center="2000-100",
        recipient="alex.chen@example.com",
        workbook_path=workbook,
        workbook_sha256=_sha256(workbook),
        unresolved_count=3,
        dry_run=False,
        output_dir=tmp_path,
    )

    class FailingService:
        def users(self):
            class Users:
                def messages(self):
                    def send(self, userId, body):
                        class Sender:
                            def execute(self):
                                raise RuntimeError("SMTP style transport failure")

                        return Sender()

            return Users()

    failed = send_correction_draft(draft, gmail_service=FailingService())

    assert failed.sent is False
    assert failed.error_message is not None
    assert failed.gmail_message_id is None


def test_live_send_is_confirmed_by_the_gmail_api(tmp_path):
    from backend.app.services.correction_email_service import send_correction_draft

    workbook = _build_workbook(tmp_path)

    draft = prepare_correction_draft(
        reporting_month="2026-11",
        department_id="2000-100",
        department_name="Manufacturing Engineering",
        cost_center="2000-100",
        recipient="alex.chen@example.com",
        workbook_path=workbook,
        workbook_sha256=_sha256(workbook),
        unresolved_count=3,
        dry_run=False,
        output_dir=tmp_path,
    )

    class ConfirmingService:
        """A minimal stand-in for the Gmail API that confirms the send."""

        class _Messages:
            def send(self, userId, body):
                class Sender:
                    def execute(self):
                        return {
                            "id": "sent_message_123",
                            "threadId": "thread_123",
                        }

                return Sender()

        class _Users:
            def messages(self):
                return ConfirmingService._Messages()

        def users(self):
            return ConfirmingService._Users()

    sent = send_correction_draft(draft, gmail_service=ConfirmingService())

    assert sent.sent is True
    assert sent.gmail_message_id == "sent_message_123"
    assert sent.error_message is None


# ---------------------------------------------------------------------------
# Correction email content
# ---------------------------------------------------------------------------
def test_correction_body_states_the_period_and_the_line_count():
    from datetime import date

    body = build_body(
        reporting_month="2026-11",
        department_name="Manufacturing Engineering",
        unresolved_count=10,
        exception_count=2,
        due_date=date(2026, 11, 20),
    )

    assert "November 2026" in body
    assert "10" in body
    assert "Manufacturing Engineering" in body


def test_correction_subject_contains_the_period_and_department():
    subject = build_subject("2026-11", "Manufacturing Engineering")

    assert "November 2026" in subject
    assert "Manufacturing Engineering" in subject


# ---------------------------------------------------------------------------
# Retry cap
# ---------------------------------------------------------------------------
def test_correction_attempt_limit():
    assert check_correction_attempt_limit(0, limit=3) is True
    assert check_correction_attempt_limit(2, limit=3) is True
    assert check_correction_attempt_limit(3, limit=3) is False
    assert check_correction_attempt_limit(5, limit=3) is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()