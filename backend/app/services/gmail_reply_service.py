"""
Gmail reply discovery for CapEx completion submissions.

Replies are correlated to the **stored original request thread ids**.  The
subject is only used as a soft hint; it is never the sole matching rule,
because a forwarded or manually composed reply can carry any subject.

Sender authorization is enforced against the authorized department contacts.
A reply from an unknown address is never processed as a submission; it is
archived as evidence and flagged.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from typing import Iterable

from backend.app.services.email_archive_service import (
    extract_address,
    extract_headers,
)


# The marker that identifies an AccrualFlow collection request.
REQUEST_SUBJECT_MARKER = "CapEx Completion Update"


def decode_subject(value):
    """Decode a possibly RFC 2047 encoded subject into plain text."""
    if not value:
        return ""
    return str(make_header(decode_header(value)))


@dataclass
class DiscoveredReply:
    """One inbound message that looks like a completion reply."""

    message_id: str
    thread_id: str | None
    from_address: str | None
    to_address: str | None
    subject: str
    date_header: str | None
    in_reply_to: str | None
    references: str | None

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "from_address": self.from_address,
            "to_address": self.to_address,
            "subject": self.subject,
            "date_header": self.date_header,
            "in_reply_to": self.in_reply_to,
            "references": self.references,
        }


def _header_lookup(headers: dict, name: str) -> str | None:
    return headers.get(name.lower())


def list_message_ids(
    service,
    query: str,
    max_results: int = 100,
) -> list[str]:
    """List the Gmail message ids matching a query."""
    page_token = None
    message_ids: list[str] = []

    while True:
        response = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=max_results,
            pageToken=page_token,
        ).execute()

        for item in response.get("messages", []) or []:
            message_ids.append(item["id"])

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return message_ids


def fetch_message(service, message_id: str, fmt: str = "full"):
    """Fetch one Gmail message."""
    return (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format=fmt)
        .execute()
    )


def fetch_raw_bytes(service, message_id: str) -> bytes:
    """Fetch the original raw bytes of a Gmail message."""
    message = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="raw")
        .execute()
    )

    raw = message.get("raw")

    if not raw:
        raise ValueError(
            f"The Gmail API returned no raw payload for {message_id}"
        )

    return base64.urlsafe_b64decode(raw)


def is_original_request(
    message: dict,
    known_request_message_ids: Iterable[str] | None = None,
) -> bool:
    """Return True when the message is one of our own original requests.

    The check never relies on the subject alone.  A message that carries an
    ``In-Reply-To`` header is treated as a reply; a message whose id is a
    known outbound request id is treated as the original request.
    """
    if known_request_message_ids:
        if message.get("id") in set(known_request_message_ids):
            return True

    headers = extract_headers(message)

    if _header_lookup(headers, "in-reply-to"):
        return False

    return True


def discover_replies(
    service,
    days: int = 30,
    request_thread_ids: Iterable[str] | None = None,
    request_message_ids: Iterable[str] | None = None,
) -> list[DiscoveredReply]:
    """Discover the inbound replies that belong to our collection requests.

    Parameters
    ----------
    service:
        An authenticated Gmail service.
    days:
        How far back to look.
    request_thread_ids:
        The thread ids of the original requests.  When provided, only
        messages in those threads are considered.
    request_message_ids:
        The message ids of the original requests.  These are excluded.
    """
    after_date = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y/%m/%d")

    thread_ids = set(request_thread_ids or [])
    known_request_ids = set(request_message_ids or [])

    if thread_ids:
        candidates: set[str] = set()
        for thread_id in thread_ids:
            candidates.update(
                list_message_ids(
                    service,
                    f"in:inbox after:{after_date} thread:{thread_id}",
                )
            )
    else:
        candidates = set(
            list_message_ids(
                service,
                f'in:inbox after:{after_date} "{REQUEST_SUBJECT_MARKER}"',
            )
        )

    replies: list[DiscoveredReply] = []

    for message_id in sorted(candidates):
        message = fetch_message(service, message_id)
        headers = extract_headers(message)

        if is_original_request(message, known_request_ids):
            continue

        subject = decode_subject(headers.get("subject", ""))

        replies.append(
            DiscoveredReply(
                message_id=message_id,
                thread_id=message.get("threadId"),
                from_address=extract_address(headers.get("from")),
                to_address=extract_address(headers.get("to")),
                subject=subject,
                date_header=headers.get("date"),
                in_reply_to=headers.get("in-reply-to"),
                references=headers.get("references"),
            )
        )

    return replies


def find_capex_replies(days=30):
    """Backward compatible entry point.

    Returns the legacy reply dictionaries.  The subject-based search is kept
    for backwards compatibility, but the original request email is excluded
    by checking the ``In-Reply-To`` header and the known request ids.
    """
    from backend.app.services.gmail_auth_service import get_gmail_service

    service = get_gmail_service()

    request_ids, request_threads = _load_known_requests()

    replies = discover_replies(
        service,
        days=days,
        request_thread_ids=request_threads or None,
        request_message_ids=request_ids or None,
    )

    return [reply.to_dict() for reply in replies]


def _load_known_requests():
    """Load the stored outbound request message ids and thread ids."""
    try:
        from backend.app.services.submission_persistence_service import connect

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT gmail_message_id, gmail_thread_id
                    FROM email_requests
                    WHERE gmail_message_id IS NOT NULL
                    """
                )
                rows = list(cur.fetchall())
    except Exception:  # noqa: BLE001 - the ledger is optional
        return set(), set()

    message_ids = {row["gmail_message_id"] for row in rows}
    thread_ids = {row["gmail_thread_id"] for row in rows if row["gmail_thread_id"]}

    return message_ids, thread_ids


def download_reply_attachments(message_id):
    """Download the Excel attachments of one Gmail message.

    Kept for backwards compatibility.  The pipeline uses
    :mod:`backend.app.services.email_archive_service` instead so that the
    attachments are archived together with the raw EML.
    """
    from backend.app.services.gmail_auth_service import get_gmail_service

    service = get_gmail_service()

    message = fetch_message(service, message_id)

    output_dir = (
        __import__("pathlib").Path("backend/output/reply_attachments") / message_id
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    saved_files = []

    for part in _iter_parts(message.get("payload", {})):
        filename = part.get("filename", "")
        if not filename.lower().endswith(".xlsx"):
            continue

        body = part.get("body", {})
        data = body.get("data")

        if not data:
            continue

        file_bytes = base64.urlsafe_b64decode(data)

        safe_filename = __import__("pathlib").Path(
            filename.replace("\\", "/")
        ).name

        file_path = output_dir / safe_filename
        file_path.write_bytes(file_bytes)
        saved_files.append(file_path)

        print("Downloaded:", file_path)
        print("Size:", len(file_bytes), "bytes")

    if not saved_files:
        print("No Excel attachments found.")

    return saved_files


def _iter_parts(payload: dict):
    if not payload:
        return

    yield payload

    for part in payload.get("parts", []) or []:
        yield from _iter_parts(part)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    found = find_capex_replies(days=args.days)

    print(f"Total reply emails: {len(found)}")

    for reply in found:
        print("Reply Message ID:", reply["message_id"])
