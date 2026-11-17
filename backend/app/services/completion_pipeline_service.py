"""
End-to-end completion processing pipeline.

The pipeline is deterministic and idempotent:

    freeze the expected population
    -> archive the raw message and the attachments
    -> parse the workbook
    -> validate every line
    -> compare against the previous finalized baseline
    -> persist the submission version and the issues
    -> update the department status
    -> prepare the correction request

Every step is safe to run repeatedly.  A message that was already processed
is skipped without creating a second submission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from backend.app.services.completion_parser_service import (
    ParsedSubmission,
    SubmissionParseError,
    mark_duplicate_lines,
    parse_completion_workbook,
)
from backend.app.services.email_archive_service import (
    SUPPORTED_ATTACHMENT_EXTENSIONS,
    ArchiveError,
    archive_message,
)
from backend.app.services.month_comparison_service import (
    ReasonStatus,
    compare_with_previous,
)
from backend.app.services.population_status_scope_service import (
    LIFECYCLE_ACTIVE,
)
from backend.app.services.percentage_validation_service import (
    ExceptionCode,
    FormatStatus,
)
from backend.app.services.submission_persistence_service import (
    SubmissionLineRecord,
    SubmissionRecord,
    build_accepted_dataset,
    connect,
    freeze_expected_population,
    load_expected_population,
    load_previous_baselines,
    persist_email_archive,
    persist_email_attachments,
    persist_submission,
    previous_month_of,
    register_gmail_message,
    update_gmail_processing,
)


class PipelineError(Exception):
    """Raised when the pipeline cannot process a message."""


@dataclass
class ProcessingResult:
    """The outcome of processing one inbound message."""

    gmail_message_id: str
    reporting_month: str
    status: str
    department_id: str | None = None
    department_name: str | None = None
    submission_id: int | None = None
    version: int | None = None

    expected_lines: int = 0
    accepted_lines: int = 0
    unresolved_lines: int = 0

    eml_path: str | None = None
    attachment_path: str | None = None

    error_message: str | None = None
    issues: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "gmail_message_id": self.gmail_message_id,
            "reporting_month": self.reporting_month,
            "status": self.status,
            "department_id": self.department_id,
            "department_name": self.department_name,
            "submission_id": self.submission_id,
            "version": self.version,
            "expected_lines": self.expected_lines,
            "accepted_lines": self.accepted_lines,
            "unresolved_lines": self.unresolved_lines,
            "eml_path": self.eml_path,
            "attachment_path": self.attachment_path,
            "error_message": self.error_message,
            "issues": self.issues,
        }


# ---------------------------------------------------------------------------
# Expected population
# ---------------------------------------------------------------------------


def build_expected_population_from_rows(
    rows: list[dict],
    reporting_month: str,
    legal_entity: str,
) -> list:
    """Build the expected population rows from the ERP/DB source rows.

    The previous baseline is looked up from the finalized baseline table of
    the previous month.  A missing baseline stays missing; it is never
    defaulted to 0.
    """
    from backend.app.services.submission_persistence_service import (
        ExpectedPopulationRow,
    )

    previous_month = previous_month_of(reporting_month)
    baselines = load_previous_baselines(
        reporting_month, legal_entity, previous_month
    )

    population = []

    for row in rows:
        key = (str(row["po_number"]), int(row["po_line"]))

        previous_pct = baselines.get(key)
        baseline_status = "PRESENT" if previous_pct is not None else "MISSING"

        population.append(
            ExpectedPopulationRow(
                po_number=str(row["po_number"]),
                po_line=int(row["po_line"]),
                legal_entity=legal_entity,
                department_id=str(row["cost_center"]),
                department_name=str(row.get("department", "")),
                cost_center=str(row["cost_center"]),
                department_owner=row.get("manager"),
                authorized_email=row.get("manager"),
                po_description=row.get("description"),
                po_line_amount=row.get("po_line_amount"),
                cumulative_gr=row.get("cumulative_gr_amount"),
                currency=row.get("currency"),
                previous_completion_pct=previous_pct,
                previous_baseline_period=(
                    date.fromisoformat(f"{previous_month}-01")
                    if previous_pct is not None
                    else None
                ),
                baseline_status=baseline_status,
                po_status=row.get("po_status"),
                lifecycle_status=LIFECYCLE_ACTIVE,
            )
        )

    return population


# ---------------------------------------------------------------------------
# Line validation
# ---------------------------------------------------------------------------


def validate_submission_lines(
    submission: ParsedSubmission,
    expected_rows: list[dict],
    previous_baselines: dict,
    reporting_month: str,
) -> list[SubmissionLineRecord]:
    """Validate every submitted line against the expected population.

    Returns one :class:`SubmissionLineRecord` per submitted line.
    """
    expected_by_key = {
        (str(row["po_number"]), int(row["po_line"])): row
        for row in expected_rows
    }

    duplicates = mark_duplicate_lines(submission.lines)

    records: list[SubmissionLineRecord] = []

    for line in submission.lines:
        interpretation = line.interpretation

        po_line = _to_int(line.po_line)
        key = (line.po_number, po_line)

        expected = expected_by_key.get(key)

        exception_codes = list(interpretation.exception_codes) if interpretation else []

        previous_pct = None
        baseline_status = "MISSING"

        if expected is not None:
            previous_pct = expected.get("previous_completion_pct")
            baseline_status = expected.get("baseline_status", "MISSING")
        else:
            exception_codes.append(ExceptionCode.UNKNOWN_PO_LINE.value)

        if (line.po_number, po_line) in duplicates:
            if ExceptionCode.DUPLICATE_PO_LINE.value not in exception_codes:
                exception_codes.append(ExceptionCode.DUPLICATE_PO_LINE.value)

        current_pct = interpretation.effective_pct if interpretation else None

        comparison = compare_with_previous(
            current_pct=current_pct,
            previous_pct=previous_pct,
            remarks=line.remarks,
            baseline_period=(
                expected.get("previous_baseline_period").isoformat()
                if expected and expected.get("previous_baseline_period")
                else None
            ),
            baseline_status=baseline_status,
        )

        exception_codes.extend(comparison.exception_codes)

        # ----------------------------------------------- validation status
        if interpretation is None:
            validation_status = "MISSING"
        elif interpretation.status is FormatStatus.ACCEPTED:
            validation_status = "VALID"
        elif interpretation.status is FormatStatus.MISSING:
            validation_status = "MISSING"
        elif interpretation.status is FormatStatus.INVALID:
            validation_status = "INVALID"
        else:
            validation_status = "FORMAT_CONFIRMATION_REQUIRED"

        if expected is None:
            validation_status = "UNKNOWN_PO_LINE"

        # -------------------------------------------- format confirmation
        if interpretation and interpretation.needs_confirmation:
            format_confirmation_status = "REQUIRED"
        else:
            format_confirmation_status = "NOT_REQUIRED"

        # ------------------------------------------------------- acceptance
        accepted_current_pct = None

        # A valid, unambiguous value is accepted as it is.  A missing
        # baseline is an exception for the audit trail, but it does not
        # invalidate an otherwise valid submitted value.
        if (
            validation_status == "VALID"
            and current_pct is not None
            and comparison.reason_status
            in (
                ReasonStatus.NOT_APPLICABLE,
                ReasonStatus.REVIEW_REQUIRED,
            )
        ):
            accepted_current_pct = current_pct

        records.append(
            SubmissionLineRecord(
                po_number=line.po_number,
                po_line=po_line,
                source_sheet=line.source_sheet,
                source_row=line.source_row,
                raw_value=line.raw_value,
                raw_number_format=line.raw_number_format,
                raw_data_type=line.raw_data_type,
                normalized_pct=interpretation.normalized_pct if interpretation else None,
                suggested_pct=interpretation.suggested_pct if interpretation else None,
                format_detection=interpretation.detection.value if interpretation else None,
                format_confirmation_status=format_confirmation_status,
                previous_pct=previous_pct,
                change_percentage_points=comparison.change_percentage_points,
                remarks=line.remarks,
                reason_status=comparison.reason_status.value,
                validation_status=validation_status,
                exception_codes=sorted(set(exception_codes)),
                confidence=interpretation.confidence if interpretation else None,
                evidence=interpretation.evidence if interpretation else [],
                accepted_current_pct=accepted_current_pct,
            )
        )

    return records


def build_missing_line_records(
    expected_rows: list[dict],
    submitted_keys: set,
    reporting_month: str,
    legal_entity: str,
) -> list[SubmissionLineRecord]:
    """Create MISSING records for the expected lines that were not submitted."""
    records = []

    for row in expected_rows:
        key = (str(row["po_number"]), int(row["po_line"]))

        if key in submitted_keys:
            continue

        previous_pct = row.get("previous_completion_pct")

        records.append(
            SubmissionLineRecord(
                po_number=key[0],
                po_line=key[1],
                source_sheet=None,
                source_row=None,
                raw_value=None,
                raw_number_format=None,
                raw_data_type=None,
                normalized_pct=None,
                suggested_pct=None,
                format_detection="BLANK",
                format_confirmation_status="NOT_REQUIRED",
                previous_pct=previous_pct,
                change_percentage_points=None,
                remarks=None,
                reason_status=ReasonStatus.NOT_APPLICABLE.value,
                validation_status="MISSING",
                exception_codes=[ExceptionCode.MISSING_LINE.value],
                confidence="HIGH",
                evidence=[
                    "The expected PO line was not present in the submitted "
                    "workbook."
                ],
                accepted_current_pct=None,
            )
        )

    return records


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


# ---------------------------------------------------------------------------
# Main processing entry point
# ---------------------------------------------------------------------------


def process_inbound_message(
    gmail_message_id: str,
    reporting_month: str,
    raw_bytes: bytes,
    message: dict,
    subject: str | None = None,
    legal_entity: str = "2000",
    service=None,
) -> ProcessingResult:
    """Process one inbound reply end to end.

    The function is idempotent.  When the message has already been
    registered it is skipped and no second submission is created.
    """
    from backend.app.services.email_archive_service import extract_address, extract_headers

    headers = extract_headers(message)
    sender = extract_address(headers.get("from"))
    thread_id = message.get("threadId")

    result = ProcessingResult(
        gmail_message_id=gmail_message_id,
        reporting_month=reporting_month,
        status="DISCOVERED",
    )

    # ------------------------------------------------------- idempotency
    is_new, _ = register_gmail_message(
        gmail_message_id=gmail_message_id,
        reporting_month=reporting_month,
        gmail_thread_id=thread_id,
        sender_address=sender,
        recipient_address=extract_address(headers.get("to")),
        subject=subject or headers.get("subject"),
        message_date=headers.get("date"),
    )

    if not is_new:
        result.status = "SKIPPED_DUPLICATE"
        return result

    # ---------------------------------------------------------- authorize
    authorized = _check_sender_authorization(
        reporting_month, legal_entity, sender, thread_id
    )

    if not authorized["authorized"]:
        update_gmail_processing(
            gmail_message_id,
            processing_status="SKIPPED_UNAUTHORIZED",
            authorization_status="UNAUTHORIZED",
            error_message=authorized["reason"],
        )
        result.status = "SKIPPED_UNAUTHORIZED"
        result.error_message = authorized["reason"]
        return result

    # ----------------------------------------------------------- archive
    try:
        archived = archive_message(
            raw_bytes=raw_bytes,
            message=message,
            gmail_message_id=gmail_message_id,
            reporting_month=reporting_month,
            subject=subject,
        )
    except ArchiveError as exc:
        update_gmail_processing(
            gmail_message_id,
            processing_status="FAILED",
            error_message=str(exc),
        )
        result.status = "FAILED"
        result.error_message = str(exc)
        return result

    result.eml_path = archived.eml_path

    archive_id = persist_email_archive(
        gmail_message_id=gmail_message_id,
        reporting_month=reporting_month,
        eml_path=archived.eml_path,
        eml_sha256=archived.eml_sha256,
        eml_size_bytes=archived.eml_size_bytes,
        gmail_thread_id=thread_id,
        subject=archived.subject,
        sender_address=archived.from_address,
        recipient_address=archived.to_address,
        message_date=archived.date_header,
    )

    persist_email_attachments(
        archive_id=archive_id,
        gmail_message_id=gmail_message_id,
        attachments=[
            {
                "original_filename": attachment.original_filename,
                "stored_filename": attachment.stored_filename,
                "stored_path": attachment.stored_path,
                "sha256": attachment.sha256,
                "size_bytes": attachment.size_bytes,
                "content_type": attachment.content_type,
                "status": (
                    "ARCHIVED"
                    if attachment.stored_path
                    else "REJECTED"
                ),
            }
            for attachment in archived.attachments
        ],
    )

    update_gmail_processing(
        gmail_message_id,
        processing_status="ARCHIVED",
        authorization_status=authorized["status"],
    )

    # ----------------------------------------------------------- parse
    workbook_attachment = _select_workbook(archived)

    if workbook_attachment is None:
        update_gmail_processing(
            gmail_message_id,
            processing_status="FAILED",
            error_message="No usable Excel attachment found.",
        )
        result.status = "FAILED"
        result.error_message = "No usable Excel attachment found."
        return result

    result.attachment_path = workbook_attachment["stored_path"]

    try:
        parsed = parse_completion_workbook(workbook_attachment["stored_path"])
    except SubmissionParseError as exc:
        update_gmail_processing(
            gmail_message_id,
            processing_status="FAILED",
            error_message=str(exc),
        )
        result.status = "FAILED"
        result.error_message = str(exc)
        return result

    update_gmail_processing(
        gmail_message_id, processing_status="PARSED"
    )

    # --------------------------------------------------------- determine
    department = _resolve_department(
        reporting_month, legal_entity, parsed
    )

    if department is None:
        update_gmail_processing(
            gmail_message_id,
            processing_status="FAILED",
            error_message=(
                "The submitted workbook does not match any expected "
                "department for this reporting month."
            ),
        )
        result.status = "FAILED"
        result.error_message = "Unknown department."
        return result

    result.department_id = department["department_id"]
    result.department_name = department["department_name"]

    expected_rows = load_expected_population(
        reporting_month,
        legal_entity=legal_entity,
        department_id=department["department_id"],
    )

    previous_baselines = load_previous_baselines(
        reporting_month,
        legal_entity,
        previous_month_of(reporting_month),
    )

    # --------------------------------------------------------- validate
    line_records = validate_submission_lines(
        parsed,
        expected_rows,
        previous_baselines,
        reporting_month,
    )

    submitted_keys = {
        (record.po_number, record.po_line) for record in line_records
    }

    line_records.extend(
        build_missing_line_records(
            expected_rows,
            submitted_keys,
            reporting_month,
            legal_entity,
        )
    )

    update_gmail_processing(
        gmail_message_id, processing_status="VALIDATED"
    )

    # --------------------------------------------------------- persist
    record = SubmissionRecord(
        gmail_message_id=gmail_message_id,
        reporting_month=reporting_month,
        legal_entity=legal_entity,
        department_id=department["department_id"],
        department_name=department["department_name"],
        cost_center=department.get("cost_center"),
        gmail_thread_id=thread_id,
        archive_id=archive_id,
        source_attachment_sha256=workbook_attachment["sha256"],
        source_attachment_path=workbook_attachment["stored_path"],
        source_sheet=parsed.sheet_name,
        submitted_by=sender,
        submitted_at=archived.date_header,
        processing_status="PERSISTED",
        lines=line_records,
    )

    submission_id, version, created = persist_submission(record)

    result.submission_id = submission_id
    result.version = version
    result.expected_lines = len(line_records)
    result.accepted_lines = sum(
        1 for line in line_records if line.accepted_current_pct is not None
    )
    result.unresolved_lines = (
        result.expected_lines - result.accepted_lines
    )
    result.status = "PERSISTED" if created else "SKIPPED_DUPLICATE"

    update_gmail_processing(
        gmail_message_id, processing_status="PERSISTED"
    )

    return result


def _select_workbook(archived) -> dict | None:
    """Pick the Excel attachment that should be parsed."""
    candidates = [
        attachment
        for attachment in archived.attachments
        if attachment.stored_path
    ]

    if not candidates:
        return None

    for attachment in candidates:
        name = attachment.original_filename.lower()
        if name.endswith(SUPPORTED_ATTACHMENT_EXTENSIONS):
            return attachment.to_dict()

    return None


def _resolve_department(
    reporting_month: str,
    legal_entity: str,
    parsed: ParsedSubmission,
) -> dict | None:
    """Determine which department a submission belongs to.

    The department is derived from the frozen expected population, never
    from the content of the submitted file.
    """
    cost_centers = {
        line.cost_center
        for line in parsed.lines
        if line.cost_center
    }

    with connect() as conn:
        with conn.cursor() as cur:
            for cost_center in sorted(cost_centers):
                cur.execute(
                    """
                    SELECT
                        department_id,
                        department_name,
                        cost_center
                    FROM expected_po_lines
                    WHERE reporting_month = %s
                      AND legal_entity = %s
                      AND cost_center = %s
                    LIMIT 1
                    """,
                    (
                        date.fromisoformat(f"{reporting_month}-01"),
                        legal_entity,
                        cost_center,
                    ),
                )
                row = cur.fetchone()

                if row is not None:
                    return row

    return None


def _check_sender_authorization(
    reporting_month: str,
    legal_entity: str,
    sender: str | None,
    thread_id: str | None,
) -> dict:
    """Verify that the sender is an authorized department contact."""
    month = date.fromisoformat(f"{reporting_month}-01")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT authorized_email, delegate_email
                FROM department_contacts
                WHERE active = TRUE
                """
            )
            contacts = list(cur.fetchall())

            authorized_emails = {
                row["authorized_email"].lower()
                for row in contacts
                if row["authorized_email"]
            }
            delegate_emails = {
                row["delegate_email"].lower()
                for row in contacts
                if row["delegate_email"]
            }

            # The frozen expected population also carries the authorized
            # contact of every department.
            cur.execute(
                """
                SELECT DISTINCT authorized_email, department_owner
                FROM expected_po_lines
                WHERE reporting_month = %s
                """,
                (month,),
            )
            for row in cur.fetchall():
                if row["authorized_email"]:
                    authorized_emails.add(row["authorized_email"].lower())
                if row["department_owner"]:
                    authorized_emails.add(row["department_owner"].lower())

            # The sender of the original request thread is trusted for a
            # reply in the same thread, even when the contact list changed.
            if thread_id:
                cur.execute(
                    """
                    SELECT recipient
                    FROM email_requests
                    WHERE gmail_thread_id = %s
                      AND reporting_month = %s
                    LIMIT 1
                    """,
                    (thread_id, month),
                )
                request = cur.fetchone()

                if request and request["recipient"]:
                    authorized_emails.add(request["recipient"].lower())

    if not sender:
        return {
            "authorized": False,
            "status": "UNAUTHORIZED",
            "reason": "The message has no From address.",
        }

    normalized = sender.lower()

    if normalized in authorized_emails:
        return {"authorized": True, "status": "AUTHORIZED", "reason": None}

    if normalized in delegate_emails:
        return {
            "authorized": True,
            "status": "DELEGATED",
            "reason": "The sender is a registered delegate.",
        }

    return {
        "authorized": False,
        "status": "UNAUTHORIZED",
        "reason": f"The sender {sender} is not an authorized contact.",
    }
