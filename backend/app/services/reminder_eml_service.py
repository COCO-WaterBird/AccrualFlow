
import argparse
import hashlib
import json
from email.message import EmailMessage
from pathlib import Path


OUTPUT_ROOT = Path("backend/output/email_drafts")


def sha256_file(path):
    digest = hashlib.sha256()

    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def generate_eml_files(reporting_month):
    output_dir = OUTPUT_ROOT / reporting_month

    json_files = sorted(output_dir.glob("*.json"))

    if not json_files:
        raise RuntimeError("No email drafts found")

    count = 0

    for json_file in json_files:
        draft = json.loads(
            json_file.read_text(encoding="utf-8")
        )

        if draft["status"] != "DRAFT":
            raise ValueError(
                f"Invalid draft status: {json_file}"
            )

        attachment_path = Path(draft["attachment_path"])

        if not attachment_path.is_file():
            raise FileNotFoundError(attachment_path)

        actual_hash = sha256_file(attachment_path)

        if actual_hash != draft["attachment_sha256"]:
            raise RuntimeError(
                f"Attachment hash mismatch: {attachment_path}"
            )

        if draft["reporting_month"] != reporting_month:
            raise ValueError(
                f"Reporting month mismatch: {json_file}"
            )

        message = EmailMessage()

        message["To"] = draft["to"]
        message["Subject"] = draft["subject"].strip()

        # This is a local preview, not a send-ready email.
        # The actual sending service must replace
        # {DUE_DATE} using the real sending date.
        message.set_content(draft["body"])

        message.add_attachment(
            attachment_path.read_bytes(),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=draft["attachment_name"],
        )

        eml_file = output_dir / f"{draft['cost_center']}.eml"

        eml_file.write_bytes(message.as_bytes())

        print(
            f"EML: {eml_file.name} "
            f"-> {draft['to']}"
        )

        count += 1

    print(f"\nTotal EML Files: {count}")
    print("No emails were sent.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--month",
        required=True,
    )

    args = parser.parse_args()

    generate_eml_files(args.month)
