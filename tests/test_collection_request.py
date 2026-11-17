"""
Tests for the collection request generation.

The request workbook is the contract with the department: it must contain
exactly the PO lines of that department, and the completion cell must be
percent formatted so that the submitted value is unambiguous.
"""

import pytest
from openpyxl import load_workbook

from backend.app.services.collection_request_service import (
    CollectionRequestError,
    build_request_body,
    build_request_subject,
    build_template_workbook,
    prepare_request,
    send_request,
)


def _rows(count: int = 5):
    return [
        {
            "po_number": f"457{index:05d}",
            "po_line": 1,
            "cost_center": "2000-100",
            "po_description": f"Capital equipment line {index}",
            "po_line_amount": 113350.0,
            "cumulative_gr": 34005.0,
            "currency": "USD",
            "previous_completion_pct": 0.40,
            "requester": "alex.chen",
            "supplier_code": "SUP-200005",
            "supplier_name": "Cognex Corporation",
        }
        for index in range(1, count + 1)
    ]


def test_template_workbook_contains_every_po_line(tmp_path):
    path, checksum = build_template_workbook(
        "2026-11",
        "2000-100",
        "Manufacturing Engineering",
        "2000-100",
        _rows(5),
        output_dir=tmp_path,
    )

    assert checksum

    workbook = load_workbook(path)

    assert set(workbook.sheetnames) == {"PO Completion", "Instructions"}

    worksheet = workbook["PO Completion"]

    # One header row plus five PO lines.
    assert worksheet.max_row == 6

    header = [cell.value for cell in worksheet[1]]
    assert "Current Estimated Completion %" in header
    assert "Comments / Explanation" in header

    workbook.close()


def test_the_completion_cell_is_percent_formatted(tmp_path):
    """The template removes the ambiguity for the department."""
    path, _ = build_template_workbook(
        "2026-11",
        "2000-100",
        "Manufacturing Engineering",
        "2000-100",
        _rows(3),
        output_dir=tmp_path,
    )

    workbook = load_workbook(path)
    worksheet = workbook["PO Completion"]

    # Column L is the completion column, column K the previous completion.
    assert worksheet.cell(row=2, column=12).number_format == "0.00%"
    assert worksheet.cell(row=2, column=11).number_format == "0.00%"

    workbook.close()


def test_the_completion_cell_starts_empty(tmp_path):
    path, _ = build_template_workbook(
        "2026-11",
        "2000-100",
        "Manufacturing Engineering",
        "2000-100",
        _rows(3),
        output_dir=tmp_path,
    )

    workbook = load_workbook(path)
    worksheet = workbook["PO Completion"]

    for row in range(2, worksheet.max_row + 1):
        assert worksheet.cell(row=row, column=12).value is None

    workbook.close()


def test_the_previous_completion_is_carried_over(tmp_path):
    path, _ = build_template_workbook(
        "2026-11",
        "2000-100",
        "Manufacturing Engineering",
        "2000-100",
        _rows(3),
        output_dir=tmp_path,
    )

    workbook = load_workbook(path)
    worksheet = workbook["PO Completion"]

    assert worksheet.cell(row=2, column=11).value == 0.40

    workbook.close()


def test_request_subject_and_body_mention_the_period():
    from datetime import date

    subject = build_request_subject("2026-11", "Manufacturing Engineering")
    body = build_request_body(
        "2026-11",
        "Manufacturing Engineering",
        5,
        date(2026, 11, 20),
    )

    assert "November 2026" in subject
    assert "Manufacturing Engineering" in subject

    assert "November 2026" in body
    assert "5 open PO line" in body
    assert "November 20, 2026" in body


def test_an_invalid_recipient_is_rejected(tmp_path):
    with pytest.raises(CollectionRequestError):
        prepare_request(
            reporting_month="2026-11",
            legal_entity="2000",
            department_id="2000-100",
            department_name="Manufacturing Engineering",
            cost_center="2000-100",
            recipient="not-an-address",
            rows=_rows(1),
            output_dir=tmp_path,
        )


def test_a_dry_run_request_is_never_sent(tmp_path):
    request = prepare_request(
        reporting_month="2026-11",
        legal_entity="2000",
        department_id="2000-100",
        department_name="Manufacturing Engineering",
        cost_center="2000-100",
        recipient="alex.chen@example.com",
        rows=_rows(3),
        output_dir=tmp_path,
        dry_run=True,
    )

    assert request.dry_run is True
    assert request.sent is False
    assert request.gmail_message_id is None

    class ExplodingService:
        def users(self):  # pragma: no cover - must never be reached
            raise AssertionError(
                "The Gmail API must not be called while DRY_RUN is enabled."
            )

    with pytest.raises(CollectionRequestError):
        send_request(request, gmail_service=ExplodingService())


def test_the_request_is_confirmed_only_after_the_api_returns(tmp_path):
    request = prepare_request(
        reporting_month="2026-11",
        legal_entity="2000",
        department_id="2000-100",
        department_name="Manufacturing Engineering",
        cost_center="2000-100",
        recipient="alex.chen@example.com",
        rows=_rows(3),
        output_dir=tmp_path,
        dry_run=False,
    )

    class FailingService:
        def users(self):
            class Users:
                def messages(self):
                    def send(self, userId, body):
                        class Sender:
                            def execute(self):
                                raise RuntimeError("transport failure")

                        return Sender()

            return Users()

    failed = send_request(request, gmail_service=FailingService())

    assert failed.sent is False
    assert failed.error_message is not None
    assert failed.gmail_message_id is None