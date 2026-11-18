"""
The PO status scope of the frozen expected population.

The owner decided that the monthly report covers the **OPEN** PO lines
only.  Two consequences follow, and both of them have to be visible:

1. **A filtered line is never silently dropped.**  It is written to the
   exclusion ledger, which is surfaced in the Master Excel and in the
   Power BI scope page.

2. **A line that closes during a reporting month stays in that month.**
   The month in which the work finished still shows the line and its
   final accepted value.  The line is marked as closed
   (``lifecycle_status = 'CLOSED_IN_MONTH'``) and is only removed from
   the population of the **next** month, where it appears on the ledger
   with its last active month.

The "last active month" is derived from the ERP import history
(``capex_po_history``).  It is only knowable when the ERP has been
imported more than once; with a single import batch there is no status
history, and the last active month stays unknown rather than being
guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from backend.app.services.submission_persistence_service import (
    connect,
    previous_month_of,
)


# The status that is included in the reporting scope.
INCLUDED_STATUS = "Open"

# The statuses that are filtered out of the reporting scope.
EXCLUDED_STATUSES = ("On Hold", "Closed")

# The lifecycle state of a frozen line.
LIFECYCLE_ACTIVE = "ACTIVE"
LIFECYCLE_CLOSED_IN_MONTH = "CLOSED_IN_MONTH"

# The reason recorded on the exclusion ledger for a filtered line.
EXCLUSION_REASON_STATUS = "PO_STATUS_NOT_IN_SCOPE"
EXCLUSION_REASON_CLOSED = "PO_CLOSED"


@dataclass
class StatusExclusion:
    """One PO line that was filtered out of the population."""

    po_number: str
    po_line: int
    po_status: str
    cost_center: str
    department_name: str | None = None
    manager: str | None = None
    po_description: str | None = None
    po_line_amount: object | None = None
    currency: str | None = None
    last_active_month: date | None = None
    exclusion_reason: str = EXCLUSION_REASON_STATUS


@dataclass
class ClosedInMonth:
    """One PO line that closed during the reporting month."""

    po_number: str
    po_line: int
    po_status: str
    cost_center: str
    department_name: str | None = None
    po_description: str | None = None
    po_line_amount: object | None = None
    currency: str | None = None
    last_active_month: date | None = None


@dataclass
class StatusScopeResult:
    """The outcome of applying the status scope to one reporting month."""

    reporting_month: str
    included_count: int = 0
    excluded_count: int = 0
    excluded: list[StatusExclusion] = field(default_factory=list)
    closed_in_month: list[ClosedInMonth] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The ERP status history
# ---------------------------------------------------------------------------


def load_status_history(legal_entity: str) -> dict[tuple[str, int], list[dict]]:
    """Load the status history of every PO line from the ERP import log.

    The history is keyed by ``(po_number, po_line)`` and ordered from the
    oldest to the newest capture.  A line that appears in only one batch
    has no status history, so its last active month cannot be derived.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    h.po_number,
                    h.po_line_number,
                    h.po_data ->> 'po_status' AS po_status,
                    h.captured_at,
                    b.imported_at
                FROM capex_po_history h
                JOIN import_batch b
                  ON b.batch_id = h.batch_id
                WHERE b.status = 'SUCCESS'
                ORDER BY h.po_number, h.po_line_number, h.captured_at
                """
            )
            rows = list(cur.fetchall())

    history: dict[tuple[str, int], list[dict]] = {}

    for row in rows:
        key = (str(row["po_number"]), int(row["po_line_number"]))
        history.setdefault(key, []).append({
            "po_status": row["po_status"],
            "captured_at": row["captured_at"],
            "imported_at": row["imported_at"],
        })

    return history


def _capture_month(capture: dict) -> date | None:
    """Return the month of a capture, or None when it has no timestamp."""
    captured_at = capture.get("captured_at")

    if captured_at is None:
        return None

    return date(captured_at.year, captured_at.month, 1)


def _is_status(capture: dict, status: str) -> bool:
    return (capture.get("po_status") or "").strip().lower() == status.lower()


def classify_line_status(
    history: list[dict],
    current_status: str,
    reporting_month: str,
) -> tuple[date | None, bool, bool]:
    """Classify one PO line against the ERP status history.

    The result is a triple:

    ``last_active_month``
        The last month in which the line was part of the reporting scope.
        For a line that is still open this is the month of the newest
        open capture.  For a line that closed, it is the month in which
        it closed: the line was still active during that month, it just
        finished partway through it.  That is the month the line is
        retained in.

    ``known``
        Whether the answer is known.  It is only known when the ERP has
        been imported more than once, so that a status change could
        actually be observed.  With a single import batch there is no
        history to read, and the answer stays unknown rather than being
        guessed.

    ``closed_in_reporting_month``
        Whether the line closed during the reporting month itself.  Such
        a line stays in the population of that month, marked as closed.
    """
    reporting = date.fromisoformat(f"{reporting_month}-01")

    if len(history) < 2:
        return None, False, False

    # The newest month in which the line was still open.
    last_open_month: date | None = None

    for capture in reversed(history):
        if _is_status(capture, "Open"):
            month = _capture_month(capture)

            if month is not None and month <= reporting:
                last_open_month = month

            break

    if current_status.strip().lower() != "closed":
        # An open or suspended line was last active in the month of its
        # newest open capture.
        return last_open_month, True, False

    # The line is closed now.  Find the month in which it closed: the
    # first capture that shows it as closed.
    closing_month: date | None = None
    was_open_before = False

    for capture in history:
        if _is_status(capture, "Closed"):
            closing_month = _capture_month(capture)
            break

        if _is_status(capture, "Open"):
            was_open_before = True

    if closing_month is None or not was_open_before:
        # The line was already closed at the first capture, so it was
        # never active in this reporting cycle.
        return None, True, False

    closed_in_reporting_month = closing_month == reporting

    return closing_month, True, closed_in_reporting_month


# ---------------------------------------------------------------------------
# Applying the scope
# ---------------------------------------------------------------------------


def apply_status_scope(
    reporting_month: str,
    po_rows: list[dict],
    legal_entity: str = "2000",
    included_status: str = INCLUDED_STATUS,
) -> StatusScopeResult:
    """Split the ERP PO lines by their status.

    The lines whose status is ``included_status`` are the reporting scope.

    A line with a different status is an exclusion, except when it is a
    line that **closed during the reporting month**: such a line stays in
    the population of its last active month, marked as closed, and only
    leaves the population from the following month on.

    The result carries both lists, so the caller does not have to classify
    the lines a second time.
    """
    result = StatusScopeResult(reporting_month=reporting_month)

    history = load_status_history(legal_entity)

    for row in po_rows:
        status = (row.get("po_status") or "").strip()

        if status.lower() == included_status.lower():
            result.included_count += 1
            continue

        key = (str(row["po_number"]), int(row["po_line"]))
        last_active, _known, closed_in_month = classify_line_status(
            history.get(key, []),
            status,
            reporting_month,
        )

        if closed_in_month:
            # The line closed during this month.  It is retained in this
            # month's population, so it is not an exclusion yet.
            result.closed_in_month.append(
                ClosedInMonth(
                    po_number=str(row["po_number"]),
                    po_line=int(row["po_line"]),
                    po_status=status,
                    cost_center=str(row["cost_center"] or ""),
                    department_name=row.get("department"),
                    po_description=row.get("description"),
                    po_line_amount=row.get("po_line_amount"),
                    currency=row.get("currency"),
                    last_active_month=last_active,
                )
            )
            continue

        result.excluded.append(
            StatusExclusion(
                po_number=str(row["po_number"]),
                po_line=int(row["po_line"]),
                po_status=status,
                cost_center=str(row["cost_center"] or ""),
                department_name=row.get("department"),
                manager=row.get("manager"),
                po_description=row.get("description"),
                po_line_amount=row.get("po_line_amount"),
                currency=row.get("currency"),
                last_active_month=last_active,
                exclusion_reason=(
                    EXCLUSION_REASON_CLOSED
                    if status.lower() == "closed"
                    else EXCLUSION_REASON_STATUS
                ),
            )
        )
        result.excluded_count += 1

    return result


# ---------------------------------------------------------------------------
# Persisting the scope
# ---------------------------------------------------------------------------


def record_status_scope(
    reporting_month: str,
    legal_entity: str,
    result: StatusScopeResult,
) -> None:
    """Persist the applied status scope and the exclusion ledger.

    The ledger is rebuilt for the month, so it always matches the
    population that is actually frozen.
    """
    month = date.fromisoformat(f"{reporting_month}-01")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO population_status_scope (
                    reporting_month, legal_entity,
                    included_status, excluded_statuses,
                    retain_closed_in_last_active_month
                )
                VALUES (%s, %s, %s, %s, TRUE)
                ON CONFLICT (reporting_month, legal_entity)
                DO UPDATE SET
                    included_status = EXCLUDED.included_status,
                    excluded_statuses = EXCLUDED.excluded_statuses,
                    retain_closed_in_last_active_month = TRUE
                """,
                (
                    month,
                    legal_entity,
                    INCLUDED_STATUS,
                    ", ".join(EXCLUDED_STATUSES),
                ),
            )

            cur.execute(
                """
                DELETE FROM population_status_exclusions
                WHERE reporting_month = %s AND legal_entity = %s
                """,
                (month, legal_entity),
            )

            cur.execute(
                """
                DELETE FROM population_closed_lines
                WHERE reporting_month = %s AND legal_entity = %s
                """,
                (month, legal_entity),
            )

            for exclusion in result.excluded:
                cur.execute(
                    """
                    INSERT INTO population_status_exclusions (
                        reporting_month, legal_entity,
                        cost_center, department_name, manager,
                        po_number, po_line, po_status,
                        po_description, po_line_amount, currency,
                        last_active_month
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (
                        reporting_month, legal_entity,
                        po_number, po_line, po_status
                    )
                    DO UPDATE SET
                        department_name = EXCLUDED.department_name,
                        manager = EXCLUDED.manager,
                        last_active_month = EXCLUDED.last_active_month
                    """,
                    (
                        month,
                        legal_entity,
                        exclusion.cost_center,
                        exclusion.department_name,
                        exclusion.manager,
                        exclusion.po_number,
                        exclusion.po_line,
                        exclusion.po_status,
                        exclusion.po_description,
                        exclusion.po_line_amount,
                        exclusion.currency,
                        exclusion.last_active_month,
                    ),
                )

            for closed in result.closed_in_month:
                cur.execute(
                    """
                    INSERT INTO population_closed_lines (
                        reporting_month, legal_entity,
                        cost_center, department_name,
                        po_number, po_line,
                        po_description, po_line_amount, currency,
                        po_status, last_active_month
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (
                        reporting_month, legal_entity, po_number, po_line
                    )
                    DO UPDATE SET
                        department_name = EXCLUDED.department_name,
                        last_active_month = EXCLUDED.last_active_month
                    """,
                    (
                        month,
                        legal_entity,
                        closed.cost_center,
                        closed.department_name,
                        closed.po_number,
                        closed.po_line,
                        closed.po_description,
                        closed.po_line_amount,
                        closed.currency,
                        closed.po_status,
                        closed.last_active_month,
                    ),
                )


def load_status_scope(
    reporting_month: str,
    legal_entity: str = "2000",
) -> dict | None:
    """Load the status scope that was applied to a population."""
    month = date.fromisoformat(f"{reporting_month}-01")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    included_status,
                    excluded_statuses,
                    retain_closed_in_last_active_month,
                    created_at
                FROM population_status_scope
                WHERE reporting_month = %s AND legal_entity = %s
                """,
                (month, legal_entity),
            )
            return cur.fetchone()


def load_status_exclusions(
    reporting_month: str,
    legal_entity: str = "2000",
) -> list[dict]:
    """Load the PO lines that were filtered out of a population."""
    month = date.fromisoformat(f"{reporting_month}-01")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cost_center, department_name, manager,
                    po_number, po_line, po_status,
                    po_description, po_line_amount, currency,
                    last_active_month,
                    reported_to_finance, reported_at
                FROM population_status_exclusions
                WHERE reporting_month = %s AND legal_entity = %s
                ORDER BY cost_center, po_number, po_line
                """,
                (month, legal_entity),
            )
            return list(cur.fetchall())
