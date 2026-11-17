"""
Department level status computation.

The department status is derived from the accepted dataset.  It is never set
to ``COMPLETED`` while a required PO line is missing, a format ambiguity is
unresolved, a review is pending, or the evidence / export is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from backend.app.services.month_comparison_service import ReasonStatus
from backend.app.services.percentage_validation_service import ExceptionCode
from backend.app.services.submission_persistence_service import (
    connect,
    _month_start,
)


class DepartmentStatus:
    """The allowed department states."""

    NOT_SUBMITTED = "NOT_SUBMITTED"
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    FORMAT_CONFIRMATION_REQUIRED = "FORMAT_CONFIRMATION_REQUIRED"
    CORRECTION_PREPARED = "CORRECTION_PREPARED"
    CORRECTION_REQUESTED = "CORRECTION_REQUESTED"
    RESUBMITTED = "RESUBMITTED"
    DATA_COMPLETE_REVIEW_PENDING = "DATA_COMPLETE_REVIEW_PENDING"
    COMPLETED = "COMPLETED"


ALL_STATUSES = (
    DepartmentStatus.NOT_SUBMITTED,
    DepartmentStatus.RECEIVED,
    DepartmentStatus.VALIDATING,
    DepartmentStatus.VALIDATION_FAILED,
    DepartmentStatus.FORMAT_CONFIRMATION_REQUIRED,
    DepartmentStatus.CORRECTION_PREPARED,
    DepartmentStatus.CORRECTION_REQUESTED,
    DepartmentStatus.RESUBMITTED,
    DepartmentStatus.DATA_COMPLETE_REVIEW_PENDING,
    DepartmentStatus.COMPLETED,
)

# Exceptions that block a department from reaching COMPLETED.
BLOCKING_CODES = frozenset(
    {
        ExceptionCode.MISSING_VALUE.value,
        ExceptionCode.MISSING_LINE.value,
        ExceptionCode.INVALID_VALUE.value,
        ExceptionCode.OUT_OF_RANGE.value,
        ExceptionCode.NEGATIVE_VALUE.value,
        ExceptionCode.NON_NUMERIC_VALUE.value,
        ExceptionCode.FORMULA_CELL.value,
        ExceptionCode.EXCEL_ERROR.value,
        ExceptionCode.AMBIGUOUS_INTEGER.value,
        ExceptionCode.AMBIGUOUS_DECIMAL.value,
        ExceptionCode.AMBIGUOUS_ZERO_OR_ONE.value,
        ExceptionCode.AMBIGUOUS_MIXED_FORMATS.value,
        ExceptionCode.UNKNOWN_PO_LINE.value,
        ExceptionCode.DUPLICATE_PO_LINE.value,
        ExceptionCode.DEPARTMENT_MISMATCH.value,
        ExceptionCode.PERIOD_MISMATCH.value,
        ExceptionCode.BASELINE_MISSING.value,
        ExceptionCode.DECREASE_REASON_MISSING.value,
        ExceptionCode.DECREASE_REASON_PROVIDED.value,
    }
)


@dataclass
class DepartmentEvaluation:
    """The computed status of one department for one reporting month."""

    reporting_month: str
    legal_entity: str
    department_id: str
    department_name: str
    cost_center: str | None = None

    status: str = DepartmentStatus.NOT_SUBMITTED
    previous_status: str | None = None
    reason: str | None = None

    expected_line_count: int = 0
    accepted_line_count: int = 0
    missing_line_count: int = 0
    invalid_line_count: int = 0
    review_line_count: int = 0
    format_confirmation_count: int = 0
    unresolved_line_count: int = 0

    open_issue_count: int = 0
    latest_correction_status: str | None = None
    has_evidence: bool = False
    master_export_succeeded: bool = False

    changed: bool = False
    """True when the status differs from the previously recorded one."""

    def to_dict(self) -> dict:
        return {
            "reporting_month": self.reporting_month,
            "legal_entity": self.legal_entity,
            "department_id": self.department_id,
            "department_name": self.department_name,
            "cost_center": self.cost_center,
            "status": self.status,
            "previous_status": self.previous_status,
            "reason": self.reason,
            "expected_line_count": self.expected_line_count,
            "accepted_line_count": self.accepted_line_count,
            "missing_line_count": self.missing_line_count,
            "invalid_line_count": self.invalid_line_count,
            "review_line_count": self.review_line_count,
            "format_confirmation_count": self.format_confirmation_count,
            "unresolved_line_count": self.unresolved_line_count,
            "open_issue_count": self.open_issue_count,
            "latest_correction_status": self.latest_correction_status,
            "has_evidence": self.has_evidence,
            "master_export_succeeded": self.master_export_succeeded,
            "changed": self.changed,
        }


def evaluate_department(
    reporting_month: str,
    legal_entity: str,
    department_id: str,
    accepted_rows: list,
    open_issues: list | None = None,
    latest_correction_status: str | None = None,
    has_evidence: bool = False,
    master_export_succeeded: bool = False,
) -> DepartmentEvaluation:
    """Compute the department status from the accepted dataset.

    Parameters
    ----------
    accepted_rows:
        One row per expected PO line, as returned by
        ``build_accepted_dataset``.
    open_issues:
        The still-open validation issues of this department.
    latest_correction_status:
        ``PREPARED``, ``SENT``, ``FAILED``, ``RESOLVED`` or None.
    has_evidence:
        True when the raw EML and the source workbook were archived.
    master_export_succeeded:
        True when the Master Excel for this month was published successfully.
    """
    first = accepted_rows[0] if accepted_rows else {}

    evaluation = DepartmentEvaluation(
        reporting_month=reporting_month,
        legal_entity=legal_entity,
        department_id=department_id,
        department_name=first.get("department_name", ""),
        cost_center=first.get("cost_center"),
        expected_line_count=len(accepted_rows),
        latest_correction_status=latest_correction_status,
        has_evidence=has_evidence,
        master_export_succeeded=master_export_succeeded,
    )

    issues = open_issues or []
    evaluation.open_issue_count = len(issues)

    blocking_codes: set = set()
    for issue in issues:
        code = issue.get("exception_code")
        if code in BLOCKING_CODES:
            blocking_codes.add(code)

    for row in accepted_rows:
        validation_status = row.get("validation_status")
        reason_status = row.get("reason_status")
        accepted_pct = row.get("accepted_current_pct")

        if validation_status == "MISSING":
            evaluation.missing_line_count += 1
        elif validation_status == "INVALID":
            evaluation.invalid_line_count += 1

        if validation_status == "FORMAT_CONFIRMATION_REQUIRED":
            evaluation.format_confirmation_count += 1

        if reason_status in (
            ReasonStatus.DECREASE_REASON_MISSING.value,
            ReasonStatus.DECREASE_REASON_PROVIDED.value,
            ReasonStatus.REVIEW_REQUIRED.value,
        ):
            evaluation.review_line_count += 1

        if accepted_pct is not None:
            evaluation.accepted_line_count += 1

        for code in row.get("exception_codes") or []:
            if code in BLOCKING_CODES:
                blocking_codes.add(code)

    evaluation.unresolved_line_count = (
        evaluation.expected_line_count - evaluation.accepted_line_count
    )

    # -------------------------------------------------------------- status
    if evaluation.expected_line_count == 0:
        evaluation.status = DepartmentStatus.NOT_SUBMITTED
        evaluation.reason = "No expected PO line population for this department."
        return evaluation

    if evaluation.format_confirmation_count > 0:
        evaluation.status = DepartmentStatus.FORMAT_CONFIRMATION_REQUIRED
        evaluation.reason = (
            f"{evaluation.format_confirmation_count} line(s) have an "
            "ambiguous percentage representation."
        )
        return evaluation

    if evaluation.missing_line_count > 0 or evaluation.invalid_line_count > 0:
        evaluation.status = DepartmentStatus.VALIDATION_FAILED
        evaluation.reason = (
            f"{evaluation.missing_line_count} missing and "
            f"{evaluation.invalid_line_count} invalid line(s)."
        )
        return evaluation

    if evaluation.accepted_line_count == 0 and not issues:
        evaluation.status = DepartmentStatus.NOT_SUBMITTED
        evaluation.reason = "No submission has been received."
        return evaluation

    if evaluation.review_line_count > 0:
        evaluation.status = DepartmentStatus.DATA_COMPLETE_REVIEW_PENDING
        evaluation.reason = (
            f"{evaluation.review_line_count} line(s) await business review."
        )
        return evaluation

    if evaluation.accepted_line_count < evaluation.expected_line_count:
        evaluation.status = DepartmentStatus.DATA_COMPLETE_REVIEW_PENDING
        evaluation.reason = (
            f"{evaluation.unresolved_line_count} line(s) are still unresolved."
        )
        return evaluation

    if not evaluation.has_evidence:
        evaluation.status = DepartmentStatus.VALIDATION_FAILED
        evaluation.reason = "The evidence archive is missing for this submission."
        return evaluation

    if not evaluation.master_export_succeeded:
        evaluation.status = DepartmentStatus.DATA_COMPLETE_REVIEW_PENDING
        evaluation.reason = (
            "All lines are accepted but the Master Excel has not been "
            "published for this reporting month."
        )
        return evaluation

    evaluation.status = DepartmentStatus.COMPLETED
    evaluation.reason = "All expected PO lines are accepted and evidenced."
    return evaluation


def record_status(
    evaluation: DepartmentEvaluation,
    changed_by: str = "system",
) -> int:
    """Persist a department status transition."""
    month = _month_start(evaluation.reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status
                FROM department_status_history
                WHERE reporting_month = %s
                  AND legal_entity = %s
                  AND department_id = %s
                ORDER BY changed_at DESC, status_id DESC
                LIMIT 1
                """,
                (month, evaluation.legal_entity, evaluation.department_id),
            )
            previous = cur.fetchone()

            previous_status = previous["status"] if previous else None
            evaluation.previous_status = previous_status
            evaluation.changed = previous_status != evaluation.status

            cur.execute(
                """
                INSERT INTO department_status_history (
                    reporting_month, legal_entity,
                    department_id, department_name, cost_center,
                    status, previous_status,
                    expected_line_count, accepted_line_count,
                    missing_line_count, invalid_line_count,
                    review_line_count, reason, changed_by
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING status_id
                """,
                (
                    month,
                    evaluation.legal_entity,
                    evaluation.department_id,
                    evaluation.department_name,
                    evaluation.cost_center,
                    evaluation.status,
                    previous_status,
                    evaluation.expected_line_count,
                    evaluation.accepted_line_count,
                    evaluation.missing_line_count,
                    evaluation.invalid_line_count,
                    evaluation.review_line_count,
                    evaluation.reason,
                    changed_by,
                ),
            )
            return cur.fetchone()["status_id"]


def get_department_status(
    reporting_month: str,
    legal_entity: str,
    department_id: str,
) -> str | None:
    """Return the current status of a department."""
    month = _month_start(reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status
                FROM department_status_history
                WHERE reporting_month = %s
                  AND legal_entity = %s
                  AND department_id = %s
                ORDER BY changed_at DESC, status_id DESC
                LIMIT 1
                """,
                (month, legal_entity, department_id),
            )
            row = cur.fetchone()
            return row["status"] if row else None


def list_department_statuses(
    reporting_month: str,
    legal_entity: str | None = None,
) -> list:
    """List the current status of every department of a reporting month."""
    month = _month_start(reporting_month)

    params: list[Any] = [month]
    clause = ""

    if legal_entity:
        clause = "AND h.legal_entity = %s"
        params.append(legal_entity)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT ON (h.department_id)
                    h.reporting_month,
                    h.legal_entity,
                    h.department_id,
                    h.department_name,
                    h.status,
                    h.previous_status,
                    h.expected_line_count,
                    h.accepted_line_count,
                    h.missing_line_count,
                    h.invalid_line_count,
                    h.review_line_count,
                    h.reason,
                    h.changed_at
                FROM department_status_history h
                WHERE h.reporting_month = %s
                  {clause}
                ORDER BY h.department_id, h.changed_at DESC, h.status_id DESC
                """,
                params,
            )
            return list(cur.fetchall())