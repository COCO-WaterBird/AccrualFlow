"""
Tests for the PO status scope of the frozen population.

The owner decided that the monthly report covers the OPEN PO lines only,
that a filtered line is recorded on the exclusion ledger, and that a line
which closes DURING a reporting month stays in that month's population,
marked as closed.

The "closed during the month" rule depends on the ERP import history: a
status change can only be observed when the ERP was imported more than
once.  These tests build that history explicitly.

These tests run against a real PostgreSQL database and are skipped when
none is reachable.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from psycopg.types.json import Json

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.population_status_scope_service import (  # noqa: E402
    EXCLUDED_STATUSES,
    INCLUDED_STATUS,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_CLOSED_IN_MONTH,
    apply_status_scope,
    derive_last_active_month,
    is_closed_in_last_active_month,
)
from backend.app.services.submission_persistence_service import connect  # noqa: E402


pytestmark = pytest.mark.integration


def _database_available() -> bool:
    try:
        with connect():
            pass
    except Exception:  # noqa: BLE001
        return False
    return True


if not _database_available():
    pytest.skip(
        "No AccrualFlow database is reachable; skipping integration tests.",
        allow_module_level=True,
    )


ENTITY = "2000"
MONTH = "2097-04"


# ---------------------------------------------------------------------------
# A controllable ERP import history
# ---------------------------------------------------------------------------


@pytest.fixture
def erp_history():
    """Build a controllable ERP import history.

    The fixture removes the import batches it creates, so it never
    interferes with the real ERP data.
    """
    created_batches: list[int] = []

    def _batch(po_number: str, po_line: int, po_status: str, captured_at):
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO import_batch (
                        reporting_month, source_system, source_filename,
                        status, imported_at, completed_at, total_rows,
                        imported_rows
                    )
                    VALUES (
                        %s, 'Mock ERP', %s, 'SUCCESS', %s, %s, 1, 1
                    )
                    RETURNING batch_id
                    """,
                    (
                        captured_at.date().replace(day=1),
                        f"test-{po_number}-{captured_at.timestamp()}.xlsx",
                        captured_at,
                        captured_at,
                    ),
                )
                batch_id = cur.fetchone()["batch_id"]

                cur.execute(
                    """
                    INSERT INTO capex_po_history (
                        batch_id, po_number, po_line_number, po_data, captured_at
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        batch_id,
                        po_number,
                        po_line,
                        Json({"po_status": po_status}),
                        captured_at,
                    ),
                )

        created_batches.append(batch_id)

        return batch_id

    yield _batch

    if created_batches:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM capex_po_history
                    WHERE batch_id = ANY(%s)
                    """,
                    (created_batches,),
                )
                cur.execute(
                    """
                    DELETE FROM import_batch
                    WHERE batch_id = ANY(%s)
                    """,
                    (created_batches,),
                )


def _po(po_number, po_line, po_status, cost_center="2000-100"):
    return {
        "po_number": po_number,
        "po_line": po_line,
        "po_status": po_status,
        "cost_center": cost_center,
        "department": "Manufacturing Engineering",
        "manager": "a@example.com",
        "description": "Test PO line",
        "po_line_amount": "1000.00",
        "currency": "USD",
    }


# ---------------------------------------------------------------------------
# The status filter
# ---------------------------------------------------------------------------


def test_only_open_lines_are_in_the_reporting_scope(erp_history):
    erp_history("PO-OPEN", 1, "Open", datetime(2097, 4, 5, tzinfo=timezone.utc))

    scope = apply_status_scope(
        MONTH,
        [
            _po("PO-OPEN", 1, "Open"),
            _po("PO-HOLD", 1, "On Hold"),
            _po("PO-CLOSED", 1, "Closed"),
        ],
        ENTITY,
    )

    assert scope.included_count == 1
    assert scope.excluded_count == 2

    excluded = {(e.po_number, e.po_line) for e in scope.excluded}

    assert excluded == {("PO-HOLD", 1), ("PO-CLOSED", 1)}


def test_a_filtered_line_is_recorded_with_its_status(erp_history):
    erp_history("PO-HOLD", 1, "On Hold", datetime(2097, 4, 5, tzinfo=timezone.utc))

    scope = apply_status_scope(
        MONTH,
        [_po("PO-HOLD", 1, "On Hold")],
        ENTITY,
    )

    exclusion = scope.excluded[0]

    assert exclusion.po_status == "On Hold"
    assert exclusion.exclusion_reason == "PO_STATUS_NOT_IN_SCOPE"
    assert exclusion.cost_center == "2000-100"
    assert exclusion.manager == "a@example.com"


def test_the_included_status_is_open():
    assert INCLUDED_STATUS == "Open"
    assert "On Hold" in EXCLUDED_STATUSES
    assert "Closed" in EXCLUDED_STATUSES


# ---------------------------------------------------------------------------
# The last active month
# ---------------------------------------------------------------------------


def test_the_last_active_month_is_unknown_without_history():
    """With one capture there is no status change to observe."""
    captures = [{"po_status": "Open", "captured_at": None}]

    month, known = derive_last_active_month(captures, MONTH)

    assert month is None
    assert known is False


def test_the_last_active_month_is_derived_from_the_history():
    captures = [
        {
            "po_status": "Open",
            "captured_at": datetime(2097, 3, 10, tzinfo=timezone.utc),
        },
        {
            "po_status": "Open",
            "captured_at": datetime(2097, 4, 10, tzinfo=timezone.utc),
        },
    ]

    month, known = derive_last_active_month(captures, MONTH)

    assert known is True
    assert month == date(2097, 4, 1)


def test_a_line_that_was_open_in_march_is_last_active_in_april():
    """A line that closed in April was still active during April.

    It finished partway through April, so April is the month it is
    retained in.
    """
    captures = [
        {
            "po_status": "Open",
            "captured_at": datetime(2097, 3, 10, tzinfo=timezone.utc),
        },
        {
            "po_status": "Closed",
            "captured_at": datetime(2097, 4, 10, tzinfo=timezone.utc),
        },
    ]

    month, known = derive_last_active_month(captures, MONTH)

    assert known is True
    assert month == date(2097, 4, 1)


# ---------------------------------------------------------------------------
# A line that closes during the reporting month
# ---------------------------------------------------------------------------


def test_a_line_that_closes_during_the_month_is_detected(erp_history):
    """The line was open last month and is closed in this month."""
    erp_history(
        "PO-CLOSING", 1, "Open",
        datetime(2097, 3, 10, tzinfo=timezone.utc),
    )
    erp_history(
        "PO-CLOSING", 1, "Closed",
        datetime(2097, 4, 10, tzinfo=timezone.utc),
    )

    closed = is_closed_in_last_active_month(
        MONTH,
        [_po("PO-CLOSING", 1, "Closed")],
        ENTITY,
    )

    assert len(closed) == 1
    assert closed[0].po_number == "PO-CLOSING"
    assert closed[0].last_active_month == date(2097, 4, 1)


def test_a_line_that_was_already_closed_is_not_a_mid_month_close(erp_history):
    """It was closed in both captures, so it never was active."""
    erp_history(
        "PO-OLD", 1, "Closed",
        datetime(2097, 2, 10, tzinfo=timezone.utc),
    )
    erp_history(
        "PO-OLD", 1, "Closed",
        datetime(2097, 4, 10, tzinfo=timezone.utc),
    )

    closed = is_closed_in_last_active_month(
        MONTH,
        [_po("PO-OLD", 1, "Closed")],
        ENTITY,
    )

    assert closed == []


def test_a_line_that_closed_in_a_previous_month_is_not_a_mid_month_close(
    erp_history,
):
    """The closing happened before the reporting month."""
    erp_history(
        "PO-EARLY", 1, "Open",
        datetime(2097, 1, 10, tzinfo=timezone.utc),
    )
    erp_history(
        "PO-EARLY", 1, "Closed",
        datetime(2097, 2, 10, tzinfo=timezone.utc),
    )

    closed = is_closed_in_last_active_month(
        MONTH,
        [_po("PO-EARLY", 1, "Closed")],
        ENTITY,
    )

    assert closed == []


def test_a_line_without_history_is_not_a_mid_month_close(erp_history):
    """A single capture cannot show a closing."""
    erp_history(
        "PO-SINGLE", 1, "Closed",
        datetime(2097, 4, 10, tzinfo=timezone.utc),
    )

    closed = is_closed_in_last_active_month(
        MONTH,
        [_po("PO-SINGLE", 1, "Closed")],
        ENTITY,
    )

    assert closed == []


# ---------------------------------------------------------------------------
# The frozen population carries the state
# ---------------------------------------------------------------------------


def test_the_frozen_line_records_the_status_and_the_lifecycle():
    from backend.app.services.submission_persistence_service import (
        ExpectedPopulationRow,
    )

    row = ExpectedPopulationRow(
        po_number="PO-1",
        po_line=1,
        legal_entity="2000",
        department_id="2000-100",
        department_name="Manufacturing Engineering",
        cost_center="2000-100",
        po_status="Open",
        lifecycle_status=LIFECYCLE_ACTIVE,
    )

    assert row.po_status == "Open"
    assert row.lifecycle_status == LIFECYCLE_ACTIVE
    assert LIFECYCLE_CLOSED_IN_MONTH == "CLOSED_IN_MONTH"


def test_a_frozen_population_only_contains_open_lines():
    """The real population for 2026-11 only holds the Open lines."""
    from backend.app.services.submission_persistence_service import (
        load_expected_population,
    )

    rows = load_expected_population("2026-11", ENTITY)

    if not rows:
        pytest.skip("The population for 2026-11 is not frozen.")

    statuses = {row["po_status"] for row in rows}

    assert statuses == {"Open"}


def test_the_status_exclusion_ledger_holds_the_filtered_lines():
    from backend.app.services.population_status_scope_service import (
        load_status_exclusions,
    )

    rows = load_status_exclusions("2026-11", ENTITY)

    if not rows:
        pytest.skip("No status exclusions were recorded for 2026-11.")

    for row in rows:
        assert row["po_status"] in EXCLUDED_STATUSES
        assert row["po_number"]
        assert row["po_line"] is not None


def test_the_status_scope_is_recorded_for_the_month():
    from backend.app.services.population_status_scope_service import (
        load_status_scope,
    )

    scope = load_status_scope("2026-11", ENTITY)

    if scope is None:
        pytest.skip("No status scope was recorded for 2026-11.")

    assert scope["included_status"] == "Open"
    assert scope["retain_closed_in_last_active_month"] is True
