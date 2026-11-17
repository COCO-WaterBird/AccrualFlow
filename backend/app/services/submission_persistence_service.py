"""
Persistence of submissions, submission lines, validation issues and the
expected PO line population.

Design rules
------------
* **Idempotency.**  A Gmail message id identifies exactly one logical
  submission.  Re-running the pipeline never creates a second submission
  for the same message.
* **Version precedence.**  Every submission carries an explicit version.
  A new version only supersedes the lines it actually contains; previously
  accepted lines survive untouched.
* **Transactional.**  Each persistence operation runs in a single
  transaction.  A failure rolls the whole operation back.
* **No invented values.**  A missing baseline is stored as NULL with an
  explicit ``baseline_status``.  An unaccepted line keeps
  ``accepted_current_pct`` NULL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.rows import dict_row

from backend.app.core.config import (
    ACCRUALFLOW_DB_NAME,
    DB_HOST,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
)


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


def connect(dbname: str | None = None):
    """Open a connection to the AccrualFlow database."""
    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=dbname or ACCRUALFLOW_DB_NAME,
        row_factory=dict_row,
    )


def _month_start(reporting_month: str) -> date:
    """Convert ``YYYY-MM`` into the first day of that month."""
    parsed = date.fromisoformat(f"{reporting_month}-01")
    if parsed.strftime("%Y-%m") != reporting_month:
        raise ValueError("Reporting month must use YYYY-MM format")
    return parsed


# ---------------------------------------------------------------------------
# Expected population
# ---------------------------------------------------------------------------


@dataclass
class ExpectedPopulationRow:
    """One row of the expected PO line population."""

    po_number: str
    po_line: int
    legal_entity: str
    department_id: str
    department_name: str
    cost_center: str
    department_owner: str | None = None
    authorized_email: str | None = None
    po_description: str | None = None
    po_line_amount: Decimal | None = None
    cumulative_gr: Decimal | None = None
    currency: str | None = None
    previous_completion_pct: Decimal | None = None
    previous_baseline_period: date | None = None
    baseline_status: str = "MISSING"

    # The ERP po_status of the line at the moment the population was
    # frozen.  It records the scope the population was built under.
    po_status: str | None = None

    # ACTIVE           the line is a live part of the reporting scope.
    # CLOSED_IN_MONTH  the line closed during this reporting month and is
    #                  retained here, marked as closed.
    lifecycle_status: str = "ACTIVE"


def ensure_reporting_period(
    reporting_month: str,
    entity: str = "2000",
) -> None:
    """Create the reporting period row if it does not exist yet."""
    month = _month_start(reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reporting_periods (
                    reporting_month, entity
                )
                VALUES (%s, %s)
                ON CONFLICT (reporting_month) DO NOTHING
                """,
                (month, entity),
            )


def freeze_expected_population(
    reporting_month: str,
    rows: Sequence[ExpectedPopulationRow],
    source: str = "Mock ERP",
    entity: str = "2000",
) -> int:
    """Persist and freeze the expected PO line population for a month.

    The snapshot is written exactly once.  A second call with the same
    content is a no-op; a second call with different content raises, because
    a frozen population must never silently change underneath an in-flight
    submission.
    """
    month = _month_start(reporting_month)

    ensure_reporting_period(reporting_month, entity)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT population_frozen, population_row_count
                FROM reporting_periods
                WHERE reporting_month = %s
                FOR UPDATE
                """,
                (month,),
            )
            record = cur.fetchone()

            if record and record["population_frozen"]:
                if record["population_row_count"] != len(rows):
                    raise RuntimeError(
                        "The expected population for "
                        f"{reporting_month} is already frozen with "
                        f"{record['population_row_count']} rows; refusing to "
                        f"replace it with {len(rows)} rows."
                    )
                return len(rows)

            cur.executemany(
                """
                INSERT INTO expected_po_lines (
                    reporting_month, legal_entity,
                    department_id, department_name, cost_center,
                    department_owner, authorized_email,
                    po_number, po_line, po_description,
                    po_line_amount, cumulative_gr, currency,
                    previous_completion_pct,
                    previous_baseline_period,
                    baseline_status,
                    po_status, lifecycle_status
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (
                    reporting_month, legal_entity,
                    po_number, po_line
                )
                DO UPDATE SET
                    department_id = EXCLUDED.department_id,
                    department_name = EXCLUDED.department_name,
                    cost_center = EXCLUDED.cost_center,
                    department_owner = EXCLUDED.department_owner,
                    authorized_email = EXCLUDED.authorized_email,
                    po_description = EXCLUDED.po_description,
                    po_line_amount = EXCLUDED.po_line_amount,
                    cumulative_gr = EXCLUDED.cumulative_gr,
                    currency = EXCLUDED.currency,
                    previous_completion_pct = EXCLUDED.previous_completion_pct,
                    previous_baseline_period = EXCLUDED.previous_baseline_period,
                    baseline_status = EXCLUDED.baseline_status,
                    po_status = EXCLUDED.po_status,
                    lifecycle_status = EXCLUDED.lifecycle_status
                """,
                [
                    (
                        month,
                        row.legal_entity,
                        row.department_id,
                        row.department_name,
                        row.cost_center,
                        row.department_owner,
                        row.authorized_email,
                        row.po_number,
                        row.po_line,
                        row.po_description,
                        row.po_line_amount,
                        row.cumulative_gr,
                        row.currency,
                        row.previous_completion_pct,
                        row.previous_baseline_period,
                        row.baseline_status,
                        row.po_status,
                        row.lifecycle_status,
                    )
                    for row in rows
                ],
            )

            cur.execute(
                """
                UPDATE reporting_periods
                SET population_frozen = TRUE,
                    population_frozen_at = NOW(),
                    population_source = %s,
                    population_row_count = %s
                WHERE reporting_month = %s
                """,
                (source, len(rows), month),
            )

    return len(rows)


def load_expected_population(
    reporting_month: str,
    legal_entity: str | None = None,
    department_id: str | None = None,
) -> list[dict]:
    """Load the frozen expected population for a reporting month."""
    month = _month_start(reporting_month)

    clauses = ["reporting_month = %s"]
    params: list[Any] = [month]

    if legal_entity:
        clauses.append("legal_entity = %s")
        params.append(legal_entity)

    if department_id:
        clauses.append("department_id = %s")
        params.append(department_id)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                FROM expected_po_lines
                WHERE {' AND '.join(clauses)}
                ORDER BY
                    legal_entity, department_id,
                    po_number, po_line
                """,
                params,
            )
            return list(cur.fetchall())


def load_previous_baselines(
    reporting_month: str,
    legal_entity: str,
    previous_month: str,
) -> dict[tuple[str, int], Decimal]:
    """Load the previous finalized baseline for each PO line.

    The previous month is derived as the reporting month minus one calendar
    month.  Only finalized baselines are used.  A missing baseline is simply
    absent from the returned mapping - it is never defaulted to 0.
    """
    month = _month_start(reporting_month)
    previous = _month_start(previous_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT po_number, po_line, accepted_completion_pct
                FROM monthly_completion_baselines
                WHERE reporting_month = %s
                  AND legal_entity = %s
                """,
                (previous, legal_entity),
            )
            return {
                (row["po_number"], row["po_line"]): row["accepted_completion_pct"]
                for row in cur.fetchall()
            }


def previous_month_of(reporting_month: str) -> str:
    """Return the calendar month before ``reporting_month`` as ``YYYY-MM``."""
    month = _month_start(reporting_month)
    if month.month == 1:
        return f"{month.year - 1:04d}-12"
    return f"{month.year:04d}-{month.month - 1:02d}"


# ---------------------------------------------------------------------------
# Gmail processing ledger
# ---------------------------------------------------------------------------


def register_gmail_message(
    gmail_message_id: str,
    reporting_month: str,
    gmail_thread_id: str | None = None,
    sender_address: str | None = None,
    recipient_address: str | None = None,
    subject: str | None = None,
    message_date: Any = None,
) -> tuple[bool, int | None]:
    """Register an inbound Gmail message.

    Returns ``(is_new, processing_id)``.  ``is_new`` is False when the
    message was already registered, in which case the pipeline must not
    process it again.
    """
    month = _month_start(reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gmail_message_processing (
                    gmail_message_id, gmail_thread_id,
                    reporting_month, sender_address,
                    recipient_address, subject, message_date
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (gmail_message_id) DO NOTHING
                RETURNING processing_id
                """,
                (
                    gmail_message_id,
                    gmail_thread_id,
                    month,
                    sender_address,
                    recipient_address,
                    subject,
                    message_date,
                ),
            )
            record = cur.fetchone()

            if record is None:
                cur.execute(
                    """
                    SELECT processing_id
                    FROM gmail_message_processing
                    WHERE gmail_message_id = %s
                    """,
                    (gmail_message_id,),
                )
                existing = cur.fetchone()
                return False, existing["processing_id"] if existing else None

            return True, record["processing_id"]


def update_gmail_processing(
    gmail_message_id: str,
    processing_status: str | None = None,
    authorization_status: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update the processing state of a registered Gmail message."""
    assignments = ["processed_at = NOW()"]
    params: list[Any] = []

    if processing_status is not None:
        assignments.append("processing_status = %s")
        params.append(processing_status)

    if authorization_status is not None:
        assignments.append("authorization_status = %s")
        params.append(authorization_status)

    if error_message is not None:
        assignments.append("error_message = %s")
        params.append(error_message)

    params.append(gmail_message_id)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE gmail_message_processing
                SET {', '.join(assignments)}
                WHERE gmail_message_id = %s
                """,
                params,
            )


# ---------------------------------------------------------------------------
# Email archives
# ---------------------------------------------------------------------------


def persist_email_archive(
    gmail_message_id: str,
    reporting_month: str,
    eml_path: str,
    eml_sha256: str,
    eml_size_bytes: int,
    gmail_thread_id: str | None = None,
    subject: str | None = None,
    sender_address: str | None = None,
    recipient_address: str | None = None,
    message_date: Any = None,
    direction: str = "INBOUND",
) -> int:
    """Persist the archive metadata of one message. Idempotent."""
    month = _month_start(reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO email_archives (
                    gmail_message_id, gmail_thread_id,
                    reporting_month, subject,
                    sender_address, recipient_address, message_date,
                    eml_path, eml_sha256, eml_size_bytes, direction
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (gmail_message_id, direction)
                DO UPDATE SET
                    gmail_thread_id = EXCLUDED.gmail_thread_id,
                    subject = EXCLUDED.subject,
                    sender_address = EXCLUDED.sender_address,
                    recipient_address = EXCLUDED.recipient_address,
                    message_date = EXCLUDED.message_date,
                    eml_path = EXCLUDED.eml_path,
                    eml_sha256 = EXCLUDED.eml_sha256,
                    eml_size_bytes = EXCLUDED.eml_size_bytes
                RETURNING archive_id
                """,
                (
                    gmail_message_id,
                    gmail_thread_id,
                    month,
                    subject,
                    sender_address,
                    recipient_address,
                    message_date,
                    eml_path,
                    eml_sha256,
                    eml_size_bytes,
                    direction,
                ),
            )
            return cur.fetchone()["archive_id"]


def persist_email_attachments(
    archive_id: int,
    gmail_message_id: str,
    attachments: Iterable[dict],
) -> None:
    """Persist attachment metadata for one archived message."""
    rows = list(attachments)

    if not rows:
        return

    with connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO email_attachments (
                    archive_id, gmail_message_id,
                    original_filename, stored_filename, stored_path,
                    content_sha256, size_bytes, content_type, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (content_sha256, gmail_message_id)
                WHERE content_sha256 IS NOT NULL
                DO NOTHING
                """,
                [
                    (
                        archive_id,
                        gmail_message_id,
                        row.get("original_filename", ""),
                        row.get("stored_filename") or None,
                        row.get("stored_path") or None,
                        row.get("sha256") or None,
                        row.get("size_bytes", 0),
                        row.get("content_type"),
                        row.get("status", "ARCHIVED"),
                    )
                    for row in rows
                ],
            )


# ---------------------------------------------------------------------------
# Submissions
# ---------------------------------------------------------------------------


@dataclass
class SubmissionLineRecord:
    """One validated submission line ready to be persisted."""

    po_number: str
    po_line: int
    source_sheet: str | None = None
    source_row: int | None = None

    raw_value: Any = None
    raw_number_format: str | None = None
    raw_data_type: str | None = None

    normalized_pct: Decimal | None = None
    suggested_pct: Decimal | None = None

    format_detection: str | None = None
    format_confirmation_status: str = "NOT_REQUIRED"

    previous_pct: Decimal | None = None
    change_percentage_points: Decimal | None = None

    remarks: str | None = None
    reason_status: str = "NOT_APPLICABLE"

    validation_status: str = "VALID"
    exception_codes: list[str] = field(default_factory=list)

    confidence: str | None = None
    evidence: list[str] = field(default_factory=list)

    accepted_current_pct: Decimal | None = None


@dataclass
class SubmissionRecord:
    """A complete submission ready to be persisted."""

    gmail_message_id: str
    reporting_month: str
    legal_entity: str
    department_id: str
    department_name: str
    cost_center: str | None = None
    gmail_thread_id: str | None = None
    archive_id: int | None = None
    version: int | None = None
    source_attachment_sha256: str | None = None
    source_attachment_path: str | None = None
    source_sheet: str | None = None
    submitted_by: str | None = None
    submitted_at: Any = None
    processing_status: str = "PERSISTED"
    error_message: str | None = None
    lines: list[SubmissionLineRecord] = field(default_factory=list)


def _next_submission_version(
    cur,
    reporting_month: date,
    legal_entity: str,
    department_id: str,
) -> int:
    cur.execute(
        """
        SELECT COALESCE(MAX(version), 0) + 1 AS next_version
        FROM department_submissions
        WHERE reporting_month = %s
          AND legal_entity = %s
          AND department_id = %s
        """,
        (reporting_month, legal_entity, department_id),
    )
    return cur.fetchone()["next_version"]


def persist_submission(record: SubmissionRecord) -> tuple[int, int, bool]:
    """Persist one submission with all its lines.

    Returns ``(submission_id, version, created)``.  ``created`` is False
    when the same Gmail message had already been persisted, in which case
    nothing was written.
    """
    month = _month_start(record.reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            # Serialize concurrent writers for this department.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (
                    f"submission:{record.reporting_month}:"
                    f"{record.legal_entity}:{record.department_id}",
                ),
            )

            cur.execute(
                """
                SELECT submission_id, version
                FROM department_submissions
                WHERE gmail_message_id = %s
                ORDER BY version
                LIMIT 1
                """,
                (record.gmail_message_id,),
            )
            existing = cur.fetchone()

            if existing is not None:
                return existing["submission_id"], existing["version"], False

            version = record.version or _next_submission_version(
                cur, month, record.legal_entity, record.department_id
            )

            cur.execute(
                """
                INSERT INTO department_submissions (
                    gmail_message_id, gmail_thread_id, archive_id,
                    reporting_month, legal_entity,
                    department_id, department_name, cost_center,
                    version, source_attachment_sha256,
                    source_attachment_path, source_sheet,
                    submitted_by, submitted_at,
                    processing_status, error_message
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING submission_id
                """,
                (
                    record.gmail_message_id,
                    record.gmail_thread_id,
                    record.archive_id,
                    month,
                    record.legal_entity,
                    record.department_id,
                    record.department_name,
                    record.cost_center,
                    version,
                    record.source_attachment_sha256,
                    record.source_attachment_path,
                    record.source_sheet,
                    record.submitted_by,
                    record.submitted_at,
                    record.processing_status,
                    record.error_message,
                ),
            )
            submission_id = cur.fetchone()["submission_id"]

            line_ids: dict = {}

            for line in record.lines:
                cur.execute(
                    """
                    INSERT INTO submission_lines (
                        submission_id, reporting_month, legal_entity,
                        department_id, po_number, po_line,
                        source_sheet, source_row,
                        raw_value, raw_number_format, raw_data_type,
                        normalized_pct, suggested_pct,
                        format_detection, format_confirmation_status,
                        previous_pct, change_percentage_points,
                        remarks, reason_status,
                        validation_status, exception_codes,
                        confidence, evidence,
                        accepted_current_pct, accepted_at,
                        is_current_version
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, CASE WHEN %s::NUMERIC IS NOT NULL THEN NOW() END, TRUE
                    )
                    RETURNING submission_line_id
                    """,
                    (
                        submission_id,
                        month,
                        record.legal_entity,
                        record.department_id,
                        line.po_number,
                        line.po_line,
                        line.source_sheet,
                        line.source_row,
                        None if line.raw_value is None else str(line.raw_value),
                        line.raw_number_format,
                        line.raw_data_type,
                        line.normalized_pct,
                        line.suggested_pct,
                        line.format_detection,
                        line.format_confirmation_status,
                        line.previous_pct,
                        line.change_percentage_points,
                        line.remarks,
                        line.reason_status,
                        line.validation_status,
                        line.exception_codes,
                        line.confidence,
                        line.evidence,
                        line.accepted_current_pct,
                        line.accepted_current_pct,
                    ),
                )
                line_ids[id(line)] = cur.fetchone()["submission_line_id"]

            # The new version supersedes the previous version's lines for
            # this department, but the previous rows stay in the database so
            # the audit trail is complete.  Only rows of *other* submissions
            # are demoted; the rows just inserted stay current.
            cur.execute(
                """
                UPDATE submission_lines
                SET is_current_version = FALSE
                WHERE reporting_month = %s
                  AND legal_entity = %s
                  AND department_id = %s
                  AND is_current_version
                  AND submission_id <> %s
                """,
                (
                    month,
                    record.legal_entity,
                    record.department_id,
                    submission_id,
                ),
            )

            cur.executemany(
                """
                INSERT INTO validation_issues (
                    reporting_month, legal_entity, department_id,
                    submission_id, submission_line_id, po_number, po_line,
                    exception_code, severity, detail, evidence,
                    status
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN'
                )
                """,
                [
                    (
                        month,
                        record.legal_entity,
                        record.department_id,
                        submission_id,
                        line_ids[id(line)],
                        line.po_number,
                        line.po_line,
                        code,
                        _severity_for(code),
                        _detail_for(code),
                        line.evidence,
                    )
                    for line in record.lines
                    for code in line.exception_codes
                ],
            )

    return submission_id, version, True


def _severity_for(exception_code: str) -> str:
    """Map an exception code to a severity for reporting."""
    if exception_code in (
        "MISSING_VALUE",
        "MISSING_LINE",
        "DECREASE_REASON_MISSING",
    ):
        return "ERROR"
    if exception_code in (
        "INVALID_VALUE",
        "OUT_OF_RANGE",
        "NEGATIVE_VALUE",
        "NON_NUMERIC_VALUE",
        "FORMULA_CELL",
        "EXCEL_ERROR",
        "BASELINE_MISSING",
    ):
        return "ERROR"
    return "WARNING"


def _detail_for(exception_code: str) -> str:
    """Human readable description of an exception code."""
    return {
        "MISSING_VALUE": "The completion cell is blank.",
        "MISSING_LINE": "The expected PO line was not present in the submission.",
        "INVALID_VALUE": "The submitted value is not a valid completion percentage.",
        "OUT_OF_RANGE": "The submitted percentage is outside the valid 0-100% range.",
        "NEGATIVE_VALUE": "The submitted percentage is negative.",
        "NON_NUMERIC_VALUE": "The submitted value is not numeric.",
        "FORMULA_CELL": "The cell contains a formula instead of a value.",
        "EXCEL_ERROR": "The cell contains an Excel error.",
        "AMBIGUOUS_INTEGER": "The cell contains an integer without percent formatting.",
        "AMBIGUOUS_DECIMAL": "The cell contains a decimal without percent formatting.",
        "AMBIGUOUS_ZERO_OR_ONE": "The value 0 or 1 is ambiguous without percent formatting.",
        "AMBIGUOUS_MIXED_FORMATS": "The column mixes several percentage representations.",
        "UNKNOWN_PO_LINE": "The submitted PO line is not part of the expected population.",
        "DUPLICATE_PO_LINE": "The same PO line appears more than once.",
        "DEPARTMENT_MISMATCH": "The submission does not belong to the expected department.",
        "PERIOD_MISMATCH": "The submission does not belong to the expected reporting month.",
        "BASELINE_MISSING": "No finalized baseline exists for the previous month.",
        "DECREASE_REASON_MISSING": "The completion decreased but no explanation was provided.",
        "DECREASE_REASON_PROVIDED": "The completion decreased and an explanation awaits review.",
        "GR_RATIO_EXCEEDED": "The reported completion is below the cumulative goods receipt ratio.",
        "CORRUPT_ATTACHMENT": "The attachment could not be read as a workbook.",
        "EMPTY_ATTACHMENT": "The attachment is empty.",
        "UNSUPPORTED_FILE_TYPE": "The attachment is not a supported workbook.",
        "UNAUTHORIZED_SENDER": "The message was not sent by an authorized contact.",
    }.get(exception_code, exception_code)


# ---------------------------------------------------------------------------
# Accepted value selection
# ---------------------------------------------------------------------------


def build_accepted_dataset(
    reporting_month: str,
    legal_entity: str,
) -> list[dict]:
    """Return the currently accepted value of every expected PO line.

    The result always contains one row per expected PO line.  An unresolved
    line is present with a NULL accepted value; it is never dropped and
    never receives a fabricated value.
    """
    month = _month_start(reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
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
                    e.reporting_month,
                    e.legal_entity,
                    e.department_id,
                    e.department_name,
                    e.cost_center,
                    e.department_owner,
                    e.authorized_email,
                    e.po_number,
                    e.po_line,
                    e.po_description,
                    e.po_line_amount,
                    e.cumulative_gr,
                    e.currency,
                    e.previous_completion_pct,
                    e.previous_baseline_period,
                    e.baseline_status,

                    sl.submission_line_id,
                    sl.source_sheet,
                    sl.source_row,
                    sl.raw_value,
                    sl.raw_number_format,
                    sl.raw_data_type,
                    sl.normalized_pct,
                    sl.suggested_pct,
                    sl.format_detection,
                    sl.format_confirmation_status,
                    sl.change_percentage_points,
                    sl.remarks,
                    sl.reason_status,
                    sl.validation_status,
                    sl.exception_codes,
                    sl.accepted_current_pct,
                    sl.accepted_at,

                    ds.submission_id,
                    ds.gmail_message_id,
                    ds.gmail_thread_id,
                    ds.version,
                    ds.source_attachment_path,
                    ds.source_attachment_sha256,
                    ds.submitted_by,
                    ds.submitted_at,

                    ea.eml_path
                FROM expected_po_lines e
                LEFT JOIN current_lines sl
                  ON sl.po_number = e.po_number
                 AND sl.po_line   = e.po_line
                LEFT JOIN department_submissions ds
                  ON ds.submission_id = sl.submission_id
                LEFT JOIN email_archives ea
                  ON ea.archive_id = ds.archive_id
                WHERE e.reporting_month = %s
                  AND e.legal_entity = %s
                ORDER BY
                    e.department_id, e.po_number, e.po_line
                """,
                (month, legal_entity, month, legal_entity),
            )
            return list(cur.fetchall())


def load_unknown_po_lines(
    reporting_month: str,
    legal_entity: str,
) -> list[dict]:
    """Return the submitted PO lines that are not in the expected population.

    These lines are reported as ``UNKNOWN_PO_LINE``.  They are kept apart
    from the expected population so that the expected line count is never
    inflated by them.
    """
    month = _month_start(reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
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
                SELECT sl.*
                FROM current_lines sl
                LEFT JOIN expected_po_lines e
                  ON e.reporting_month = sl.reporting_month
                 AND e.legal_entity   = sl.legal_entity
                 AND e.po_number      = sl.po_number
                 AND e.po_line        = sl.po_line
                WHERE e.po_number IS NULL
                ORDER BY sl.po_number, sl.po_line
                """,
                (month, legal_entity),
            )
            return list(cur.fetchall())


def finalize_baselines(
    reporting_month: str,
    legal_entity: str,
) -> int:
    """Write the accepted values of a month into the baseline table.

    Only lines with an accepted value produce a baseline.  A line without an
    accepted value produces no baseline, so the next month correctly reports
    ``BASELINE_MISSING`` for it.
    """
    month = _month_start(reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO monthly_completion_baselines (
                    reporting_month, legal_entity,
                    po_number, po_line, accepted_completion_pct,
                    source_submission_id
                )
                SELECT
                    sl.reporting_month,
                    sl.legal_entity,
                    sl.po_number,
                    sl.po_line,
                    sl.accepted_current_pct,
                    sl.submission_id
                FROM submission_lines sl
                WHERE sl.reporting_month = %s
                  AND sl.legal_entity = %s
                  AND sl.is_current_version
                  AND sl.accepted_current_pct IS NOT NULL
                ON CONFLICT (
                    reporting_month, legal_entity,
                    po_number, po_line
                )
                DO UPDATE SET
                    accepted_completion_pct = EXCLUDED.accepted_completion_pct,
                    source_submission_id = EXCLUDED.source_submission_id,
                    finalized_at = NOW()
                """,
                (month, legal_entity),
            )
            return cur.rowcount


def get_submission(submission_id: int) -> dict | None:
    """Load one submission with its lines."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM department_submissions
                WHERE submission_id = %s
                """,
                (submission_id,),
            )
            return cur.fetchone()


def get_submission_lines(submission_id: int) -> list[dict]:
    """Load all lines of one submission."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM submission_lines
                WHERE submission_id = %s
                ORDER BY po_number, po_line, source_row
                """,
                (submission_id,),
            )
            return list(cur.fetchall())


def count_submissions_for_message(gmail_message_id: str) -> int:
    """Return how many submissions exist for a Gmail message id."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM department_submissions
                WHERE gmail_message_id = %s
                """,
                (gmail_message_id,),
            )
            return cur.fetchone()["count"]


if __name__ == "__main__":
    print("PostgreSQL persistence helpers loaded.")
    print("Database:", ACCRUALFLOW_DB_NAME)