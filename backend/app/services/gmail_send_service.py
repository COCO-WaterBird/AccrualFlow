
import argparse
import base64
from email.message import EmailMessage

from backend.app.services.gmail_auth_service import get_gmail_service


def send_test_email(recipient):
    service = get_gmail_service()

    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = "AccrualFlow AI - Gmail API Integration Test"

    message.set_content(
        "Hi,\n\n"
        "This is a test email sent from AccrualFlow AI "
        "using the Gmail API.\n\n"
        "No CapEx reporting action is required.\n\n"
        "Best regards,\n"
        "AccrualFlow AI"
    )

    raw_message = base64.urlsafe_b64encode(
        message.as_bytes()
    ).decode("utf-8")

    result = service.users().messages().send(
        userId="me",
        body={"raw": raw_message},
    ).execute()

    print("Gmail API Send: SUCCESS")
    print("Recipient:", recipient)
    print("Message ID:", result["id"])
    print("Thread ID:", result.get("threadId"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--to",
        required=True,
        help="Test recipient email address",
    )

    args = parser.parse_args()

    send_test_email(args.to)
