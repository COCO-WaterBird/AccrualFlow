"""
Deterministic percentage interpretation and validation.

This module is intentionally free of any LLM or heuristic "guess" logic.
Every decision is derived from the Excel cell value, the Excel number format
and the column-wide context, and every outcome is reported with an explicit
status plus supporting evidence.

Canonical representation
-----------------------
A completion percentage is represented as a ``decimal.Decimal`` fraction in
the closed interval [0, 1].  ``Decimal("0.45")`` means 45%.

The original cell value, the original number format and the original data
type are always preserved alongside the canonical value so that nothing is
lost during normalization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Sequence


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------


class FormatStatus(str, Enum):
    """Result of interpreting one submitted percentage cell."""

    ACCEPTED = "ACCEPTED"
    """Value is unambiguous and inside the valid range."""

    SUGGESTED = "SUGGESTED"
    """A canonical value could be proposed but requires human confirmation."""

    FORMAT_CONFIRMATION_REQUIRED = "FORMAT_CONFIRMATION_REQUIRED"
    """The representation is ambiguous and cannot be resolved automatically."""

    INVALID = "INVALID"
    """The value cannot be interpreted as a percentage at all."""

    MISSING = "MISSING"
    """The cell is blank."""


class ExceptionCode(str, Enum):
    """Stable machine-readable exception codes."""

    MISSING_VALUE = "MISSING_VALUE"
    MISSING_LINE = "MISSING_LINE"
    INVALID_VALUE = "INVALID_VALUE"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    NEGATIVE_VALUE = "NEGATIVE_VALUE"
    NON_NUMERIC_VALUE = "NON_NUMERIC_VALUE"
    FORMULA_CELL = "FORMULA_CELL"
    EXCEL_ERROR = "EXCEL_ERROR"
    AMBIGUOUS_INTEGER = "AMBIGUOUS_INTEGER"
    AMBIGUOUS_DECIMAL = "AMBIGUOUS_DECIMAL"
    AMBIGUOUS_ZERO_OR_ONE = "AMBIGUOUS_ZERO_OR_ONE"
    AMBIGUOUS_MIXED_FORMATS = "AMBIGUOUS_MIXED_FORMATS"
    UNKNOWN_PO_LINE = "UNKNOWN_PO_LINE"
    DUPLICATE_PO_LINE = "DUPLICATE_PO_LINE"
    DEPARTMENT_MISMATCH = "DEPARTMENT_MISMATCH"
    PERIOD_MISMATCH = "PERIOD_MISMATCH"
    BASELINE_MISSING = "BASELINE_MISSING"
    DECREASE_REASON_MISSING = "DECREASE_REASON_MISSING"
    DECREASE_REASON_PROVIDED = "DECREASE_REASON_PROVIDED"
    GR_RATIO_EXCEEDED = "GR_RATIO_EXCEEDED"
    CORRUPT_ATTACHMENT = "CORRUPT_ATTACHMENT"
    EMPTY_ATTACHMENT = "EMPTY_ATTACHMENT"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    UNAUTHORIZED_SENDER = "UNAUTHORIZED_SENDER"


class DetectionKind(str, Enum):
    """How the submitted value was interpreted."""

    PERCENT_FORMATTED = "PERCENT_FORMATTED"
    """Cell carried an Excel percent number format."""

    EXPLICIT_PERCENT_TEXT = "EXPLICIT_PERCENT_TEXT"
    """Cell contained text such as ``"45%"``."""

    INTEGER_WITHOUT_PERCENT = "INTEGER_WITHOUT_PERCENT"
    """Cell contained ``45`` with no percent formatting."""

    DECIMAL_WITHOUT_PERCENT = "DECIMAL_WITHOUT_PERCENT"
    """Cell contained ``0.45`` with no percent formatting."""

    ZERO_OR_ONE = "ZERO_OR_ONE"
    """Cell contained exactly ``0`` or ``1``."""

    FORMULA = "FORMULA"
    """Cell contained an Excel formula."""

    EXCEL_ERROR = "EXCEL_ERROR"
    """Cell contained an Excel error literal such as ``#VALUE!``."""

    NON_NUMERIC = "NON_NUMERIC"
    """Cell contained text that is not a percentage."""

    BLANK = "BLANK"
    """Cell was empty."""


# Excel error literals recognised by openpyxl.
_EXCEL_ERRORS = {
    "#NULL!",
    "#DIV/0!",
    "#VALUE!",
    "#REF!",
    "#NAME?",
    "#NUM!",
    "#N/A",
    "#GETTING_DATA",
    "#SPILL!",
    "#CALC!",
    "#FIELD!",
    "#BLOCKED!",
    "#UNKNOWN!",
    "#CONNECT!",
    "#BUSY!",
}

# Number formats that unambiguously mean "the stored number is a fraction".
_PERCENT_FORMAT_TOKENS = ("%",)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class PercentageInterpretation:
    """Outcome of interpreting a single submitted percentage cell."""

    raw_value: Any = None
    raw_number_format: str | None = None
    raw_data_type: str | None = None

    normalized_pct: Decimal | None = None
    """Canonical fraction in [0, 1]. Only set when the value is unambiguous."""

    suggested_pct: Decimal | None = None
    """Proposed canonical fraction that still requires confirmation."""

    detection: DetectionKind = DetectionKind.BLANK
    status: FormatStatus = FormatStatus.MISSING
    exception_codes: list[str] = field(default_factory=list)

    confidence: str = "NONE"
    """Evidence category: NONE / LOW / MEDIUM / HIGH. Never a certainty."""

    evidence: list[str] = field(default_factory=list)

    @property
    def is_accepted(self) -> bool:
        return self.status is FormatStatus.ACCEPTED

    @property
    def needs_confirmation(self) -> bool:
        return self.status in (
            FormatStatus.SUGGESTED,
            FormatStatus.FORMAT_CONFIRMATION_REQUIRED,
        )

    @property
    def effective_pct(self) -> Decimal | None:
        """The value that may be used for comparisons.

        Only accepted values contribute.  A suggestion is never treated as
        an accepted number.
        """
        if self.status is FormatStatus.ACCEPTED:
            return self.normalized_pct
        return None

    def to_dict(self) -> dict:
        return {
            "raw_value": self.raw_value,
            "raw_number_format": self.raw_number_format,
            "raw_data_type": self.raw_data_type,
            "normalized_pct": (
                str(self.normalized_pct) if self.normalized_pct is not None else None
            ),
            "suggested_pct": (
                str(self.suggested_pct) if self.suggested_pct is not None else None
            ),
            "detection": self.detection.value,
            "status": self.status.value,
            "exception_codes": list(self.exception_codes),
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decimal_from_float(value: float) -> Decimal:
    """Convert a binary float to a Decimal without float artifacts."""
    return Decimal(str(value))


def is_percent_number_format(number_format: str | None) -> bool:
    """Return True when the Excel number format renders the value as a percent."""
    if not number_format:
        return False
    # Strip literal sections and quoted literals before looking for '%'.
    cleaned = []
    in_quotes = False
    in_brackets = False
    for char in number_format:
        if char == '"':
            in_quotes = not in_quotes
            continue
        if char == "[":
            in_brackets = True
            continue
        if char == "]":
            in_brackets = False
            continue
        if not in_quotes and not in_brackets:
            cleaned.append(char)
    text = "".join(cleaned)
    return any(token in text for token in _PERCENT_FORMAT_TOKENS)


def parse_explicit_percent_text(value: Any) -> Decimal | None:
    """Parse text such as ``"45%"`` or ``"45 %"`` into a canonical fraction."""
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    if not text.endswith("%"):
        return None

    number_part = text[:-1].strip().replace(",", "")
    if not number_part:
        return None

    try:
        number = Decimal(number_part)
    except (InvalidOperation, ValueError):
        return None

    return number / Decimal(100)


def _looks_like_excel_error(value: Any) -> bool:
    return isinstance(value, str) and value.strip().upper() in _EXCEL_ERRORS


def _is_formula(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("=")


# ---------------------------------------------------------------------------
# Column wide diagnostics
# ---------------------------------------------------------------------------


@dataclass
class ColumnProfile:
    """Column-wide view of all non-blank completion cells in one sheet."""

    total_cells: int = 0
    nonblank_cells: int = 0

    percent_formatted: int = 0
    explicit_percent_text: int = 0
    plain_integer: int = 0
    plain_decimal: int = 0
    zero_or_one: int = 0
    formulas: int = 0
    excel_errors: int = 0
    non_numeric: int = 0

    integer_values: list[Decimal] = field(default_factory=list)
    decimal_values: list[Decimal] = field(default_factory=list)

    @property
    def is_all_integer(self) -> bool:
        return (
            self.nonblank_cells > 0
            and self.plain_integer == self.nonblank_cells
        )

    @property
    def is_all_decimal(self) -> bool:
        return (
            self.nonblank_cells > 0
            and self.plain_decimal == self.nonblank_cells
        )

    @property
    def is_all_explicit_percent(self) -> bool:
        return (
            self.nonblank_cells > 0
            and self.percent_formatted == self.nonblank_cells
        )

    @property
    def is_mixed(self) -> bool:
        """More than one representation family is present in the column."""
        families = sum(
            1
            for count in (
                self.percent_formatted,
                self.explicit_percent_text,
                self.plain_integer,
                self.plain_decimal,
                self.zero_or_one,
                self.formulas,
                self.excel_errors,
                self.non_numeric,
            )
            if count > 0
        )
        return families > 1

    @property
    def integers_all_in_0_100(self) -> bool:
        return bool(self.integer_values) and all(
            Decimal(0) <= value <= Decimal(100) for value in self.integer_values
        )

    @property
    def decimals_all_in_0_1(self) -> bool:
        return bool(self.decimal_values) and all(
            Decimal(0) <= value <= Decimal(1) for value in self.decimal_values
        )

    def to_dict(self) -> dict:
        return {
            "total_cells": self.total_cells,
            "nonblank_cells": self.nonblank_cells,
            "percent_formatted": self.percent_formatted,
            "explicit_percent_text": self.explicit_percent_text,
            "plain_integer": self.plain_integer,
            "plain_decimal": self.plain_decimal,
            "zero_or_one": self.zero_or_one,
            "formulas": self.formulas,
            "excel_errors": self.excel_errors,
            "non_numeric": self.non_numeric,
            "is_all_integer": self.is_all_integer,
            "is_all_decimal": self.is_all_decimal,
            "is_all_explicit_percent": self.is_all_explicit_percent,
            "is_mixed": self.is_mixed,
            "integers_all_in_0_100": self.integers_all_in_0_100,
            "decimals_all_in_0_1": self.decimals_all_in_0_1,
        }


def _classify_into_profile(
    profile: ColumnProfile,
    value: Any,
    number_format: str | None,
) -> None:
    """Add one raw cell to the column profile."""
    profile.total_cells += 1

    if value is None or (isinstance(value, str) and not value.strip()):
        return

    profile.nonblank_cells += 1

    if _is_formula(value):
        profile.formulas += 1
        return

    if _looks_like_excel_error(value):
        profile.excel_errors += 1
        return

    if isinstance(value, bool):
        profile.non_numeric += 1
        return

    if isinstance(value, str):
        parsed = parse_explicit_percent_text(value)
        if parsed is not None:
            profile.explicit_percent_text += 1
            return
        profile.non_numeric += 1
        return

    if isinstance(value, (int, float)):
        number = (
            Decimal(value)
            if isinstance(value, int)
            else _decimal_from_float(value)
        )
        if is_percent_number_format(number_format):
            profile.percent_formatted += 1
            return
        if number == Decimal(0) or number == Decimal(1):
            profile.zero_or_one += 1
            profile.decimal_values.append(number)
            return
        if number == number.to_integral_value():
            profile.plain_integer += 1
            profile.integer_values.append(number)
            return
        profile.plain_decimal += 1
        profile.decimal_values.append(number)
        return

    profile.non_numeric += 1


def build_column_profile(
    cells: Iterable[tuple[Any, str | None]],
) -> ColumnProfile:
    """Build a :class:`ColumnProfile` from ``(value, number_format)`` pairs."""
    profile = ColumnProfile()
    for value, number_format in cells:
        _classify_into_profile(profile, value, number_format)
    return profile


# ---------------------------------------------------------------------------
# Single cell interpretation
# ---------------------------------------------------------------------------


def interpret_percentage(
    value: Any,
    number_format: str | None = None,
    data_type: str | None = None,
    column_profile: ColumnProfile | None = None,
) -> PercentageInterpretation:
    """Interpret one submitted percentage cell.

    Parameters
    ----------
    value:
        The raw cell value.
    number_format:
        The raw Excel number format of the cell.
    data_type:
        The raw Excel data type of the cell (``n``, ``s``, ``f``, ``e``).
    column_profile:
        Optional column-wide context used as *supporting evidence* for
        ambiguous representations.  Historical similarity is never treated
        as proof of intent.
    """
    result = PercentageInterpretation(
        raw_value=value,
        raw_number_format=number_format,
        raw_data_type=data_type,
    )

    # ---------------------------------------------------------------- blank
    if value is None or (isinstance(value, str) and not value.strip()):
        result.detection = DetectionKind.BLANK
        result.status = FormatStatus.MISSING
        result.exception_codes.append(ExceptionCode.MISSING_VALUE.value)
        result.evidence.append("Cell is empty.")
        return result

    # -------------------------------------------------------------- formula
    if _is_formula(value):
        result.detection = DetectionKind.FORMULA
        result.status = FormatStatus.INVALID
        result.exception_codes.append(ExceptionCode.FORMULA_CELL.value)
        result.evidence.append(
            "Cell contains an Excel formula; formulas are not executed."
        )
        return result

    # ---------------------------------------------------------- excel error
    if _looks_like_excel_error(value):
        result.detection = DetectionKind.EXCEL_ERROR
        result.status = FormatStatus.INVALID
        result.exception_codes.append(ExceptionCode.EXCEL_ERROR.value)
        result.evidence.append(f"Cell contains Excel error {value!r}.")
        return result

    # ------------------------------------------------------------- boolean
    if isinstance(value, bool):
        result.detection = DetectionKind.NON_NUMERIC
        result.status = FormatStatus.INVALID
        result.exception_codes.append(ExceptionCode.NON_NUMERIC_VALUE.value)
        result.evidence.append("Boolean values are not valid percentages.")
        return result

    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, (int, float)):
        number = (
            Decimal(value) if isinstance(value, int) else _decimal_from_float(value)
        )
    else:
        number = None

    # ------------------------------------------------------ explicit "45%"
    if isinstance(value, str):
        parsed = parse_explicit_percent_text(value)
        if parsed is not None:
            result.detection = DetectionKind.EXPLICIT_PERCENT_TEXT
            result.evidence.append(
                f"Cell contains explicit percent text {value!r}."
            )
            if Decimal(0) <= parsed <= Decimal(1):
                result.normalized_pct = parsed
                result.status = FormatStatus.ACCEPTED
                result.confidence = "HIGH"
            else:
                result.suggested_pct = parsed
                result.status = FormatStatus.INVALID
                result.exception_codes.append(ExceptionCode.OUT_OF_RANGE.value)
                result.confidence = "HIGH"
            return result

        result.detection = DetectionKind.NON_NUMERIC
        result.status = FormatStatus.INVALID
        result.exception_codes.append(ExceptionCode.NON_NUMERIC_VALUE.value)
        result.evidence.append(f"Cell contains non-numeric text {value!r}.")
        return result

    # -------------------------------------------------------------- numeric
    if number is None:
        result.detection = DetectionKind.NON_NUMERIC
        result.status = FormatStatus.INVALID
        result.exception_codes.append(ExceptionCode.NON_NUMERIC_VALUE.value)
        result.evidence.append(f"Unsupported cell type: {type(value).__name__}.")
        return result

    # ------------------------------------------- explicitly percent formatted
    if is_percent_number_format(number_format):
        result.detection = DetectionKind.PERCENT_FORMATTED
        result.evidence.append(
            f"Excel number format {number_format!r} marks the value as a percent."
        )
        if Decimal(0) <= number <= Decimal(1):
            result.normalized_pct = number
            result.status = FormatStatus.ACCEPTED
            result.confidence = "HIGH"
        else:
            result.suggested_pct = number
            result.status = FormatStatus.INVALID
            result.exception_codes.append(ExceptionCode.OUT_OF_RANGE.value)
            result.confidence = "HIGH"
        return result

    # ------------------------------------------------------ unformatted int
    if number == number.to_integral_value():
        if number == Decimal(0) or number == Decimal(1):
            return _interpret_zero_or_one(result, number, column_profile)

        result.detection = DetectionKind.INTEGER_WITHOUT_PERCENT
        result.evidence.append(
            f"Cell contains the unformatted integer {number} with no percent "
            "number format."
        )

        if number < Decimal(0):
            result.suggested_pct = number / Decimal(100)
            result.status = FormatStatus.INVALID
            result.exception_codes.append(ExceptionCode.NEGATIVE_VALUE.value)
            result.confidence = "MEDIUM"
            return result

        if number > Decimal(100):
            result.suggested_pct = number / Decimal(100)
            result.status = FormatStatus.INVALID
            result.exception_codes.append(ExceptionCode.OUT_OF_RANGE.value)
            result.confidence = "MEDIUM"
            return result

        # 2..100 without percent formatting: a plausible "missing %" case.
        result.suggested_pct = number / Decimal(100)
        result.status = FormatStatus.FORMAT_CONFIRMATION_REQUIRED
        result.exception_codes.append(ExceptionCode.AMBIGUOUS_INTEGER.value)

        if column_profile is not None:
            if column_profile.is_all_integer and column_profile.integers_all_in_0_100:
                result.confidence = "MEDIUM"
                result.evidence.append(
                    "Column-wide diagnostic: every non-blank cell in this "
                    "column is an integer between 0 and 100, which supports "
                    "the suggestion that the percent symbol is missing."
                )
            elif column_profile.is_mixed:
                result.confidence = "LOW"
                result.exception_codes.append(
                    ExceptionCode.AMBIGUOUS_MIXED_FORMATS.value
                )
                result.evidence.append(
                    "Column-wide diagnostic: the column mixes several "
                    "representations, so no consistent intent can be inferred."
                )
            else:
                result.confidence = "LOW"
                result.evidence.append(
                    "Column-wide diagnostic: no consistent representation "
                    "pattern was found for this column."
                )
        else:
            result.confidence = "LOW"

        return result

    # ------------------------------------------------ unformatted fractional
    if number < Decimal(0):
        result.detection = DetectionKind.DECIMAL_WITHOUT_PERCENT
        result.suggested_pct = number / Decimal(100)
        result.status = FormatStatus.INVALID
        result.exception_codes.append(ExceptionCode.NEGATIVE_VALUE.value)
        result.confidence = "MEDIUM"
        result.evidence.append(
            f"Cell contains the negative number {number}."
        )
        return result

    if Decimal(0) <= number <= Decimal(1):
        result.detection = DetectionKind.DECIMAL_WITHOUT_PERCENT
        result.suggested_pct = number
        result.status = FormatStatus.FORMAT_CONFIRMATION_REQUIRED
        result.exception_codes.append(ExceptionCode.AMBIGUOUS_DECIMAL.value)
        result.evidence.append(
            f"Cell contains the unformatted decimal {number} between 0 and 1; "
            "it may be a fraction without percent formatting."
        )

        if column_profile is not None:
            if column_profile.is_all_decimal and column_profile.decimals_all_in_0_1:
                result.confidence = "MEDIUM"
                result.evidence.append(
                    "Column-wide diagnostic: every non-blank cell in this "
                    "column is a decimal between 0 and 1, which supports the "
                    "suggestion that the percent formatting is missing."
                )
            elif column_profile.is_mixed:
                result.confidence = "LOW"
                result.exception_codes.append(
                    ExceptionCode.AMBIGUOUS_MIXED_FORMATS.value
                )
                result.evidence.append(
                    "Column-wide diagnostic: the column mixes several "
                    "representations, so no consistent intent can be inferred."
                )
            else:
                result.confidence = "LOW"
        else:
            result.confidence = "LOW"

        return result

    # --------------------------------------------------- decimal above 1
    result.detection = DetectionKind.DECIMAL_WITHOUT_PERCENT
    result.suggested_pct = number / Decimal(100)
    result.status = FormatStatus.INVALID
    result.exception_codes.append(ExceptionCode.OUT_OF_RANGE.value)
    result.confidence = "MEDIUM"
    result.evidence.append(
        f"Cell contains {number}, which is outside the canonical range [0, 1] "
        "and outside the 0-100 integer range."
    )
    return result


def _interpret_zero_or_one(
    result: PercentageInterpretation,
    number: Decimal,
    column_profile: ColumnProfile | None,
) -> PercentageInterpretation:
    """Handle the specially ambiguous ``0`` and ``1`` values."""
    result.detection = DetectionKind.ZERO_OR_ONE
    result.evidence.append(
        f"Cell contains the unformatted value {number}; both '0 percent' and "
        "'100 percent' are plausible readings."
    )

    # The literal value is a valid canonical fraction in either reading, so
    # it can be kept as the normalized value, but the intent is ambiguous and
    # therefore confirmation is required.
    result.normalized_pct = number
    result.suggested_pct = number
    result.status = FormatStatus.FORMAT_CONFIRMATION_REQUIRED
    result.exception_codes.append(ExceptionCode.AMBIGUOUS_ZERO_OR_ONE.value)

    if column_profile is not None:
        if column_profile.is_all_explicit_percent:
            result.confidence = "MEDIUM"
            result.evidence.append(
                "Column-wide diagnostic: all other cells in this column carry "
                "an explicit percent format, which supports reading the "
                "value as a percentage."
            )
        elif column_profile.is_mixed:
            result.confidence = "LOW"
            result.exception_codes.append(
                ExceptionCode.AMBIGUOUS_MIXED_FORMATS.value
            )
        else:
            result.confidence = "LOW"
    else:
        result.confidence = "LOW"

    return result


# ---------------------------------------------------------------------------
# Range validation
# ---------------------------------------------------------------------------


def validate_range(
    interpretation: PercentageInterpretation,
) -> tuple[bool, list[str]]:
    """Check the canonical range of an already interpreted value.

    Only an accepted value is range checked.  A suggestion is never
    promoted to an accepted value by this function.
    """
    codes: list[str] = []

    if interpretation.status is not FormatStatus.ACCEPTED:
        # A value that is not accepted is out of the accepted range by
        # definition.  The specific reason is already carried by the
        # interpretation itself.
        codes.extend(
            code
            for code in interpretation.exception_codes
            if code
            in (
                ExceptionCode.OUT_OF_RANGE.value,
                ExceptionCode.NEGATIVE_VALUE.value,
            )
        )
        return False, codes

    value = interpretation.normalized_pct

    if value is None:
        return False, codes

    if value < Decimal(0):
        codes.append(ExceptionCode.NEGATIVE_VALUE.value)
        return False, codes

    if value > Decimal(1):
        codes.append(ExceptionCode.OUT_OF_RANGE.value)
        return False, codes

    return True, codes


def summarize_interpretations(
    interpretations: Sequence[PercentageInterpretation],
) -> dict:
    """Return counts per status, used by the Master Excel and reporting views."""
    counts: dict[str, int] = {
        status.value: 0 for status in FormatStatus
    }
    for interpretation in interpretations:
        counts[interpretation.status.value] += 1
    return counts
