"""
Tests for the safety of running the monitoring DAG.

A DAG run is **not** side-effect free: it writes to the database, records
email requests, publishes workbooks and — when ``ACCRUALFLOW_DRY_RUN`` is
off — sends email.

The reporting month therefore has to be pinnable, so that a test run can
never touch the live reporting period by accident.

These tests parse the DAG source rather than importing it, because
importing the DAG requires the Airflow SDK.
"""

import ast
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAG_FILE = PROJECT_ROOT / "airflow" / "dags" / "accrualflow_daily_monitoring.py"


def _dag_source() -> str:
    return DAG_FILE.read_text(encoding="utf-8")


def _extract_reporting_month():
    """Compile the ``_reporting_month`` helper out of the DAG source.

    The function is executed on its own so that the behaviour can be
    tested without importing the Airflow DAG.
    """
    tree = ast.parse(_dag_source())

    function = None

    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if node.name == "_reporting_month":
                function = node

    assert function is not None, "_reporting_month is missing from the DAG"

    module = ast.Module(
        body=[function],
        type_ignores=[],
    )

    import os
    import re

    namespace: dict = {
        "os": os,
        "re": re,
        "datetime": datetime,
        "BUSINESS_TIMEZONE": ZoneInfo("America/Los_Angeles"),
        "REPORTING_MONTH_OVERRIDE_ENV": "ACCRUALFLOW_REPORTING_MONTH",
    }

    exec(compile(module, str(DAG_FILE), "exec"), namespace)

    return namespace["_reporting_month"]


# ---------------------------------------------------------------------------
# The reporting month
# ---------------------------------------------------------------------------


def test_the_reporting_month_comes_from_the_logical_date():
    _reporting_month = _extract_reporting_month()

    logical_date = datetime(2026, 11, 17, 9, 0, 0)

    assert _reporting_month(logical_date) == "2026-11"


def test_the_override_pins_the_reporting_month(monkeypatch):
    """A test run must be able to target a throwaway month."""
    _reporting_month = _extract_reporting_month()

    monkeypatch.setenv("ACCRUALFLOW_REPORTING_MONTH", "2098-01")

    logical_date = datetime(2026, 11, 17, 9, 0, 0)

    assert _reporting_month(logical_date) == "2098-01"


def test_without_the_override_the_logical_date_is_used(monkeypatch):
    _reporting_month = _extract_reporting_month()

    monkeypatch.delenv("ACCRUALFLOW_REPORTING_MONTH", raising=False)

    logical_date = datetime(2026, 11, 17, 9, 0, 0)

    assert _reporting_month(logical_date) == "2026-11"


def test_an_empty_override_is_ignored(monkeypatch):
    _reporting_month = _extract_reporting_month()

    monkeypatch.setenv("ACCRUALFLOW_REPORTING_MONTH", "   ")

    logical_date = datetime(2026, 11, 17, 9, 0, 0)

    assert _reporting_month(logical_date) == "2026-11"


def test_the_override_must_be_a_single_month(monkeypatch):
    """A malformed override must not silently select the wrong month."""
    _reporting_month = _extract_reporting_month()

    monkeypatch.setenv("ACCRUALFLOW_REPORTING_MONTH", "not-a-month")

    with pytest.raises(Exception):
        _reporting_month(datetime(2026, 11, 17))


# ---------------------------------------------------------------------------
# The dry run switch
# ---------------------------------------------------------------------------


def test_the_dag_documents_that_a_run_is_not_side_effect_free():
    source = _dag_source()

    assert "not** side-effect free" in source or (
        "not** side-effect free" in source.replace("**", "**")
    )


def test_the_dag_documents_the_reporting_month_override():
    source = _dag_source()

    assert "ACCRUALFLOW_REPORTING_MONTH" in source
