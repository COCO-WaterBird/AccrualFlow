"""
Tests for the Power BI reporting layer.

The views are the primary source for Power BI, so their numbers must agree
with the underlying accepted dataset. These tests run against a real
PostgreSQL database and are skipped when none is reachable.
"""

import pytest

from backend.app.services.submission_persistence_service import (
    ExpectedPopulationRow,
    connect,
    freeze_expected_population,
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


REPORTING_MONTH = "2098-07"


@pytest.fixture
def clean_month():
    month = f"{REPORTING_MONTH}-01"

    def _wipe():
        with connect() as conn:
            with conn.cursor() as cur:
                for statement in (
                    "DELETE FROM validation_issues WHERE reporting_month = %s",
                    "DELETE FROM submission_lines WHERE reporting_month = %s",
                    "DELETE FROM department_submissions WHERE reporting_month = %s",
                    "DELETE FROM department_status_history WHERE reporting_month = %s",
                    "DELETE FROM master_export_runs WHERE reporting_month = %s",
                    "DELETE FROM expected_po_lines WHERE reporting_month = %s",
                    "DELETE FROM monthly_completion_baselines WHERE reporting_month = %s",
                    "DELETE FROM reporting_periods WHERE reporting_month = %s",
                ):
                    cur.execute(statement, (month,))

    _wipe()
    yield REPORTING_MONTH
    _wipe()


def _seed():
    rows = [
        ExpectedPopulationRow(
            po_number=f"PO{index:05d}",
            po_line=1,
            legal_entity="2000",
            department_id="2000-100",
            department_name="Manufacturing Engineering",
            cost_center="2000-100",
            department_owner="alex.chen@example.com",
            authorized_email="alex.chen@example.com",
            po_line_amount="1000.00",
            previous_completion_pct="0.40",
            baseline_status="PRESENT",
        )
        for index in range(1, 6)
    ]

    freeze_expected_population(REPORTING_MONTH, rows)


def test_views_exist_and_expose_the_expected_columns(clean_month):
    _seed()

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'vw_pbi_department_submission_overview'
                """
            )
            columns = {row["column_name"] for row in cur.fetchall()}

    assert "expected_line_count" in columns
    assert "accepted_line_count" in columns
    assert "missing_line_count" in columns
    assert "review_required_count" in columns
    assert "line_acceptance_rate_pct" in columns
    assert "department_status" in columns


def test_department_overview_counts_the_expected_lines(clean_month):
    _seed()

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    expected_line_count,
                    accepted_line_count,
                    line_acceptance_rate_pct
                FROM vw_pbi_department_submission_overview
                WHERE reporting_month = %s
                """,
                (f"{clean_month}-01",),
            )
            row = cur.fetchone()

    assert row["expected_line_count"] == 5
    assert row["accepted_line_count"] == 0
    assert row["line_acceptance_rate_pct"] == 0.0


def test_completion_trends_never_fabricates_a_value(clean_month):
    _seed()

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS total, COUNT(trend_pct) AS with_value
                FROM vw_pbi_completion_trends
                WHERE reporting_month = %s
                """,
                (f"{clean_month}-01",),
            )
            row = cur.fetchone()

    # Every expected line appears, but no line has a trend value yet.
    assert row["total"] == 5
    assert row["with_value"] == 0


def test_the_master_export_is_recorded_for_the_freshness_view(clean_month):
    from backend.app.services.master_excel_service import export_master_excel

    _seed()

    export_master_excel(clean_month, "2000")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT export_run_count, last_export_file
                FROM vw_pbi_reporting_freshness
                WHERE reporting_month = %s
                """,
                (f"{clean_month}-01",),
            )
            row = cur.fetchone()

    assert row["export_run_count"] == 1
    assert row["last_export_file"].startswith("CapEx_Completion_Master_")