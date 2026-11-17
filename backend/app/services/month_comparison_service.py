"""
Month-over-month comparison for CapEx completion percentages.

The comparison is always made against the **previous finalized** reporting
month for the same expected PO line key:

    reporting period + entity + PO number + PO line

Differences are expressed in **percentage points**:

    delta_pp = (current_fraction - previous_fraction) * 100

A missing previous baseline is reported explicitly as ``BASELINE_MISSING``.
It is never silently treated as 0%.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from backend.app.services.percentage_validation_service import ExceptionCode


class ReasonStatus(str, Enum):
    """Review state of a decrease explanation."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    """The completion did not decrease."""

    DECREASE_REASON_MISSING = "DECREASE_REASON_MISSING"
    """The completion decreased and no remark was supplied."""

    DECREASE_REASON_PROVIDED = "DECREASE_REASON_PROVIDED"
    """The completion decreased and a remark was supplied, pending review."""

    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    """A business exception needs human review."""


@dataclass
class MonthComparison:
    """Result of comparing one PO line against its previous baseline."""

    current_pct: Decimal | None = None
    previous_pct: Decimal | None = None
    baseline_period: str | None = None

    change_percentage_points: Decimal | None = None
    """Change expressed in percentage points."""

    decreased: bool = False
    reason_status: ReasonStatus = ReasonStatus.NOT_APPLICABLE

    exception_codes: list[str] = field(default_factory=list)
    remarks: str | None = None

    baseline_status: str = "PRESENT"
    """PRESENT / MISSING / NOT_COMPARABLE."""

    def to_dict(self) -> dict:
        return {
            "current_pct": (
                str(self.current_pct) if self.current_pct is not None else None
            ),
            "previous_pct": (
                str(self.previous_pct) if self.previous_pct is not None else None
            ),
            "baseline_period": self.baseline_period,
            "change_percentage_points": (
                str(self.change_percentage_points)
                if self.change_percentage_points is not None
                else None
            ),
            "decreased": self.decreased,
            "reason_status": self.reason_status.value,
            "exception_codes": list(self.exception_codes),
            "remarks": self.remarks,
            "baseline_status": self.baseline_status,
        }


def _normalize_remarks(remarks: object) -> str | None:
    """Return a trimmed remark string, or None when effectively empty."""
    if remarks is None:
        return None
    text = str(remarks).strip()
    return text or None


def compare_with_previous(
    current_pct: Decimal | None,
    previous_pct: Decimal | None,
    remarks: object = None,
    baseline_period: str | None = None,
    baseline_status: str = "PRESENT",
) -> MonthComparison:
    """Compare the current completion against the previous finalized value.

    Parameters
    ----------
    current_pct:
        The accepted canonical fraction for the current period.  ``None``
        means the current value is missing or not accepted, in which case no
        decrease check is performed.
    previous_pct:
        The previous finalized canonical fraction, or ``None`` when no
        baseline exists.
    remarks:
        The free-text explanation supplied by the department.
    """
    comparison = MonthComparison(
        current_pct=current_pct,
        previous_pct=previous_pct,
        baseline_period=baseline_period,
        remarks=_normalize_remarks(remarks),
        baseline_status=baseline_status,
    )

    # No accepted current value: the decrease check cannot be applied.
    if current_pct is None:
        comparison.reason_status = ReasonStatus.NOT_APPLICABLE
        return comparison

    # No previous baseline: report explicitly, never assume 0%.
    #
    # A missing baseline is an audit-trail flag, not a decrease.  There is
    # no decrease to explain, so the reason status stays NOT_APPLICABLE
    # and the submitted value is not blocked.  The BASELINE_MISSING
    # exception code still records the gap in the validation issues.
    if previous_pct is None:
        comparison.baseline_status = "MISSING"
        comparison.reason_status = ReasonStatus.NOT_APPLICABLE
        comparison.exception_codes.append(ExceptionCode.BASELINE_MISSING.value)
        return comparison

    delta = (current_pct - previous_pct) * Decimal(100)
    comparison.change_percentage_points = delta

    if current_pct < previous_pct:
        comparison.decreased = True
        if comparison.remarks:
            # A remark only passes a literal non-empty check.  It is never
            # treated as an acceptable explanation on its own.
            comparison.reason_status = ReasonStatus.DECREASE_REASON_PROVIDED
            comparison.exception_codes.append(
                ExceptionCode.DECREASE_REASON_PROVIDED.value
            )
        else:
            comparison.reason_status = ReasonStatus.DECREASE_REASON_MISSING
            comparison.exception_codes.append(
                ExceptionCode.DECREASE_REASON_MISSING.value
            )
    else:
        comparison.reason_status = ReasonStatus.NOT_APPLICABLE

    return comparison


def is_review_pending(reason_status: ReasonStatus) -> bool:
    """Return True when the reason status still requires human review."""
    return reason_status in (
        ReasonStatus.DECREASE_REASON_PROVIDED,
        ReasonStatus.DECREASE_REASON_MISSING,
        ReasonStatus.REVIEW_REQUIRED,
    )


def requires_correction(reason_status: ReasonStatus) -> bool:
    """Return True when a correction request must be issued for this line."""
    return reason_status in (
        ReasonStatus.DECREASE_REASON_MISSING,
        ReasonStatus.REVIEW_REQUIRED,
    )
