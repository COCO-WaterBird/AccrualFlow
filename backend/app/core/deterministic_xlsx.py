"""
Deterministic XLSX packaging.

The checksum of an exported workbook is used to prove that two exports
contain the same data:

* the Master Excel stores it in ``master_export_runs.file_sha256`` so that
  a re-export can be compared with the published one, and
* the correction workbook stores it so that a new attempt can be compared
  with the previous one.

An ``.xlsx`` file is a ZIP archive, and a ZIP archive carries a
modification timestamp in the header of every entry.  openpyxl therefore
produces a different file for identical data whenever the two exports land
in a different second.  Hashing the raw file bytes would make the
checksum a function of *when* the export ran instead of *what* it
contains.

This module repackages a workbook with fixed ZIP timestamps so that the
checksum is a pure function of the exported content.  The published file
is the canonical repackaging, so its checksum can be verified with a
plain ``sha256sum`` as well.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime
from pathlib import Path


# The timestamp written into the ZIP entry headers.
#
# The ZIP format stores the time with a resolution of two seconds and
# without a timezone.  A fixed value in the past is used so that the
# packaging never depends on the wall clock.
FIXED_ZIP_TIMESTAMP = (2000, 1, 1, 0, 0, 0)

# The timestamp written into the document properties of the workbook.
#
# openpyxl stamps the current time into ``docProps/core.xml``.  That is
# part of the file content and would break the checksum as well.
FIXED_DOCUMENT_TIMESTAMP = datetime(2000, 1, 1, 0, 0, 0)

# The document properties part of the workbook.
CORE_PROPERTIES_PART = "docProps/core.xml"

# The volatile timestamps inside the document properties.
_CORE_TIMESTAMP_PATTERN = re.compile(
    r"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:(?:created|modified)>)"
)


def _normalize_core_properties(payload: bytes) -> bytes:
    """Replace the volatile timestamps in the document properties.

    openpyxl writes the current time into the ``modified`` element when
    the workbook is saved, regardless of what was set on the workbook
    object.  The element is rewritten here so that the content of the
    file no longer depends on the wall clock.
    """
    timestamp = FIXED_DOCUMENT_TIMESTAMP.isoformat() + "Z"

    return _CORE_TIMESTAMP_PATTERN.sub(
        lambda match: match.group(1) + timestamp + match.group(2),
        payload.decode("utf-8"),
    ).encode("utf-8")


def canonicalize_xlsx(source: Path | str) -> bytes:
    """Return a byte-stable repackaging of an xlsx file.

    The entry order and the entry contents are preserved.  Only the
    volatile timestamps are replaced.
    """
    source = Path(source)

    with zipfile.ZipFile(source, "r") as archive:
        entries = [
            (info.filename, archive.read(info.filename))
            for info in archive.infolist()
        ]

    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            if name == CORE_PROPERTIES_PART:
                payload = _normalize_core_properties(payload)

            info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload)

    return buffer.getvalue()


def write_canonical_xlsx(source: Path | str) -> bytes:
    """Repackage the workbook in place and return the canonical bytes.

    The file at ``source`` is replaced by its canonical repackaging, so
    the checksum of the bytes on disk equals the checksum of the file.
    """
    source = Path(source)

    payload = canonicalize_xlsx(source)
    source.write_bytes(payload)

    return payload
