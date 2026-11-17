
import argparse
import base64
import hashlib
import json
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.app.services.gmail_auth_service import get_gmail_service


DRAFT_ROOT = Path("backend/output/email_drafts")
TIMEZONE = ZoneInfo("America/Los_Angeles")


def send_capex_test(month, test_recipient):
    draft_file = DRAFT_ROOT / month / "2000-100.json"

    draft = json.loads(
        draft_file.read_text(encoding="utf-8")
    )

    if draft["status"] != "DRAFT":
        raise ValueError("Invalid draft status")

    if draft["reporting_month"] != month:
        raise ValueError("Reporting month mismatch")

    attachment_path = Path(draft["attachment_path"])

    attachment_bytes = attachment_path.read_bytes()

    actual_hash = hashlib.sha256(
        attachment_bytes
    ).hexdigest()

    if actual_hash != draft["attachment_sha256"]:
        raise RuntimeError("Attachment SHA-256 mismatch")

    if "{DUE_DATE}" not in draft["body"]:
        raise ValueError("Missing due-date placeholder")

    # Calculate the deadline using the sending date.
    send_date = datetime.now(TIMEZONE).date()
    due_date = send_date + timedelta(days=5)

    body = draft["body"].replace(
        "{DUE_DATE}",
        due_date.strftime("%B %d, %Y"),
    )

    message = EmailMessage()
    message["To"] = test_recipient
    message["Subject"] = "[TEST] " + draft["subject"].strip()
    message.set_content(body)

    message.add_attachment(
        attachment_bytes,
        maintype="application",
        subtype=(
            "vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        filename=draft["attachment_name"],
    )

    print("Recipient:", test_recipient)
    print("Subject:", message["Subject"])
    print("Attachment:", draft["attachment_name"])
    print("Due date:", due_date)

    service = get_gmail_service()

    raw = base64.urlsafe_b64encode(
        message.as_bytes()
    ).decode("ascii")

    result = service.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute()

    print("\nGmail API Send: SUCCESS")
    print("Message ID:", result["id"])
    print("Thread ID:", result.get("threadId"))
    print("Test only: no production send log created.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", required=True)
    parser.add_argument("--to", required=True)
    args = parser.parse_args()

    send_capex_test(args.month, args.to)
