"""
Tests for the visibility of the excluded departments.

A department that was filtered out of the frozen population was never
asked to report.  Finance still has to be able to see that the
department exists, that it was deliberately left out, and why - otherwise
the report silently understates the scope of the collection.

These tests run against a real PostgreSQL database and are skipped when
none is reachable.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
MONTH = "2026-11"


def _exclusions() -> list[dict]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cost_center, department_name, manager,
                       exclusion_reason, po_line_count
                FROM population_exclusions
                WHERE reporting_month = %s AND legal_entity = %s
                """,
                (f"{MONTH}-01", ENTITY),
            )
            return list(cur.fetchall())


def test_the_excluded_department_is_recorded():
    rows = _exclusions()

    assert rows, "No exclusion was recorded for the reporting month."

    for row in rows:
        assert row["exclusion_reason"]
        assert row["cost_center"]


def test_the_master_excel_reports_the_excluded_department(tmp_path):
    from openpyxl import load_workbook

    from backend.app.services.master_excel_service import export_master_excel

    exclusions = _exclusions()

    if not exclusions:
        pytest.skip("No department was excluded for this month.")

    result = export_master_excel(MONTH, ENTITY, output_dir=tmp_path)

    workbook = load_workbook(result.file_path, read_only=True)
    worksheet = workbook["Department_Summary"]

    rows = list(worksheet.iter_rows(values_only=True))
    headers = list(rows[0])

    population_status = headers.index("Population Status")
    exclusion_reason = headers.index("Exclusion Reason")
    cost_center = headers.index("Cost Center")

    reported = {
        row[cost_center]: row
        for row in rows[1:]
        if row[population_status] == "EXCLUDED"
    }

    for exclusion in exclusions:
        assert exclusion["cost_center"] in reported

        row = reported[exclusion["cost_center"]]

        assert row[exclusion_reason] == exclusion["exclusion_reason"]

    workbook.close()


def test_the_excluded_department_is_not_counted_as_a_reporting_department(
    tmp_path,
):
    from backend.app.services.master_excel_service import export_master_excel

    exclusions = _exclusions()

    if not exclusions:
        pytest.skip("No department was excluded for this month.")

    result = export_master_excel(MONTH, ENTITY, output_dir=tmp_path)

    assert result.department_count == 6
    assert len(exclusions) == 1


def test_every_mapped_department_is_either_included_or_excluded():
    """No department may simply vanish from the report."""
    from backend.app.services.submission_persistence_service import (
        load_expected_population,
    )

    population = load_expected_population(MONTH, ENTITY)

    included = {row["cost_center"] for row in population}
    excluded = {row["cost_center"] for row in _exclusions()}

    assert included
    assert included.isdisjoint(excluded)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT cost_center
                FROM costcenter_mapping
                WHERE legal_entity = %s
                """,
                (ENTITY,),
            )
            mapped = {row["cost_center"] for row in cur.fetchall()}

    # Every mapped cost centre is accounted for: either it is in the
    # population, or it is on the exclusion ledger.
    assert mapped == (included | excluded)
