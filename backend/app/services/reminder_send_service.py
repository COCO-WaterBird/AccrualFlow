
import argparse
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

OUTPUT_ROOT = Path("backend/output/email_drafts")
SEND_LOG_ROOT = Path("backend/output/email_send_logs")

RESPONSE_DAYS = 5
DUE_DATE_PLACEHOLDER = "{DUE_DATE}"


def sha256_file(path):
    digest = hashlib.sha256()

    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def simulate_send(reporting_month, send_date):
    draft_dir = OUTPUT_ROOT / reporting_month
    log_dir = SEND_LOG_ROOT / reporting_month

    json_files = sorted(draft_dir.glob("*.json"))

    if not json_files:
        raise RuntimeError("No email drafts found")

    due_date = send_date + timedelta(days=RESPONSE_DAYS)
    due_date_label = due_date.strftime("%B %d, %Y")

    # Validate all drafts before creating any send logs.
    prepared = []

    for json_file in json_files:
        draft = json.loads(
            json_file.read_text(encoding="utf-8")
        )

        if draft["status"] != "DRAFT":
            raise ValueError(
                f"Invalid draft status: {json_file.name}"
            )

        if draft["reporting_month"] != reporting_month:
            raise ValueError(
                f"Reporting month mismatch: {json_file.name}"
            )

        attachment_path = Path(draft["attachment_path"])

        if not attachment_path.is_file():
            raise FileNotFoundError(attachment_path)

        if sha256_file(attachment_path) != draft["attachment_sha256"]:
            raise RuntimeError(
                f"Attachment hash mismatch: {json_file.name}"
            )

        body = draft["body"]

        if DUE_DATE_PLACEHOLDER not in body:
            raise ValueError(
                f"Missing due date placeholder: {json_file.name}"
            )

        body = body.replace(
            DUE_DATE_PLACEHOLDER,
            due_date_label,
        )

        prepared.append((draft, body))

    log_dir.mkdir(parents=True, exist_ok=True)

    for draft, body in prepared:
        cost_center = draft["cost_center"]

        # A simulated send is not an actual email delivery.
        # Keep simulation logs separate from real send records.
        log_file = log_dir / f"{cost_center}_dry_run.json"

        if log_file.exists():
            print(f"SKIP: {cost_center} - Dry run already recorded")
            continue

        record = {
            "reporting_month": reporting_month,
            "attachment_id": draft["attachment_id"],
            "cost_center": cost_center,
            "department": draft["department"],
            "to": draft["to"],
            "subject": draft["subject"],
            "body": body,
            "attachment_name": draft["attachment_name"],
            "attachment_sha256": draft["attachment_sha256"],
            "simulated_send_date": send_date.isoformat(),
            "due_date": due_date.isoformat(),
            "response_days": RESPONSE_DAYS,
            "status": "DRY_RUN",
            "actual_sent_at": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        # Exclusive creation prevents overwriting an existing log.
        try:
            with log_file.open("x", encoding="utf-8") as f:
                json.dump(record, f, indent=2, ensure_ascii=False)
        except FileExistsError:
            print(f"SKIP: {cost_center} - Dry run already recorded")
            continue

        print(
            f"DRY RUN: {cost_center} -> {draft['to']} "
            f"| Due: {due_date.isoformat()}"
        )

    print("\nSimulation complete. No emails were sent.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("--month", required=True)
    parser.add_argument(
        "--send-date",
        required=True,
        help="Simulated sending date in YYYY-MM-DD format",
    )

    args = parser.parse_args()

    simulate_send(
        args.month,
        date.fromisoformat(args.send_date),
    )
