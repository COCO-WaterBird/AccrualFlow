import argparse
import hashlib
import json
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path

from backend.app.services.email_provider import MockEmailProvider

import psycopg

from backend.app.core.config import (
    DB_HOST,
    DB_PORT,
    DB_USER,
    DB_PASSWORD,
)

DRAFT_ROOT = Path("backend/output/email_drafts")
MOCK_LOG_ROOT = Path("backend/output/mock_send_logs")


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

# NEW: Check PostgreSQL before sending
def check_existing_send(reporting_month, attachment_id):

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
                SELECT email_id, status, sent_at, provider_message_id
                FROM reminder_email_log
                WHERE reporting_month = %s::date
                  AND attachment_id = %s
                """,
                (reporting_month + "-01", attachment_id),
            )

            record = cur.fetchone()

    if record is None:
        print("Pre-send check: NO EXISTING RECORD")
        return True

    print("Pre-send check: BLOCKED - existing record")
    print("Record:", record)
    return False



def reserve_send(
    reporting_month,
    attachment_id,
    recipient,
    subject,
):
    """
    Reserve one initial reminder send in PostgreSQL.

    Returns email_id if reservation succeeds.
    Returns None if a reservation already exists.

    No Gmail API calls are made.
    """

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
                INSERT INTO reminder_email_log (
                    attachment_id,
                    reporting_month,
                    recipient,
                    subject,
                    status
                )
                VALUES (
                    %s,
                    %s::date,
                    %s,
                    %s,
                    'SENDING'
                )
                ON CONFLICT (
                    reporting_month,
                    attachment_id
                )
                DO NOTHING
                RETURNING email_id
                """,
                (
                    attachment_id,
                    reporting_month + "-01",
                    recipient,
                    subject.strip(),
                ),
            )

            record = cur.fetchone()

    if record is None:
        print("Reservation: BLOCKED - record already exists")
        return None

    email_id = record[0]

    print("Reservation: CREATED")
    print("Email ID:", email_id)
    print("Status: SENDING")

    return email_id


def dispatch_mock(reporting_month):
    draft_dir = DRAFT_ROOT / reporting_month
    log_dir = MOCK_LOG_ROOT / reporting_month

    json_files = sorted(draft_dir.glob("*.json"))

    if not json_files:
        raise RuntimeError("No email drafts found")

    provider = MockEmailProvider()
    log_dir.mkdir(parents=True, exist_ok=True)

    for json_file in json_files:
        draft = json.loads(json_file.read_text(encoding="utf-8"))

        if draft["status"] != "DRAFT":
            raise ValueError(f"Invalid status: {json_file.name}")

        if draft["reporting_month"] != reporting_month:
            raise ValueError(f"Month mismatch: {json_file.name}")

        attachment_path = Path(draft["attachment_path"])

        if not attachment_path.is_file():
            raise FileNotFoundError(attachment_path)

        if sha256_file(attachment_path) != draft["attachment_sha256"]:
            raise RuntimeError(
                f"Attachment hash mismatch: {json_file.name}"
            )

        log_file = log_dir / f"{draft['cost_center']}.json"

        # Skip previously accepted mock requests.
        if log_file.exists():
            previous = json.loads(
                log_file.read_text(encoding="utf-8")
            )
            if previous.get("status") == "MOCK_ACCEPTED":
                print(f"SKIP: {draft['cost_center']} - Already simulated")
                continue

        if "{DUE_DATE}" not in draft["body"]:
            raise ValueError(
                f"Missing due-date placeholder: {json_file.name}"
            )

        # This is a simulation. The actual acceptance date
        # is returned by the mock provider.
        message = EmailMessage()
        message["To"] = draft["to"]
        message["Subject"] = draft["subject"].strip()

        # Preliminary body for the mock provider.
        message.set_content(draft["body"])

        message.add_attachment(
            attachment_path.read_bytes(),
            maintype="application",
            subtype=(
                "vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            filename=draft["attachment_name"],
        )

        result = provider.send(message)

        if not result.accepted:
            print(
                f"FAILED: {draft['cost_center']} "
                f"- {result.error_message}"
            )
            continue

        accepted_date = result.accepted_at.date()
        due_date = accepted_date + timedelta(
            days=draft["response_days"]
        )

        final_body = draft["body"].replace(
            "{DUE_DATE}",
            due_date.strftime("%B %d, %Y"),
        )

        record = {
            "reporting_month": reporting_month,
            "attachment_id": draft["attachment_id"],
            "cost_center": draft["cost_center"],
            "department": draft["department"],
            "to": draft["to"],
            "subject": draft["subject"],
            "body": final_body,
            "attachment_name": draft["attachment_name"],
            "mock_message_id": result.message_id,
            "mock_accepted_at": result.accepted_at.isoformat(),
            "due_date": due_date.isoformat(),
            "status": "MOCK_ACCEPTED",
            "actual_sent_at": None,
        }

        log_file.write_text(
            json.dumps(record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        print(
            f"MOCK ACCEPTED: {draft['cost_center']} "
            f"-> {draft['to']} "
            f"| Due: {due_date}"
        )

    print("\nMock dispatch complete. No real emails were sent.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", required=True)
    args = parser.parse_args()

    dispatch_mock(args.month)
