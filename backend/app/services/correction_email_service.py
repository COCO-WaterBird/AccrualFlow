"""
Correction request preparation and sending.

The default mode is **DRY_RUN**.  In that mode the email body and the
attachment are written to disk as a preview and the department status becomes
``CORRECTION_PREPARED``.  Nothing is sent.

Only when ``DRY_RUN`` is explicitly disabled *and* the Gmail API confirms the
send does the status become ``CORRECTION_REQUESTED``.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.app.core.config import (
    CORRECTION_DIR,
    DRY_RUN,
    MAX_CORRECTION_ATTEMPTS,
    REPORTING_TIMEZONE,
    ensure_runtime_dirs,
)


RESPONSE_DAYS = 5

# Exceptions that cannot be fixed by editing a percentage cell.
REVIEW_ONLY_CODES = frozenset(
    {
        "UNKNOWN_PO_LINE",
        "DUPLICATE_PO_LINE",
        "DEPARTMENT_MISMATCH",
        "PERIOD_MISMATCH",
        "CORRUPT_ATTACHMENT",
        "EMPTY_ATTACHMENT",
        "UNSUPPORTED_FILE_TYPE",
        "UNAUTHORIZED_SENDER",
    }
)

# Exceptions that require an explanation but can otherwise be corrected.
EXPLANATION_CODES = frozenset(
    {
        "DECREASE_REASON_MISSING",
        "DECREASE_REASON_PROVIDED",
        "BASELINE_MISSING",
    }
)


class CorrectionEmailError(Exception):
    """Raised when a correction request cannot be prepared or sent."""


@dataclass
class CorrectionDraft:
    """A prepared correction request."""

    reporting_month: str
    department_id: str
    department_name: str
    cost_center: str | None
    recipient: str
    subject: str
    body: str
    attachment_path: str
    attachment_name: str
    attachment_sha256: str
    gmail_thread_id: str | None = None
    due_date: str | None = None
    draft_path: str | None = None
    dry_run: bool = True
    sent: bool = False
    gmail_message_id: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict:
        return {
            "reporting_month": self.reporting_month,
            "department_id": self.department_id,
            "department_name": self.department_name,
            "cost_center": self.cost_center,
            "recipient": self.recipient,
            "subject": self.subject,
            "body": self.body,
            "attachment_path": self.attachment_path,
            "attachment_name": self.attachment_name,
            "attachment_sha256": self.attachment_sha256,
            "gmail_thread_id": self.gmail_thread_id,
            "due_date": self.due_date,
            "draft_path": self.draft_path,
            "dry_run": self.dry_run,
            "sent": self.sent,
            "gmail_message_id": self.gmail_message_id,
            "error_message": self.error_message,
        }


def build_subject(reporting_month: str, department_name: str) -> str:
    """Build the correction request subject."""
    month_date = date.fromisoformat(f"{reporting_month}-01")
    label = month_date.strftime("%B %Y")
    return (
        f"Action Required - Correction Request - {label} "
        f"CapEx Completion Update - {department_name}"
    ).strip()


def build_body(
    reporting_month: str,
    department_name: str,
    unresolved_count: int,
    exception_count: int,
    due_date: date,
) -> str:
    """Build the correction request body."""
    month_date = date.fromisoformat(f"{reporting_month}-01")
    label = month_date.strftime("%B %Y")
    due_date_label = due_date.strftime("%B %d, %Y")

    lines = [
        f"Hi {department_name} team,",
        "",
        f"We received your CapEx completion update for {label}. "
        "Thank you for the quick turnaround.",
        "",
        f"After validation, {unresolved_count} of your PO line(s) still need "
        "to be completed or corrected:"
        "",
    ]

    if unresolved_count:
        lines.append(
            "- The Current Estimated Completion % is missing, invalid or in "
            "an ambiguous format."
        )
        lines.append(
            "- The completion decreased compared with the previous month "
            "and an explanation is required."
        )

    if exception_count:
        lines.append("")
        lines.append(
            f"In addition, {exception_count} submitted line(s) could not be "
            "matched to the expected report and are listed on the "
            "'Exception_Detail' sheet for review."
        )

    lines.extend([
        "",
        "The attached workbook contains only the PO lines that need your "
        "attention. Every other line of your original report has already "
        "been accepted and does not need to be sent again.",
        "",
        "Please complete the 'Corrected Completion %' and "
        "'Corrected Explanation' columns on the 'Correction_Detail' sheet "
        "and return the workbook by "
        f"{due_date_label}.",
        "",
        "If you have any questions, please contact the Finance team.",
        "",
        "Best regards,",
        "Finance Operations",
        "",
    ])

    return "\n".join(lines)


def prepare_correction_draft(
    reporting_month: str,
    department_id: str,
    department_name: str,
    cost_center: str | None,
    recipient: str,
    workbook_path: str | Path,
    workbook_sha256: str,
    unresolved_count: int,
    exception_count: int = 0,
    gmail_thread_id: str | None = None,
    attempt: int = 1,
    dry_run: bool | None = None,
    output_dir: Path | None = None,
) -> CorrectionDraft:
    """Prepare a correction request.

    The draft is written to disk as a JSON preview plus an ``.eml`` file.
    No email is sent by this function.
    """
    if not recipient or "@" not in recipient:
        raise CorrectionEmailError(
            f"Invalid recipient for {department_id}: {recipient!r}"
        )

    path = Path(workbook_path)

    if not path.is_file():
        raise CorrectionEmailError(f"Correction workbook not found: {path}")

    actual_hash = hashlib_sha256(path)

    if actual_hash != workbook_sha256:
        raise CorrectionEmailError(
            f"Correction workbook hash mismatch: {path.name}"
        )

    ensure_runtime_dirs()

    base_dir = output_dir or CORRECTION_DIR
    month_dir = base_dir / reporting_month
    month_dir.mkdir(parents=True, exist_ok=True)

    timezone = ZoneInfo(REPORTING_TIMEZONE)
    today = datetime.now(timezone).date()
    due_date = today + timedelta(days=RESPONSE_DAYS)

    subject = build_subject(reporting_month, department_name)
    body = build_body(
        reporting_month,
        department_name,
        unresolved_count,
        exception_count,
        due_date,
    )

    effective_dry_run = DRY_RUN if dry_run is None else dry_run

    draft = CorrectionDraft(
        reporting_month=reporting_month,
        department_id=department_id,
        department_name=department_name,
        cost_center=cost_center,
        recipient=recipient,
        subject=subject,
        body=body,
        attachment_path=str(path),
        attachment_name=path.name,
        attachment_sha256=workbook_sha256,
        gmail_thread_id=gmail_thread_id,
        due_date=due_date.isoformat(),
        dry_run=effective_dry_run,
    )

    # ------------------------------------------------------ write preview
    safe_department = _safe_component(cost_center or department_id)
    draft_file = month_dir / f"{safe_department}_correction_v{attempt}.json"
    eml_file = month_dir / f"{safe_department}_correction_v{attempt}.eml"

    draft.draft_path = str(draft_file)

    draft_file.write_text(
        json.dumps(draft.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = subject
    message["In-Reply-To"] = gmail_thread_id or ""
    message.set_content(body)
    message.add_attachment(
        path.read_bytes(),
        maintype="application",
        subtype=(
            "vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        filename=path.name,
    )

    eml_file.write_bytes(message.as_bytes())

    return draft


def send_correction_draft(
    draft: CorrectionDraft,
    gmail_service=None,
) -> CorrectionDraft:
    """Send a prepared correction draft through the Gmail API.

    The draft is only marked as sent when the Gmail API confirms the send.
    """
    if draft.dry_run:
        raise CorrectionEmailError(
            "DRY_RUN is enabled. Refusing to send a correction request."
        )

    if draft.gmail_thread_id:
        # A reply is threaded; the caller supplies the service that knows
        # how to construct a threaded message.
        pass

    if gmail_service is None:
        from backend.app.services.gmail_auth_service import get_gmail_service

        gmail_service = get_gmail_service()

    message = EmailMessage()
    message["To"] = draft.recipient
    message["Subject"] = draft.subject
    message.set_content(draft.body)
    message.add_attachment(
        Path(draft.attachment_path).read_bytes(),
        maintype="application",
        subtype=(
            "vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        filename=draft.attachment_name,
    )

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    try:
        result = gmail_service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()
    except Exception as exc:  # noqa: BLE001 - Gmail transport failure
        draft.sent = False
        draft.error_message = f"{type(exc).__name__}: {exc}"
        return draft

    draft.sent = True
    draft.gmail_message_id = result.get("id")
    draft.error_message = None

    return draft


def check_correction_attempt_limit(
    existing_attempts: int,
    limit: int | None = None,
) -> bool:
    """Return True when another correction attempt is still allowed."""
    maximum = MAX_CORRECTION_ATTEMPTS if limit is None else limit
    return existing_attempts < maximum


def hashlib_sha256(path: Path) -> str:
    """Return the SHA-256 of a file."""
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_component(value: str) -> str:
    import re

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "")
    return cleaned.strip("._-") or "unknown"


def write_dry_run_log(
    draft: CorrectionDraft,
    output_dir: Path | None = None,
) -> str:
    """Write an auditable DRY_RUN send log."""
    base_dir = output_dir or CORRECTION_DIR
    month_dir = base_dir / draft.reporting_month
    month_dir.mkdir(parents=True, exist_ok=True)

    safe_department = _safe_component(draft.cost_center or draft.department_id)
    log_file = month_dir / f"{safe_department}_correction_dry_run.json"

    record = draft.to_dict()
    record["status"] = "DRY_RUN"
    record["logged_at"] = datetime.now(ZoneInfo(REPORTING_TIMEZONE)).isoformat()

    log_file.write_text(
        json.dumps(record, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return str(log_file)