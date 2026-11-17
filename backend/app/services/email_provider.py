
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from uuid import uuid4


@dataclass
class SendResult:
    accepted: bool
    message_id: str | None
    accepted_at: datetime | None
    error_message: str | None = None


class MockEmailProvider:
    """
    Simulates an email provider accepting a message.

    No SMTP connection.
    No Gmail or Outlook API.
    No actual email delivery.
    """

    def send(self, message: EmailMessage) -> SendResult:
        if not message.get("To"):
            return SendResult(
                accepted=False,
                message_id=None,
                accepted_at=None,
                error_message="Missing recipient",
            )

        if not message.get("Subject"):
            return SendResult(
                accepted=False,
                message_id=None,
                accepted_at=None,
                error_message="Missing subject",
            )

        attachments = list(message.iter_attachments())

        if len(attachments) != 1:
            return SendResult(
                accepted=False,
                message_id=None,
                accepted_at=None,
                error_message="Expected exactly one attachment",
            )

        return SendResult(
            accepted=True,
            message_id=f"mock-{uuid4()}",
            accepted_at=datetime.now(timezone.utc),
        )
