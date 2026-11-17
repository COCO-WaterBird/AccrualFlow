"""
Test matrix items 1-7 and 16: deterministic percentage interpretation.

Covers:
  1. Percent-formatted raw 0.45 -> 45% accepted.
  2. Text "45%" -> 45% accepted.
  3. Unformatted integer 45 -> suggested 45%, confirmation required.
  4. Unformatted decimal 0.45 -> suggested 45%, confirmation required.
  5. Entire column integers / decimals / mixed formats.
  6. Ambiguous 0 and 1, negative, >100%, non-numeric, blank, formula, error.
  7. Numeric range validation.
 16. Subject based EML filename sanitization and collision avoidance.
"""

from decimal import Decimal

import pytest

from backend.app.services.percentage_validation_service import (
    DetectionKind,
    ExceptionCode,
    FormatStatus,
    build_column_profile,
    interpret_percentage,
    is_percent_number_format,
    parse_explicit_percent_text,
    validate_range,
)
from backend.app.services.email_archive_service import (
    build_eml_filename,
    safe_attachment_name,
    sanitize_subject,
)
from backend.app.services.email_archive_service import ArchiveError


# ---------------------------------------------------------------------------
# 1. Percent formatted numeric
# ---------------------------------------------------------------------------
def test_percent_formatted_numeric_is_accepted():
    result = interpret_percentage(0.45, number_format="0%")

    assert result.status is FormatStatus.ACCEPTED
    assert result.normalized_pct == Decimal("0.45")
    assert result.detection is DetectionKind.PERCENT_FORMATTED
    assert result.confidence == "HIGH"
    assert result.exception_codes == []


def test_percent_formatted_numeric_with_decimals_format():
    result = interpret_percentage(Decimal("0.4567"), number_format="0.00%")

    assert result.status is FormatStatus.ACCEPTED
    assert result.normalized_pct == Decimal("0.4567")


def test_percent_formatted_value_above_one_is_out_of_range():
    result = interpret_percentage(1.2, number_format="0%")

    assert result.status is FormatStatus.INVALID
    assert ExceptionCode.OUT_OF_RANGE.value in result.exception_codes


# ---------------------------------------------------------------------------
# 2. Explicit percent text
# ---------------------------------------------------------------------------
def test_explicit_percent_text_is_accepted():
    result = interpret_percentage("45%")

    assert result.status is FormatStatus.ACCEPTED
    assert result.normalized_pct == Decimal("0.45")
    assert result.detection is DetectionKind.EXPLICIT_PERCENT_TEXT


def test_explicit_percent_text_with_spaces():
    result = interpret_percentage(" 45 % ")

    assert result.status is FormatStatus.ACCEPTED
    assert result.normalized_pct == Decimal("0.45")


def test_explicit_percent_text_above_range():
    result = interpret_percentage("120%")

    assert result.status is FormatStatus.INVALID
    assert ExceptionCode.OUT_OF_RANGE.value in result.exception_codes


def test_parse_explicit_percent_text_rejects_non_percent():
    assert parse_explicit_percent_text("45") is None
    assert parse_explicit_percent_text("forty%") is None
    assert parse_explicit_percent_text(None) is None


# ---------------------------------------------------------------------------
# 3. Unformatted integer
# ---------------------------------------------------------------------------
def test_unformatted_integer_requires_confirmation():
    result = interpret_percentage(45, number_format="General")

    assert result.status is FormatStatus.FORMAT_CONFIRMATION_REQUIRED
    assert result.suggested_pct == Decimal("0.45")
    assert result.normalized_pct is None
    assert ExceptionCode.AMBIGUOUS_INTEGER.value in result.exception_codes
    assert result.effective_pct is None


def test_unformatted_integer_with_all_integer_column_raises_confidence():
    profile = build_column_profile(
        [(40, "General"), (45, "General"), (60, "General")]
    )

    result = interpret_percentage(45, "General", column_profile=profile)

    assert result.status is FormatStatus.FORMAT_CONFIRMATION_REQUIRED
    assert result.confidence == "MEDIUM"


def test_unformatted_integer_with_mixed_column_is_low_confidence():
    profile = build_column_profile(
        [(0.4, "0%"), (45, "General"), ("abc", "General")]
    )

    result = interpret_percentage(45, "General", column_profile=profile)

    assert result.status is FormatStatus.FORMAT_CONFIRMATION_REQUIRED
    assert result.confidence == "LOW"
    assert ExceptionCode.AMBIGUOUS_MIXED_FORMATS.value in result.exception_codes


def test_unformatted_integer_above_100_is_invalid():
    result = interpret_percentage(120, "General")

    assert result.status is FormatStatus.INVALID
    assert ExceptionCode.OUT_OF_RANGE.value in result.exception_codes


def test_unformatted_negative_integer_is_invalid():
    result = interpret_percentage(-5, "General")

    assert result.status is FormatStatus.INVALID
    assert ExceptionCode.NEGATIVE_VALUE.value in result.exception_codes


# ---------------------------------------------------------------------------
# 4. Unformatted decimal
# ---------------------------------------------------------------------------
def test_unformatted_decimal_requires_confirmation():
    result = interpret_percentage(0.45, number_format="General")

    assert result.status is FormatStatus.FORMAT_CONFIRMATION_REQUIRED
    assert result.suggested_pct == Decimal("0.45")
    assert result.normalized_pct is None
    assert ExceptionCode.AMBIGUOUS_DECIMAL.value in result.exception_codes


def test_unformatted_decimal_with_all_decimal_column_raises_confidence():
    profile = build_column_profile(
        [(0.4, "General"), (0.45, "General"), (0.6, "General")]
    )

    result = interpret_percentage(0.45, "General", column_profile=profile)

    assert result.status is FormatStatus.FORMAT_CONFIRMATION_REQUIRED
    assert result.confidence == "MEDIUM"


def test_unformatted_decimal_above_one_is_invalid():
    result = interpret_percentage(1.5, "General")

    assert result.status is FormatStatus.INVALID
    assert ExceptionCode.OUT_OF_RANGE.value in result.exception_codes


# ---------------------------------------------------------------------------
# 5. Column wide diagnostics
# ---------------------------------------------------------------------------
def test_column_profile_all_integer():
    profile = build_column_profile(
        [(10, "General"), (20, "General"), (30, "General")]
    )

    assert profile.is_all_integer is True
    assert profile.is_all_decimal is False
    assert profile.is_mixed is False
    assert profile.integers_all_in_0_100 is True


def test_column_profile_all_decimal():
    profile = build_column_profile(
        [(0.1, "General"), (0.2, "General")]
    )

    assert profile.is_all_decimal is True
    assert profile.decimals_all_in_0_1 is True


def test_column_profile_all_explicit_percent():
    profile = build_column_profile(
        [(0.1, "0%"), (0.2, "0.0%")]
    )

    assert profile.is_all_explicit_percent is True
    assert profile.is_mixed is False


def test_column_profile_mixed():
    profile = build_column_profile(
        [(0.1, "0%"), (20, "General"), ("45%", "General")]
    )

    assert profile.is_mixed is True
    assert profile.is_all_integer is False
    assert profile.is_all_decimal is False


def test_column_profile_ignores_blanks():
    profile = build_column_profile(
        [(None, "0%"), ("", "0%"), (0.5, "0%")]
    )

    assert profile.total_cells == 3
    assert profile.nonblank_cells == 1
    assert profile.is_all_explicit_percent is True


# ---------------------------------------------------------------------------
# 6. Special values
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [0, 1])
def test_zero_and_one_are_ambiguous(value):
    result = interpret_percentage(value, "General")

    assert result.status is FormatStatus.FORMAT_CONFIRMATION_REQUIRED
    assert ExceptionCode.AMBIGUOUS_ZERO_OR_ONE.value in result.exception_codes
    assert result.suggested_pct == Decimal(value)


def test_zero_and_one_with_percent_column_raises_confidence():
    profile = build_column_profile([(0.4, "0%"), (0.5, "0%")])

    result = interpret_percentage(0, "General", column_profile=profile)

    assert result.status is FormatStatus.FORMAT_CONFIRMATION_REQUIRED
    assert result.confidence == "MEDIUM"


def test_blank_is_missing():
    for value in (None, "", "   "):
        result = interpret_percentage(value, "0%")

        assert result.status is FormatStatus.MISSING
        assert ExceptionCode.MISSING_VALUE.value in result.exception_codes


def test_non_numeric_text_is_invalid():
    result = interpret_percentage("forty")

    assert result.status is FormatStatus.INVALID
    assert ExceptionCode.NON_NUMERIC_VALUE.value in result.exception_codes


def test_excel_error_is_invalid():
    result = interpret_percentage("#VALUE!")

    assert result.status is FormatStatus.INVALID
    assert ExceptionCode.EXCEL_ERROR.value in result.exception_codes


def test_formula_is_not_executed():
    result = interpret_percentage("=A1/B1")

    assert result.status is FormatStatus.INVALID
    assert ExceptionCode.FORMULA_CELL.value in result.exception_codes
    assert result.normalized_pct is None
    assert result.suggested_pct is None


def test_boolean_is_invalid():
    result = interpret_percentage(True)

    assert result.status is FormatStatus.INVALID
    assert ExceptionCode.NON_NUMERIC_VALUE.value in result.exception_codes


# ---------------------------------------------------------------------------
# 7. Range validation
# ---------------------------------------------------------------------------
def test_validate_range_accepts_canonical_fraction():
    result = interpret_percentage(0.45, "0%")

    ok, codes = validate_range(result)

    assert ok is True
    assert codes == []


def test_validate_range_rejects_out_of_range():
    result = interpret_percentage(1.5, "General")

    ok, codes = validate_range(result)

    assert ok is False
    assert ExceptionCode.OUT_OF_RANGE.value in codes


# ---------------------------------------------------------------------------
# Number format detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "number_format,expected",
    [
        ("0%", True),
        ("0.00%", True),
        ("0.0%;(0.0%)", True),
        ('0"%"', False),
        ("General", False),
        ("0.00", False),
        (None, False),
        ("", False),
    ],
)
def test_is_percent_number_format(number_format, expected):
    assert is_percent_number_format(number_format) is expected


# ---------------------------------------------------------------------------
# 16. Filename sanitization and collision avoidance
# ---------------------------------------------------------------------------
def test_sanitize_subject_removes_unsafe_characters():
    token = sanitize_subject("CapEx / Completion: 2026-11 (Update)")

    assert "/" not in token
    assert ":" not in token
    assert "(" not in token
    assert " " not in token


def test_sanitize_subject_is_length_bounded():
    token = sanitize_subject("A" * 500)

    assert len(token) <= 80


def test_sanitize_subject_handles_empty():
    assert sanitize_subject(None) == "no_subject"
    assert sanitize_subject("") == "no_subject"


def test_build_eml_filename_includes_message_id():
    name = build_eml_filename("CapEx Update", "1a1c6bf760e71881")

    assert name == "CapEx_Update__1a1c6bf760e71881.eml"


def test_build_eml_filename_avoids_collisions():
    first = build_eml_filename("Same Subject", "message_a")
    second = build_eml_filename("Same Subject", "message_b")

    assert first != second


def test_safe_attachment_name_strips_directories():
    assert safe_attachment_name("dir/sub/report.xlsx") == "report.xlsx"
    assert safe_attachment_name("report.xlsx") == "report.xlsx"


def test_safe_attachment_name_rejects_traversal():
    with pytest.raises(ArchiveError):
        safe_attachment_name("../secret.xlsx")

    with pytest.raises(ArchiveError):
        safe_attachment_name("..\\secret.xlsx")

    with pytest.raises(ArchiveError):
        safe_attachment_name("")