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
                    "DELETE FROM population_exclusions WHERE reporting_month = %s",
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


# ---------------------------------------------------------------------------
# The population scope views
#
# Finance has to see which departments were asked to report and which were
# filtered out.  These tests pin that the scope is complete and that an
# excluded department is never treated as a reporting department.
# ---------------------------------------------------------------------------


def _record_exclusion(month: str, cost_center: str, reason: str = "NO_PO_LINES"):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO population_exclusions (
                    reporting_month, legal_entity,
                    cost_center, department_name, manager,
                    exclusion_reason, po_line_count
                )
                VALUES (%s, '2000', %s, 'Procurement', 'buyer@example.com', %s, 0)
                ON CONFLICT (
                    reporting_month, legal_entity,
                    cost_center, exclusion_reason
                )
                DO UPDATE SET department_name = EXCLUDED.department_name
                """,
                (f"{month}-01", cost_center, reason),
            )


def test_the_exclusions_view_exposes_the_excluded_departments(clean_month):
    _seed()
    _record_exclusion(clean_month, "2000-900")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cost_center, exclusion_reason, po_line_count,
                       population_line_count, is_authorised_contact
                FROM vw_pbi_population_exclusions
                WHERE reporting_month = %s
                """,
                (f"{clean_month}-01",),
            )
            rows = list(cur.fetchall())

    assert len(rows) == 1

    row = rows[0]

    assert row["cost_center"] == "2000-900"
    assert row["exclusion_reason"] == "NO_PO_LINES"
    assert row["population_line_count"] == 5
    assert row["is_authorised_contact"] is False


def test_the_scope_view_covers_every_department(clean_month):
    """Every department is either in the population or on the ledger."""
    _seed()
    _record_exclusion(clean_month, "2000-900")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cost_center, population_status,
                       exclusion_reason, expected_line_count,
                       is_authorised_contact
                FROM vw_pbi_population_scope
                WHERE reporting_month = %s
                ORDER BY cost_center
                """,
                (f"{clean_month}-01",),
            )
            rows = list(cur.fetchall())

    assert len(rows) == 2

    included = [r for r in rows if r["population_status"] == "INCLUDED"]
    excluded = [r for r in rows if r["population_status"] == "EXCLUDED"]

    assert len(included) == 1
    assert included[0]["cost_center"] == "2000-100"
    assert included[0]["expected_line_count"] == 5
    assert included[0]["is_authorised_contact"] is True

    assert len(excluded) == 1
    assert excluded[0]["cost_center"] == "2000-900"
    assert excluded[0]["exclusion_reason"] == "NO_PO_LINES"
    # An excluded department was never asked to report.
    assert excluded[0]["expected_line_count"] == 0
    assert excluded[0]["is_authorised_contact"] is False


def test_the_scope_view_counts_match_the_population(clean_month):
    _seed()
    _record_exclusion(clean_month, "2000-900")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT SUM(expected_line_count) AS total
                FROM vw_pbi_population_scope
                WHERE reporting_month = %s
                """,
                (f"{clean_month}-01",),
            )
            scope_total = cur.fetchone()["total"]

            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM expected_po_lines
                WHERE reporting_month = %s
                """,
                (f"{clean_month}-01",),
            )
            population_total = cur.fetchone()["total"]

    assert scope_total == population_total