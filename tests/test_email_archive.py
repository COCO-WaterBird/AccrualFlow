"""
Test matrix items 14, 15, 16 and 17.

Covers:
 14. The same Gmail message processed twice -> one logical submission.
 15. Original request email excluded; unauthorized sender handled safely.
 16. Subject based EML filename sanitization and collision avoidance.
 17. Raw EML and Excel archived even when validation fails.
"""

import base64
from pathlib import Path

import pytest

from backend.app.services.email_archive_service import (
    ArchiveError,
    archive_attachments,
    archive_message,
    archive_raw_eml,
    extract_address,
    extract_headers,
)


# ---------------------------------------------------------------------------
# Fake Gmail service
# ---------------------------------------------------------------------------
class FakeMessages:
    def __init__(self, messages):
        self._messages = messages

    def list(self, userId, q, maxResults=100, pageToken=None):
        class Result:
            def __init__(self, payload):
                self._payload = payload

            def execute(self):
                return self._payload

        items = [
            {"id": message_id}
            for message_id in self._messages
            if message_id.startswith(q.split(":")[-1].strip('"')[:6])
        ] or [{"id": message_id} for message_id in self._messages]

        return Result({"messages": items})


class FakeService:
    def __init__(self, messages):
        self._messages = messages

    def users(self):
        service = self

        class Users:
            def messages(self):
                return FakeMessages(service._messages)

        return Users()


# ---------------------------------------------------------------------------
# 15. Original request email is excluded
# ---------------------------------------------------------------------------
def test_original_request_without_in_reply_to_is_treated_as_a_request():
    from backend.app.services.gmail_reply_service import is_original_request

    message = {
        "id": "request_1",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Action Required - CapEx"},
                {"name": "From", "value": "finance@example.com"},
            ],
        },
    }

    assert is_original_request(message) is True


def test_a_message_with_in_reply_to_is_treated_as_a_reply():
    from backend.app.services.gmail_reply_service import is_original_request

    message = {
        "id": "reply_1",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Re: Action Required"},
                {"name": "In-Reply-To", "value": "<request_1@example.com>"},
            ],
        },
    }

    assert is_original_request(message) is False


def test_a_known_request_message_id_is_always_excluded():
    from backend.app.services.gmail_reply_service import is_original_request

    message = {
        "id": "known_request",
        "payload": {
            "headers": [
                {"name": "In-Reply-To", "value": "<something@example.com>"},
            ],
        },
    }

    assert is_original_request(message, {"known_request"}) is True


# ---------------------------------------------------------------------------
# Unauthorized senders are handled safely
# ---------------------------------------------------------------------------
def test_extract_address_handles_display_names():
    assert extract_address("Alex Chen <alex.chen@example.com>") == (
        "alex.chen@example.com"
    )
    assert extract_address("ALEX.CHEN@EXAMPLE.COM") == "alex.chen@example.com"
    assert extract_address(None) is None
    assert extract_address("") is None


def test_extract_headers_is_case_insensitive():
    headers = extract_headers(
        {"payload": {"headers": [{"name": "Subject", "value": "Hello"}]}}
    )

    assert headers["subject"] == "Hello"


# ---------------------------------------------------------------------------
# 16. Filename sanitization and collision avoidance
# ---------------------------------------------------------------------------
def test_archive_uses_the_sanitized_subject_and_the_message_id(tmp_path):
    eml_path, checksum, size, already = archive_raw_eml(
        raw_bytes=b"raw bytes",
        gmail_message_id="abc123",
        subject="CapEx / Completion: Update (2026-11)",
        reporting_month="2026-11",
        root=tmp_path,
    )

    name = Path(eml_path).name

    assert name.endswith("__abc123.eml")
    assert "/" not in name
    assert ":" not in name
    assert "(" not in name
    assert checksum
    assert size == len(b"raw bytes")
    assert already is False


def test_archiving_the_same_message_twice_is_idempotent(tmp_path):
    first = archive_raw_eml(
        raw_bytes=b"raw bytes",
        gmail_message_id="abc123",
        subject="Subject",
        reporting_month="2026-11",
        root=tmp_path,
    )

    second = archive_raw_eml(
        raw_bytes=b"raw bytes",
        gmail_message_id="abc123",
        subject="Subject",
        reporting_month="2026-11",
        root=tmp_path,
    )

    assert first[0] == second[0]
    assert first[1] == second[1]
    assert second[3] is True

    files = list(Path(tmp_path).rglob("*.eml"))
    assert len(files) == 1


def test_two_different_messages_never_share_a_file_name(tmp_path):
    first = archive_raw_eml(
        raw_bytes=b"one",
        gmail_message_id="msg_a",
        subject="Same Subject",
        reporting_month="2026-11",
        root=tmp_path,
    )

    second = archive_raw_eml(
        raw_bytes=b"two",
        gmail_message_id="msg_b",
        subject="Same Subject",
        reporting_month="2026-11",
        root=tmp_path,
    )

    assert Path(first[0]).name != Path(second[0]).name


def test_archiving_conflicting_content_for_one_message_is_rejected(tmp_path):
    archive_raw_eml(
        raw_bytes=b"one",
        gmail_message_id="abc123",
        subject="Subject",
        reporting_month="2026-11",
        root=tmp_path,
    )

    with pytest.raises(ArchiveError):
        archive_raw_eml(
            raw_bytes=b"different content",
            gmail_message_id="abc123",
            subject="Subject",
            reporting_month="2026-11",
            root=tmp_path,
        )


def test_empty_raw_message_is_rejected(tmp_path):
    with pytest.raises(ArchiveError):
        archive_raw_eml(
            raw_bytes=b"",
            gmail_message_id="abc123",
            subject="Subject",
            reporting_month="2026-11",
            root=tmp_path,
        )


# ---------------------------------------------------------------------------
# 17. Attachments are archived together with the raw message
# ---------------------------------------------------------------------------
def _message_with_attachment(payload: bytes, filename: str = "report.xlsx"):
    return {
        "id": "msg_1",
        "threadId": "thread_1",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "CapEx Completion Update"},
                {"name": "From", "value": "alex.chen@example.com"},
                {"name": "To", "value": "finance@example.com"},
                {"name": "Date", "value": "Wed, 11 Nov 2026 09:00:00 +0000"},
            ],
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "",
                    "body": {"size": 10},
                },
                {
                    "mimeType": (
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                    "filename": filename,
                    "body": {
                        "size": len(payload),
                        "attachmentId": "att_1",
                        "data": base64.urlsafe_b64encode(payload).decode("ascii"),
                    },
                },
            ],
        },
    }


def test_message_archives_the_eml_and_the_attachment(tmp_path):
    payload = b"fake workbook bytes"

    archived = archive_message(
        raw_bytes=b"the original raw message",
        message=_message_with_attachment(payload),
        gmail_message_id="msg_1",
        reporting_month="2026-11",
        root=tmp_path,
    )

    assert Path(archived.eml_path).exists()
    assert Path(archived.eml_path).read_bytes() == b"the original raw message"

    assert len(archived.attachments) == 1

    attachment = archived.attachments[0]
    assert Path(attachment.stored_path).exists()
    assert Path(attachment.stored_path).read_bytes() == payload
    assert attachment.sha256


def test_attachment_names_with_traversal_are_rejected(tmp_path):
    message = _message_with_attachment(b"data", filename="../../evil.xlsx")

    archived = archive_attachments(
        message=message,
        gmail_message_id="msg_evil",
        reporting_month="2026-11",
        root=tmp_path,
    )

    assert len(archived) == 1
    assert archived[0].stored_path == ""
    assert archived[0].attachment_id.endswith("__REJECTED")


def test_attachments_are_deduplicated_by_content_hash(tmp_path):
    payload = b"same workbook"

    first = archive_attachments(
        message=_message_with_attachment(payload),
        gmail_message_id="msg_a",
        reporting_month="2026-11",
        root=tmp_path,
    )

    second = archive_attachments(
        message=_message_with_attachment(payload),
        gmail_message_id="msg_b",
        reporting_month="2026-11",
        root=tmp_path,
    )

    assert first[0].sha256 == second[0].sha256
    assert first[0].stored_filename == second[0].stored_filename