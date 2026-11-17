"""
Raw email and attachment archiving.

Every relevant inbound message is archived **before** validation, including
messages that later fail validation.  The archived bytes are the original
``raw`` payload returned by the Gmail API - never a reconstructed
header/body rendering - so the archive is genuine evidence.

Layout::

    <EMAIL_ARCHIVE_DIR>/<YYYY-MM>/<gmail_message_id>/
        <sanitized-subject>__<gmail-message-id>.eml
    <ATTACHMENT_DIR>/<YYYY-MM>/<gmail_message_id>/
        <sha256-prefix>__<original_name>

Naming rules
------------
* The subject is sanitized to a safe, collision-free token.
* The Gmail message id is always appended, so two different messages can
  never share a file name.
* The path traversal guard rejects any attachment name that tries to escape
  the target directory.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from backend.app.core.config import (
    ATTACHMENT_DIR,
    DATA_DIR,
    EMAIL_ARCHIVE_DIR,
    ensure_runtime_dirs,
)


# Maximum length of the sanitized subject token.
MAX_SUBJECT_TOKEN_LENGTH = 80

# Characters allowed in a sanitized subject token.
_UNSAFE_SUBJECT_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# Extensions that are accepted as a completion workbook.
SUPPORTED_ATTACHMENT_EXTENSIONS = (".xlsx",)

# Maximum size of a single attachment (10 MiB).
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


class ArchiveError(Exception):
    """Raised when an artefact cannot be archived safely."""


@dataclass
class ArchivedAttachment:
    """Metadata of one archived attachment."""

    attachment_id: str
    original_filename: str
    stored_filename: str
    stored_path: str
    sha256: str
    size_bytes: int
    content_type: str | None = None

    def to_dict(self) -> dict:
        return {
            "attachment_id": self.attachment_id,
            "original_filename": self.original_filename,
            "stored_filename": self.stored_filename,
            "stored_path": self.stored_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
        }


@dataclass
class ArchivedEmail:
    """Metadata of one archived inbound message."""

    gmail_message_id: str
    gmail_thread_id: str | None
    subject: str
    from_address: str | None
    to_address: str | None
    date_header: str | None
    reporting_month: str
    eml_path: str
    eml_sha256: str
    eml_size_bytes: int
    attachments: list[ArchivedAttachment] = field(default_factory=list)
    already_archived: bool = False

    def to_dict(self) -> dict:
        return {
            "gmail_message_id": self.gmail_message_id,
            "gmail_thread_id": self.gmail_thread_id,
            "subject": self.subject,
            "from_address": self.from_address,
            "to_address": self.to_address,
            "date_header": self.date_header,
            "reporting_month": self.reporting_month,
            "eml_path": self.eml_path,
            "eml_sha256": self.eml_sha256,
            "eml_size_bytes": self.eml_size_bytes,
            "attachments": [a.to_dict() for a in self.attachments],
            "already_archived": self.already_archived,
        }


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------


def sanitize_subject(subject: str | None) -> str:
    """Turn an arbitrary subject into a safe file name token.

    The result never contains a path separator, never starts with a dot and
    never exceeds :data:`MAX_SUBJECT_TOKEN_LENGTH` characters.
    """
    if not subject:
        return "no_subject"

    token = _UNSAFE_SUBJECT_CHARS.sub("_", subject.strip())
    token = token.strip("._-")

    if not token:
        return "no_subject"

    if len(token) > MAX_SUBJECT_TOKEN_LENGTH:
        token = token[:MAX_SUBJECT_TOKEN_LENGTH].strip("._-")

    return token or "no_subject"


def build_eml_filename(subject: str | None, gmail_message_id: str) -> str:
    """Build the canonical EML file name.

    ``<sanitized-subject>__<gmail-message-id>.eml``
    """
    safe_message_id = _UNSAFE_SUBJECT_CHARS.sub("_", gmail_message_id)
    safe_message_id = safe_message_id.strip("._-") or "unknown"

    return f"{sanitize_subject(subject)}__{safe_message_id}.eml"


def safe_attachment_name(filename: str | None) -> str:
    """Return a safe base file name for an attachment.

    Any name that carries a ``..`` path segment is rejected outright rather
    than being silently flattened, because a flattened name would hide the
    attempt to escape the target directory.
    """
    if not filename:
        raise ArchiveError("Attachment has no filename")

    normalized = filename.replace("\\", "/")

    if ".." in normalized.split("/"):
        raise ArchiveError(
            f"Rejected attachment name with path traversal: {filename!r}"
        )

    base = Path(normalized).name

    if not base or base in (".", ".."):
        raise ArchiveError(f"Rejected attachment name: {filename!r}")

    base = _UNSAFE_SUBJECT_CHARS.sub("_", base).strip("._-")

    if not base:
        raise ArchiveError(f"Rejected attachment name: {filename!r}")

    if len(base) > 120:
        suffix = Path(base).suffix
        stem = base[: 120 - len(suffix)] if len(suffix) < 20 else base[:120]
        base = stem

    return base


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------


def message_archive_dir(
    reporting_month: str,
    gmail_message_id: str,
    root: Path | None = None,
) -> Path:
    """Return the directory that holds one message's evidence."""
    base = root or EMAIL_ARCHIVE_DIR
    safe_month = re.sub(r"[^0-9-]", "", reporting_month) or "unknown"
    safe_id = _UNSAFE_SUBJECT_CHARS.sub("_", gmail_message_id) or "unknown"
    return base / safe_month / safe_id


def attachment_dir(
    reporting_month: str,
    gmail_message_id: str,
    root: Path | None = None,
) -> Path:
    base = root or ATTACHMENT_DIR
    safe_month = re.sub(r"[^0-9-]", "", reporting_month) or "unknown"
    safe_id = _UNSAFE_SUBJECT_CHARS.sub("_", gmail_message_id) or "unknown"
    return base / safe_month / safe_id


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write bytes to ``path`` atomically and never overwrite a different file."""
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        existing = path.read_bytes()
        if existing == payload:
            return
        raise ArchiveError(
            f"Refusing to overwrite an existing archive file: {path}"
        )

    handle, temp_name = tempfile.mkstemp(
        prefix=".archive_", suffix=".tmp", dir=str(path.parent)
    )
    os.close(handle)
    temp_path = Path(temp_name)

    try:
        temp_path.write_bytes(payload)
        with open(temp_path, "rb") as src:
            with open(path, "xb") as dst:
                shutil.copyfileobj(src, dst)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        temp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------


def extract_headers(message: dict) -> dict[str, str]:
    """Return a lowercase header map for a Gmail message payload."""
    headers: dict[str, str] = {}

    for header in message.get("payload", {}).get("headers", []) or []:
        name = header.get("name", "").lower()
        value = header.get("value", "")
        if name and name not in headers:
            headers[name] = value

    return headers


def extract_address(raw: str | None) -> str | None:
    """Extract the bare e-mail address from a From/To header value."""
    if not raw:
        return None

    text = raw.strip()

    if "<" in text and ">" in text:
        start = text.index("<") + 1
        end = text.index(">", start)
        text = text[start:end]

    return text.strip().lower() or None


def iter_payload_parts(payload: dict) -> Iterable[dict]:
    """Yield every part of a Gmail payload tree, depth first."""
    if not payload:
        return

    yield payload

    for part in payload.get("parts", []) or []:
        yield from iter_payload_parts(part)


def decode_attachment_data(part: dict) -> bytes | None:
    """Return the decoded bytes of a Gmail attachment part."""
    body = part.get("body", {}) or {}
    data = body.get("data")

    if not data:
        return None

    try:
        return base64.urlsafe_b64decode(data)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Archiving
# ---------------------------------------------------------------------------


def archive_raw_eml(
    raw_bytes: bytes,
    gmail_message_id: str,
    subject: str,
    reporting_month: str,
    gmail_thread_id: str | None = None,
    from_address: str | None = None,
    to_address: str | None = None,
    date_header: str | None = None,
    root: Path | None = None,
) -> tuple[str, str, int, bool]:
    """Archive the original raw message bytes.

    Returns ``(path, sha256, size_bytes, already_archived)``.

    The operation is idempotent: archiving the same message twice keeps the
    first file untouched and reports ``already_archived=True``.
    """
    if not raw_bytes:
        raise ArchiveError("Refusing to archive an empty raw message")

    if not gmail_message_id:
        raise ArchiveError("A Gmail message id is required for archiving")

    ensure_runtime_dirs()

    directory = message_archive_dir(reporting_month, gmail_message_id, root)
    directory.mkdir(parents=True, exist_ok=True)

    filename = build_eml_filename(subject, gmail_message_id)
    target = directory / filename

    checksum = sha256_bytes(raw_bytes)

    if target.exists():
        existing_hash = sha256_file(target)
        if existing_hash != checksum:
            raise ArchiveError(
                f"Archive checksum conflict for message {gmail_message_id}"
            )
        return str(target), checksum, len(raw_bytes), True

    _atomic_write(target, raw_bytes)

    return str(target), checksum, len(raw_bytes), False


def archive_attachments(
    message: dict,
    gmail_message_id: str,
    reporting_month: str,
    root: Path | None = None,
) -> list[ArchivedAttachment]:
    """Archive every attachment of a Gmail message.

    Unsupported file types and oversized files are recorded with an empty
    ``stored_path`` and a descriptive ``attachment_id`` prefix so that the
    exception stays visible in the audit trail instead of being dropped.
    """
    ensure_runtime_dirs()

    directory = attachment_dir(reporting_month, gmail_message_id, root)
    directory.mkdir(parents=True, exist_ok=True)

    archived: list[ArchivedAttachment] = []

    for index, part in enumerate(iter_payload_parts(message.get("payload", {}))):
        filename = part.get("filename", "")
        if not filename:
            continue

        part_id = (
            part.get("body", {}).get("attachmentId")
            or f"part-{index}"
        )

        try:
            safe_name = safe_attachment_name(filename)
        except ArchiveError:
            archived.append(
                ArchivedAttachment(
                    attachment_id=f"{part_id}__REJECTED",
                    original_filename=filename,
                    stored_filename="",
                    stored_path="",
                    sha256="",
                    size_bytes=0,
                    content_type=part.get("mimeType"),
                )
            )
            continue

        payload = decode_attachment_data(part)

        if payload is None:
            archived.append(
                ArchivedAttachment(
                    attachment_id=f"{part_id}__UNREADABLE",
                    original_filename=safe_name,
                    stored_filename="",
                    stored_path="",
                    sha256="",
                    size_bytes=0,
                    content_type=part.get("mimeType"),
                )
            )
            continue

        checksum = sha256_bytes(payload)
        stored_name = f"{checksum[:16]}__{safe_name}"
        target = directory / stored_name

        if target.exists():
            if sha256_file(target) != checksum:
                raise ArchiveError(f"Attachment checksum conflict: {target}")
        else:
            if len(payload) > MAX_ATTACHMENT_BYTES:
                archived.append(
                    ArchivedAttachment(
                        attachment_id=f"{part_id}__TOO_LARGE",
                        original_filename=safe_name,
                        stored_filename="",
                        stored_path="",
                        sha256=checksum,
                        size_bytes=len(payload),
                        content_type=part.get("mimeType"),
                    )
                )
                continue

            _atomic_write(target, payload)

        archived.append(
            ArchivedAttachment(
                attachment_id=str(part_id),
                original_filename=safe_name,
                stored_filename=stored_name,
                stored_path=str(target),
                sha256=checksum,
                size_bytes=len(payload),
                content_type=part.get("mimeType"),
            )
        )

    return archived


def archive_message(
    raw_bytes: bytes,
    message: dict,
    gmail_message_id: str,
    reporting_month: str,
    subject: str | None = None,
    root: Path | None = None,
) -> ArchivedEmail:
    """Archive the raw EML and every attachment of one inbound message."""
    headers = extract_headers(message)

    resolved_subject = subject if subject is not None else headers.get("subject", "")

    eml_path, checksum, size, already = archive_raw_eml(
        raw_bytes=raw_bytes,
        gmail_message_id=gmail_message_id,
        subject=resolved_subject,
        reporting_month=reporting_month,
        gmail_thread_id=message.get("threadId"),
        from_address=extract_address(headers.get("from")),
        to_address=extract_address(headers.get("to")),
        date_header=headers.get("date"),
        root=root,
    )

    attachments = archive_attachments(
        message=message,
        gmail_message_id=gmail_message_id,
        reporting_month=reporting_month,
        root=root,
    )

    return ArchivedEmail(
        gmail_message_id=gmail_message_id,
        gmail_thread_id=message.get("threadId"),
        subject=resolved_subject,
        from_address=extract_address(headers.get("from")),
        to_address=extract_address(headers.get("to")),
        date_header=headers.get("date"),
        reporting_month=reporting_month,
        eml_path=eml_path,
        eml_sha256=checksum,
        eml_size_bytes=size,
        attachments=attachments,
        already_archived=already,
    )


def resolve_data_dir() -> Path:
    """Return the configured writable data root."""
    ensure_runtime_dirs()
    return DATA_DIR


if __name__ == "__main__":
    print("Data directory:", resolve_data_dir())
    print("Email archive directory:", EMAIL_ARCHIVE_DIR)
    print("Attachment directory:", ATTACHMENT_DIR)