"""
The web dashboard data layer.

Every page of the dashboard reads one of the reporting views.  The views
are the same ones that Power BI reads, so the dashboard and the official
report can never disagree.

The service is read only.  It opens a connection per call, runs a
parameterised query and returns plain dictionaries.
"""

from __future__ import annotations

from backend.app.services.submission_persistence_service import connect


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rows(sql: str, params: tuple) -> list[dict]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def _clean(value):
    """Turn a database value into something JSON can carry.

    ``Decimal`` becomes a float, ``date`` and ``datetime`` become ISO
    strings, and ``None`` stays ``None``.
    """
    if value is None:
        return None

    if isinstance(value, bool) or isinstance(value, (int, str)):
        return value

    if isinstance(value, float):
        return value

    if hasattr(value, "isoformat"):
        return value.isoformat()

    # Decimal and anything else numeric.
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _clean_rows(rows: list[dict]) -> list[dict]:
    return [
        {key: _clean(value) for key, value in row.items()}
        for row in rows
    ]


def _month_start(reporting_month: str) -> str:
    """Validate ``YYYY-MM`` and return the first day of that month."""
    from datetime import date

    try:
        parsed = date.fromisoformat(f"{reporting_month}-01")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Reporting month must use YYYY-MM format, got {reporting_month!r}"
        ) from exc

    if parsed.strftime("%Y-%m") != reporting_month:
        raise ValueError(
            f"Reporting month must use YYYY-MM format, got {reporting_month!r}"
        )

    return parsed.isoformat()


# ---------------------------------------------------------------------------
# The reporting months
# ---------------------------------------------------------------------------


def available_months() -> list[str]:
    """Every reporting month that has a frozen population, newest first."""
    rows = _rows(
        """
        SELECT reporting_month
        FROM reporting_periods
        ORDER BY reporting_month DESC
        """,
        (),
    )

    return [row["reporting_month"].strftime("%Y-%m") for row in rows]


# ---------------------------------------------------------------------------
# Page 1 - Department submission overview
# ---------------------------------------------------------------------------


def department_overview(reporting_month: str) -> list[dict]:
    """One row per department: expected, submitted and accepted lines."""
    month = _month_start(reporting_month)

    return _clean_rows(_rows(
        """
        SELECT *
        FROM vw_pbi_department_submission_overview
        WHERE reporting_month = %s
        ORDER BY department_name
        """,
        (month,),
    ))


# ---------------------------------------------------------------------------
# Page 2 - Validation exceptions
# ---------------------------------------------------------------------------


def validation_exceptions(reporting_month: str) -> list[dict]:
    """Every line level exception of the month."""
    month = _month_start(reporting_month)

    return _clean_rows(_rows(
        """
        SELECT *
        FROM vw_pbi_validation_exceptions
        WHERE reporting_month = %s
        ORDER BY department_name, po_number, po_line
        """,
        (month,),
    ))


# ---------------------------------------------------------------------------
# Page 3 - Submission audit trail
# ---------------------------------------------------------------------------


def submission_audit(reporting_month: str) -> list[dict]:
    """One row per submission version, with the email evidence chain."""
    month = _month_start(reporting_month)

    return _clean_rows(_rows(
        """
        SELECT *
        FROM vw_pbi_submission_audit_history
        WHERE reporting_month = %s
        ORDER BY submitted_at DESC NULLS LAST
        """,
        (month,),
    ))


# ---------------------------------------------------------------------------
# Page 4 - Completion trends
# ---------------------------------------------------------------------------


def completion_trends(reporting_month: str) -> list[dict]:
    """The PO lines of the month, with their completion state."""
    month = _month_start(reporting_month)

    return _clean_rows(_rows(
        """
        SELECT *
        FROM vw_pbi_completion_trends
        WHERE reporting_month = %s
        ORDER BY department_name, po_number, po_line
        """,
        (month,),
    ))


def completion_trend_series() -> list[dict]:
    """The month by month acceptance series, across every month.

    This is the one query that is not bound to a single month, because a
    trend needs more than one point to be a trend.
    """
    return _clean_rows(_rows(
        """
        SELECT
            reporting_month,
            COUNT(*)::INTEGER                                AS expected_line_count,
            COUNT(trend_pct)::INTEGER                        AS accepted_line_count,
            ROUND(
                100.0 * COUNT(trend_pct) / NULLIF(COUNT(*), 0),
                2
            )                                                AS acceptance_rate_pct,
            ROUND(AVG(trend_pct) * 100, 2)                   AS avg_completion_pct,
            SUM(po_line_amount)                             AS total_amount
        FROM vw_pbi_completion_trends
        GROUP BY reporting_month
        ORDER BY reporting_month
        """,
        (),
    ))


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def reporting_freshness(reporting_month: str) -> list[dict]:
    """When the Master Excel of the month was last published."""
    month = _month_start(reporting_month)

    return _clean_rows(_rows(
        """
        SELECT *
        FROM vw_pbi_reporting_freshness
        WHERE reporting_month = %s
        """,
        (month,),
    ))


# ---------------------------------------------------------------------------
# Page 6 - The population scope
# ---------------------------------------------------------------------------


def population_scope(reporting_month: str) -> list[dict]:
    """Every department, flagged as INCLUDED or EXCLUDED."""
    month = _month_start(reporting_month)

    return _clean_rows(_rows(
        """
        SELECT *
        FROM vw_pbi_population_scope
        WHERE reporting_month = %s
        ORDER BY population_status DESC, department_name
        """,
        (month,),
    ))


def population_exclusions(reporting_month: str) -> list[dict]:
    """The excluded departments, with the reason."""
    month = _month_start(reporting_month)

    return _clean_rows(_rows(
        """
        SELECT *
        FROM vw_pbi_population_exclusions
        WHERE reporting_month = %s
        ORDER BY department_name
        """,
        (month,),
    ))


def population_status_exclusions(reporting_month: str) -> list[dict]:
    """The PO lines that were filtered out by their ERP status."""
    month = _month_start(reporting_month)

    return _clean_rows(_rows(
        """
        SELECT *
        FROM vw_pbi_population_status_exclusions
        WHERE reporting_month = %s
        ORDER BY department_name, po_number, po_line
        """,
        (month,),
    ))


def population_closed_lines(reporting_month: str) -> list[dict]:
    """The PO lines that closed during the month."""
    month = _month_start(reporting_month)

    return _clean_rows(_rows(
        """
        SELECT *
        FROM vw_pbi_population_closed_lines
        WHERE reporting_month = %s
        ORDER BY department_name, po_number, po_line
        """,
        (month,),
    ))
