"""
Tests for the web dashboard.

The dashboard must show the same numbers as the reporting views that
Power BI reads.  A dashboard that disagrees with the official report
would be worse than no dashboard at all, so the two are checked against
each other here.
"""

import pytest

from backend.app.services import dashboard_service as dash
from backend.app.services.submission_persistence_service import connect


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


LIVE_MONTH = "2026-11"
DEMO_MONTH = "2098-11"


# ---------------------------------------------------------------------------
# The month list
# ---------------------------------------------------------------------------


def test_the_month_list_is_not_empty_and_is_sorted_newest_first():
    months = dash.available_months()

    assert months, "at least one reporting month must exist"
    assert months == sorted(months, reverse=True)


def test_every_month_in_the_list_is_a_valid_month():
    for month in dash.available_months():
        dash._month_start(month)


# ---------------------------------------------------------------------------
# The month validation
# ---------------------------------------------------------------------------


def test_a_malformed_month_is_rejected():
    with pytest.raises(ValueError):
        dash._month_start("2026-13")


def test_a_day_is_rejected_as_a_month():
    with pytest.raises(ValueError):
        dash._month_start("2026-11-01")


def test_a_month_without_a_padding_zero_is_rejected():
    with pytest.raises(ValueError):
        dash._month_start("2026-1")


# ---------------------------------------------------------------------------
# The value cleaning
# ---------------------------------------------------------------------------


def test_a_decimal_becomes_a_float():
    from decimal import Decimal

    assert dash._clean(Decimal("12.50")) == 12.5


def test_a_date_becomes_an_iso_string():
    from datetime import date

    assert dash._clean(date(2026, 11, 1)) == "2026-11-01"


def test_none_stays_none():
    assert dash._clean(None) is None


def test_an_integer_stays_an_integer():
    assert dash._clean(476) == 476
    assert isinstance(dash._clean(476), int)


def test_a_boolean_stays_a_boolean():
    assert dash._clean(True) is True


# ---------------------------------------------------------------------------
# The dashboard agrees with the views
# ---------------------------------------------------------------------------


def test_the_dashboard_agrees_with_the_department_view():
    rows = dash.department_overview(LIVE_MONTH)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM vw_pbi_department_submission_overview
                WHERE reporting_month = %s
                """,
                (f"{LIVE_MONTH}-01",),
            )
            expected = cur.fetchone()["count"]

    assert len(rows) == expected


def test_the_dashboard_agrees_with_the_status_exclusion_view():
    rows = dash.population_status_exclusions(LIVE_MONTH)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM vw_pbi_population_status_exclusions
                WHERE reporting_month = %s
                """,
                (f"{LIVE_MONTH}-01",),
            )
            expected = cur.fetchone()["count"]

    assert len(rows) == expected


def test_the_dashboard_agrees_with_the_population_scope_view():
    rows = dash.population_scope(LIVE_MONTH)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM vw_pbi_population_scope
                WHERE reporting_month = %s
                """,
                (f"{LIVE_MONTH}-01",),
            )
            expected = cur.fetchone()["count"]

    assert len(rows) == expected


def test_every_department_is_either_included_or_excluded():
    """The invariant behind Page 6.

    A department can never silently vanish from the reporting: it is
    either in the population or on the exclusion ledger.
    """
    rows = dash.population_scope(LIVE_MONTH)

    for row in rows:
        assert row["population_status"] in ("INCLUDED", "EXCLUDED")


def test_an_excluded_department_is_never_an_authorised_contact():
    rows = dash.population_scope(LIVE_MONTH)

    for row in rows:
        if row["population_status"] == "EXCLUDED":
            assert row["is_authorised_contact"] is False
            assert row["expected_line_count"] == 0


def test_an_included_department_has_at_least_one_line():
    rows = dash.population_scope(LIVE_MONTH)

    for row in rows:
        if row["population_status"] == "INCLUDED":
            assert row["expected_line_count"] > 0


def test_the_scope_line_count_matches_the_population_size():
    """The sum of the included departments is the whole population."""
    scope = dash.population_scope(LIVE_MONTH)
    included = [
        r for r in scope if r["population_status"] == "INCLUDED"
    ]

    total = sum(r["expected_line_count"] for r in included)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM expected_po_lines
                WHERE reporting_month = %s
                """,
                (f"{LIVE_MONTH}-01",),
            )
            expected = cur.fetchone()["n"]

    assert total == expected


# ---------------------------------------------------------------------------
# The status exclusions
# ---------------------------------------------------------------------------


def test_the_status_exclusions_are_all_not_open():
    rows = dash.population_status_exclusions(LIVE_MONTH)

    for row in rows:
        assert row["po_status"].upper() != "OPEN"


def test_the_status_exclusions_state_the_scope_that_was_applied():
    rows = dash.population_status_exclusions(LIVE_MONTH)

    if rows:
        assert rows[0]["included_status"] == "Open"
        assert "On Hold" in rows[0]["excluded_statuses"]


def test_the_status_exclusions_carry_the_population_size():
    rows = dash.population_status_exclusions(LIVE_MONTH)

    for row in rows:
        assert row["population_line_count"] > 0


# ---------------------------------------------------------------------------
# The trends
# ---------------------------------------------------------------------------


def test_the_trend_series_covers_every_month():
    series = dash.completion_trend_series()

    assert series

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(DISTINCT reporting_month) AS n
                FROM vw_pbi_completion_trends
                """
            )
            expected = cur.fetchone()["n"]

    assert len(series) == expected


def test_the_trend_series_is_ordered_by_month():
    series = dash.completion_trend_series()

    months = [r["reporting_month"] for r in series]

    assert months == sorted(months)


def test_the_trend_series_never_exceeds_one_hundred_percent():
    for row in dash.completion_trend_series():
        rate = row["acceptance_rate_pct"]
        assert rate is None or 0 <= rate <= 100


def test_the_accepted_count_never_exceeds_the_expected_count():
    for row in dash.completion_trend_series():
        assert row["accepted_line_count"] <= row["expected_line_count"]


# ---------------------------------------------------------------------------
# The JSON safety of the payload
# ---------------------------------------------------------------------------


def test_every_dashboard_row_is_json_serialisable():
    import json

    for reader in (
        lambda: dash.department_overview(LIVE_MONTH),
        lambda: dash.population_scope(LIVE_MONTH),
        lambda: dash.population_status_exclusions(LIVE_MONTH),
        lambda: dash.population_exclusions(LIVE_MONTH),
        lambda: dash.completion_trends(LIVE_MONTH),
        lambda: dash.validation_exceptions(LIVE_MONTH),
        lambda: dash.submission_audit(LIVE_MONTH),
        lambda: dash.reporting_freshness(LIVE_MONTH),
        lambda: dash.population_closed_lines(LIVE_MONTH),
        lambda: dash.completion_trend_series(),
    ):
        json.dumps(reader())


def test_the_demo_month_is_readable():
    """The throwaway period must render too, not only the live one."""
    assert dash.department_overview(DEMO_MONTH)
    assert dash.completion_trends(DEMO_MONTH)
