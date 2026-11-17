
import argparse
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import psycopg

from backend.app.core.config import (
    DB_HOST,
    DB_PORT,
    DB_USER,
    DB_PASSWORD,
)

OUTPUT_ROOT = Path("backend/output/email_drafts")

DUE_DATE_PLACEHOLDER = "{DUE_DATE}"
RESPONSE_DAYS = 5


def sha256_file(path):
    digest = hashlib.sha256()

    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def calculate_due_date(sent_date):
    """
    Due date = actual email sent date + 5 calendar days.
    """
    return sent_date + timedelta(days=RESPONSE_DAYS)


def render_email_body(body_template, sent_date):
    """
    Replace the due-date placeholder when sending.
    """
    due_date = calculate_due_date(sent_date)
    due_date_label = due_date.strftime("%B %d, %Y")

    if DUE_DATE_PLACEHOLDER not in body_template:
        raise ValueError("Email body is missing due-date placeholder")

    return body_template.replace(
        DUE_DATE_PLACEHOLDER,
        due_date_label,
    )


def generate_email_drafts(reporting_month):
    month_date = date.fromisoformat(reporting_month + "-01")

    if month_date.strftime("%Y-%m") != reporting_month:
        raise ValueError("Month must be YYYY-MM")

    output_dir = OUTPUT_ROOT / reporting_month

    with psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname="accrualflow_db",
    ) as conn:

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    attachment_id,
                    manager,
                    cost_center,
                    department,
                    file_name,
                    file_path,
                    file_hash,
                    po_line_count
                FROM reminder_attachment_archive
                WHERE reporting_month = %s
                  AND version = 1
                  AND status = 'GENERATED'
                ORDER BY cost_center
                """,
                (month_date,),
            )

            records = cur.fetchall()

    if not records:
        raise RuntimeError(
            "No eligible reminder attachments found"
        )

    drafts = []
    seen_cost_centers = set()

    for record in records:
        (
            attachment_id,
            manager,
            cost_center,
            department,
            file_name,
            file_path,
            expected_hash,
            po_line_count,
        ) = record

        if cost_center in seen_cost_centers:
            raise RuntimeError(
                f"Duplicate cost center: {cost_center}"
            )

        seen_cost_centers.add(cost_center)

        path = Path(file_path)

        if not path.is_file():
            raise FileNotFoundError(path)

        actual_hash = sha256_file(path)

        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Attachment hash mismatch: {attachment_id}"
            )

        if not manager or "@" not in manager:
            raise ValueError(
                f"Invalid manager email: {manager}"
            )

        if not department:
            raise ValueError(
                f"Missing department: {cost_center}"
            )

        month_label = month_date.strftime("%B %Y")

        subject = (
            f"Action Required - {month_label} "
            f"CapEx Completion Update - {department}"
        ).strip()

        # Due date is intentionally not calculated
        # during draft generation.
        # It will be calculated using the actual
        # sent date when the email is sent.

        body = f"""Hi,


As part of the {month_label} month-end close process, Finance is collecting updated completion estimates for open CapEx purchase orders.

Please review the attached PO Completion Report for {department} and complete the following fields:

1. Current Estimated Completion % - Enter the cumulative completion percentage as of the end of {month_label}.
2. Comments / Explanation - Provide explanations for significant changes or discrepancies.

Please return the completed Excel file by {DUE_DATE_PLACEHOLDER}.

If you have any questions, please contact the Finance team.

Thank you for your support.


Best regards,
Finance Operations

"""

        drafts.append({
            "attachment_id": attachment_id,
            "reporting_month": reporting_month,
            "to": manager,
            "cost_center": cost_center,
            "department": department,
            "subject": subject,
            "body": body,
            "attachment_name": file_name,
            "attachment_path": str(path.resolve()),
            "attachment_sha256": expected_hash,
            "po_line_count": po_line_count,
            "response_days": RESPONSE_DAYS,
            "due_date": None,
            "sent_at": None,
            "status": "DRAFT",
        })

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    for draft in drafts:
        output_file = (
            output_dir / f"{draft['cost_center']}.json"
        )

        output_file.write_text(
            json.dumps(
                draft,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        print(
            f"DRAFT: {draft['cost_center']} "
            f"-> {draft['to']} "
            f"({draft['po_line_count']} PO Lines)"
        )

    print(f"\nTotal Email Drafts: {len(drafts)}")
    print(f"Output: {output_dir}")
    print("No emails were sent.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--month",
        required=True,
        help="Reporting month in YYYY-MM format",
    )

    args = parser.parse_args()

    generate_email_drafts(args.month)
