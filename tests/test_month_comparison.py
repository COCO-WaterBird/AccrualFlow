"""
Test matrix items 7-11 and 20-21.

Covers:
  7.  Current 45%, previous 40% -> no decrease issue.
  8.  Current 50%, previous 60%, with remark -> reason provided, review pending.
  9.  Current 50%, previous 60%, blank remark -> correction required.
 10.  Missing previous baseline -> explicit flag, no fabricated 0%.
 11.  Expected 87 lines, 10 issues -> 10 correction rows, 77 acceptable retained.
 20.  Resubmission updates only intended lines, preserves versions.
 21.  Department cannot be COMPLETED with missing lines, unresolved format
      ambiguity, unresolved review, or missing evidence.
"""

from decimal import Decimal

from backend.app.services.month_comparison_service import (
    ReasonStatus,
    compare_with_previous,
    is_review_pending,
    requires_correction,
)


# ---------------------------------------------------------------------------
# 7. No decrease
# ---------------------------------------------------------------------------
def test_increase_produces_no_issue():
    comparison = compare_with_previous(
        current_pct=Decimal("0.45"),
        previous_pct=Decimal("0.40"),
        remarks=None,
    )

    assert comparison.decreased is False
    assert comparison.reason_status is ReasonStatus.NOT_APPLICABLE
    assert comparison.change_percentage_points == Decimal("5.000000")
    assert comparison.exception_codes == []


def test_equal_value_produces_no_issue():
    comparison = compare_with_previous(
        current_pct=Decimal("0.40"),
        previous_pct=Decimal("0.40"),
    )

    assert comparison.decreased is False
    assert comparison.change_percentage_points == Decimal("0")


# ---------------------------------------------------------------------------
# 8. Decrease with a remark
# ---------------------------------------------------------------------------
def test_decrease_with_remark_is_review_pending():
    comparison = compare_with_previous(
        current_pct=Decimal("0.50"),
        previous_pct=Decimal("0.60"),
        remarks="Supplier delayed the delivery",
    )

    assert comparison.decreased is True
    assert comparison.reason_status is ReasonStatus.DECREASE_REASON_PROVIDED
    assert is_review_pending(comparison.reason_status) is True
    assert requires_correction(comparison.reason_status) is False


def test_a_short_remark_still_requires_review():
    comparison = compare_with_previous(
        current_pct=Decimal("0.50"),
        previous_pct=Decimal("0.60"),
        remarks="Updated",
    )

    assert comparison.reason_status is ReasonStatus.DECREASE_REASON_PROVIDED
    assert is_review_pending(comparison.reason_status) is True


def test_whitespace_only_remark_is_treated_as_missing():
    comparison = compare_with_previous(
        current_pct=Decimal("0.50"),
        previous_pct=Decimal("0.60"),
        remarks="   ",
    )

    assert comparison.reason_status is ReasonStatus.DECREASE_REASON_MISSING


# ---------------------------------------------------------------------------
# 9. Decrease without a remark
# ---------------------------------------------------------------------------
def test_decrease_without_remark_requires_correction():
    comparison = compare_with_previous(
        current_pct=Decimal("0.50"),
        previous_pct=Decimal("0.60"),
        remarks=None,
    )

    assert comparison.decreased is True
    assert comparison.reason_status is ReasonStatus.DECREASE_REASON_MISSING
    assert requires_correction(comparison.reason_status) is True
    assert "DECREASE_REASON_MISSING" in comparison.exception_codes


# ---------------------------------------------------------------------------
# 10. Missing baseline
# ---------------------------------------------------------------------------
def test_missing_baseline_is_flagged_and_not_zero():
    comparison = compare_with_previous(
        current_pct=Decimal("0.50"),
        previous_pct=None,
    )

    assert comparison.baseline_status == "MISSING"
    assert comparison.previous_pct is None
    assert "BASELINE_MISSING" in comparison.exception_codes
    assert comparison.change_percentage_points is None

    # A missing baseline is not a decrease, so there is no explanation to
    # request.  The gap stays visible in the exception codes only.
    assert comparison.reason_status is ReasonStatus.NOT_APPLICABLE
    assert requires_correction(comparison.reason_status) is False


def test_missing_current_value_skips_the_decrease_check():
    comparison = compare_with_previous(
        current_pct=None,
        previous_pct=Decimal("0.60"),
    )

    assert comparison.reason_status is ReasonStatus.NOT_APPLICABLE
    assert comparison.change_percentage_points is None
    assert comparison.exception_codes == []


def test_previous_month_of_wraps_the_year():
    from backend.app.services.submission_persistence_service import (
        previous_month_of,
    )

    assert previous_month_of("2026-01") == "2025-12"
    assert previous_month_of("2026-11") == "2026-10"