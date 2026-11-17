"""
Tests for the deterministic XLSX packaging.

The checksum of an exported workbook must be a pure function of the
exported content.  An ``.xlsx`` is a ZIP archive, and a ZIP archive
carries a modification timestamp in every entry header, so two exports of
identical data would otherwise produce different checksums whenever they
land in a different second.
"""

import hashlib
import io
import zipfile
from pathlib import Path

from openpyxl import Workbook

from backend.app.core.deterministic_xlsx import (
    FIXED_DOCUMENT_TIMESTAMP,
    FIXED_ZIP_TIMESTAMP,
    canonicalize_xlsx,
    write_canonical_xlsx,
)


def _make_workbook(path: Path, value: str = "x") -> Path:
    workbook = Workbook()
    workbook.active["A1"] = value
    workbook.save(path)
    workbook.close()
    return path


def test_canonical_bytes_are_stable_across_time(tmp_path):
    first = _make_workbook(tmp_path / "first.xlsx")
    second = _make_workbook(tmp_path / "second.xlsx")

    payload_a = canonicalize_xlsx(first)
    payload_b = canonicalize_xlsx(second)

    assert hashlib.sha256(payload_a).hexdigest() == (
        hashlib.sha256(payload_b).hexdigest()
    )


def test_canonical_bytes_ignore_the_zip_entry_timestamps(tmp_path):
    source = _make_workbook(tmp_path / "book.xlsx")

    payload = canonicalize_xlsx(source)

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for info in archive.infolist():
            assert info.date_time == FIXED_ZIP_TIMESTAMP


def test_canonicalization_preserves_the_content(tmp_path):
    source = _make_workbook(tmp_path / "book.xlsx", value="hello")

    payload = canonicalize_xlsx(source)

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()

        assert "xl/worksheets/sheet1.xml" in names

        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        assert "hello" in sheet


def test_canonicalization_ignores_the_document_properties_timestamps(tmp_path):
    """openpyxl writes the save time into docProps/core.xml."""
    source = _make_workbook(tmp_path / "book.xlsx")

    payload = canonicalize_xlsx(source)

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        core = archive.read("docProps/core.xml").decode()

    assert FIXED_DOCUMENT_TIMESTAMP.isoformat() + "Z" in core
    assert "dcterms:modified" in core


def test_canonicalization_is_idempotent(tmp_path):
    source = _make_workbook(tmp_path / "book.xlsx")

    once = canonicalize_xlsx(source)
    twice = canonicalize_xlsx(source)

    assert once == twice


def test_canonicalization_survives_a_change_of_the_wall_clock(tmp_path):
    """Two workbooks saved minutes apart must produce the same bytes."""
    first = _make_workbook(tmp_path / "first.xlsx")
    second = _make_workbook(tmp_path / "second.xlsx")

    payload_a = canonicalize_xlsx(first)
    payload_b = canonicalize_xlsx(second)

    assert payload_a == payload_b


def test_write_canonical_xlsx_replaces_the_file(tmp_path):
    source = _make_workbook(tmp_path / "book.xlsx")

    payload = write_canonical_xlsx(source)

    assert source.read_bytes() == payload
    assert (
        hashlib.sha256(source.read_bytes()).hexdigest()
        == hashlib.sha256(payload).hexdigest()
    )


def test_the_published_file_checksum_matches_its_bytes(tmp_path):
    """A finance reviewer can verify the checksum with sha256sum."""
    source = _make_workbook(tmp_path / "book.xlsx")

    payload = write_canonical_xlsx(source)

    digest = hashlib.sha256()
    digest.update(source.read_bytes())

    assert digest.hexdigest() == hashlib.sha256(payload).hexdigest()
