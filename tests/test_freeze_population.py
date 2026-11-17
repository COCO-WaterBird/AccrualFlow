"""
Tests for the population freeze script.

The frozen population is the authority the whole pipeline validates
against, and the exclusion of a department is a financial fact.  These
tests pin both behaviours.

The tests run against a real PostgreSQL database and are skipped when none
is reachable.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.submission_persistence_service import (  # noqa: E402
    connect,
    load_expected_population,
)
from scripts.freeze_population import (  # noqa: E402
    find_exclusions,
    load_mapping_rows,
    load_po_rows,
)


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


# ---------------------------------------------------------------------------
# find_exclusions
# ---------------------------------------------------------------------------


def _mapping(cost_center, department, manager, requester_count=3):
    return {
        "cost_center": cost_center,
        "department": department,
        "manager": manager,
        "requester_count": requester_count,
    }


def _po(cost_center, po_number="1", po_line=1):
    return {
        "po_number": po_number,
        "po_line": po_line,
        "cost_center": cost_center,
        "description": "d",
        "po_line_amount": None,
        "cumulative_gr_amount": None,
        "currency": "USD",
        "po_status": "Open",
        "department": "D",
        "manager": "m@example.com",
    }


def test_department_without_po_lines_is_excluded():
    mapping = [_mapping("2000-100", "Manufacturing", "a@example.com")]
    po_rows = []

    included, exclusions = find_exclusions(mapping, po_rows)

    assert included == []
    assert len(exclusions) == 1
    assert exclusions[0]["cost_center"] == "2000-100"
    assert exclusions[0]["reason"] == "NO_PO_LINES"
    assert exclusions[0]["po_line_count"] == 0


def test_department_without_manager_is_excluded():
    mapping = [_mapping("2000-100", "Manufacturing", "")]
    po_rows = [_po("2000-100")]

    included, exclusions = find_exclusions(mapping, po_rows)

    assert included == []
    assert exclusions[0]["reason"] == "NO_MANAGER"


def test_department_with_po_lines_and_manager_is_included():
    mapping = [_mapping("2000-100", "Manufacturing", "a@example.com")]
    po_rows = [_po("2000-100")]

    included, exclusions = find_exclusions(mapping, po_rows)

    assert len(included) == 1
    assert exclusions == []


def test_po_lines_of_an_excluded_department_are_dropped():
    """A department without a manager cannot be asked to report."""
    mapping = [_mapping("2000-900", "Procurement", "")]
    po_rows = [_po("2000-900")]

    included, exclusions = find_exclusions(mapping, po_rows)

    assert included == []
    assert exclusions[0]["reason"] == "NO_MANAGER"
    assert exclusions[0]["po_line_count"] == 1


def test_unmapped_cost_center_is_reported():
    mapping = [_mapping("2000-100", "Manufacturing", "a@example.com")]
    po_rows = [_po("2000-100"), _po("2000-777")]

    included, exclusions = find_exclusions(mapping, po_rows)

    assert len(included) == 1
    assert any(
        item["reason"] == "COST_CENTER_NOT_IN_MAPPING"
        and item["cost_center"] == "2000-777"
        for item in exclusions
    )


def test_every_exclusion_carries_a_reason_and_a_count():
    mapping = [_mapping("2000-900", "Procurement", "a@example.com")]
    po_rows = []

    _, exclusions = find_exclusions(mapping, po_rows)

    for item in exclusions:
        assert item["reason"]
        assert item["po_line_count"] >= 0
        assert "cost_center" in item


# ---------------------------------------------------------------------------
# The real data
# ---------------------------------------------------------------------------


def test_mapping_rows_are_collapsed_per_cost_center():
    rows = load_mapping_rows(ENTITY)

    cost_centers = [row["cost_center"] for row in rows]

    assert len(cost_centers) == len(set(cost_centers))
    assert rows


def test_every_mapped_cost_center_has_a_manager():
    for row in load_mapping_rows(ENTITY):
        assert row["manager"], (
            f"{row['cost_center']} has no manager, so it has no recipient"
        )


def test_po_rows_are_not_multiplied_by_the_requester_count():
    """The mapping holds one row per requester; the join must collapse it."""
    rows = load_po_rows(ENTITY)

    keys = [(str(row["po_number"]), int(row["po_line"])) for row in rows]

    assert len(keys) == len(set(keys))


def test_population_is_frozen_for_the_reporting_month():
    rows = load_expected_population("2026-11", ENTITY)

    assert rows

    for row in rows:
        assert row["department_id"] == row["cost_center"]
        assert row["authorized_email"]
        assert row["department_owner"]


def test_every_populated_department_is_an_authorised_contact():
    population = load_expected_population("2026-11", ENTITY)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cost_center, authorized_email
                FROM department_contacts
                WHERE legal_entity = %s AND active = TRUE
                """,
                (ENTITY,),
            )
            contacts = {
                (row["cost_center"], row["authorized_email"].lower())
                for row in cur.fetchall()
            }

    assert contacts

    for row in population:
        assert (
            row["cost_center"],
            row["authorized_email"].lower(),
        ) in contacts


def test_excluded_departments_are_not_authorised_contacts():
    """A department that was not asked to report must never be authorised."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cost_center
                FROM population_exclusions
                WHERE reporting_month = %s
                  AND legal_entity = %s
                  AND exclusion_reason = 'NO_PO_LINES'
                """,
                ("2026-11-01", ENTITY),
            )
            excluded = {row["cost_center"] for row in cur.fetchall()}

            if not excluded:
                pytest.skip("No department was excluded for this month.")

            cur.execute(
                """
                SELECT cost_center
                FROM department_contacts
                WHERE legal_entity = %s AND active = TRUE
                """,
                (ENTITY,),
            )
            contacts = {row["cost_center"] for row in cur.fetchall()}

    assert excluded.isdisjoint(contacts)


def test_exclusion_ledger_is_recorded_for_finance():
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cost_center, exclusion_reason, po_line_count
                FROM population_exclusions
                WHERE reporting_month = %s AND legal_entity = %s
                """,
                ("2026-11-01", ENTITY),
            )
            rows = list(cur.fetchall())

    assert rows

    for row in rows:
        assert row["exclusion_reason"] in (
            "NO_PO_LINES",
            "NO_MANAGER",
            "COST_CENTER_NOT_IN_MAPPING",
        )


def test_population_csv_and_exclusion_csv_exist_for_finance():
    from backend.app.core.config import POPULATION_DIR

    month_dir = POPULATION_DIR / "2026-11"

    assert (month_dir / "expected_population.csv").exists()
    assert (month_dir / "excluded_departments.csv").exists()
