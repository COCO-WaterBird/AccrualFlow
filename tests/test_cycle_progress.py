"""
Tests for the monthly collection cycle.

Two things are tested here:

* **The progress ledger.**  Every stage of the cycle writes a row, and
  the dashboard reads those rows to say where the month stands.  A
  stage that never ran must not look like a stage that succeeded.
* **The manual trigger.**  The owner starts one run per reporting month.
  The month has to be validated before a run is created, because a
  run for the wrong month would write to the wrong period.

The tests run against a real PostgreSQL database and are skipped when
none is reachable.
"""

import pytest

from backend.app.services import cycle_progress_service as progress
from backend.app.services import cycle_trigger_service as trigger
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

# The tests that write stage rows use a throwaway month and wipe it
# afterwards.  Writing into the live month would leave fake stages on the
# live cycle ledger, and the dashboard would then report a failed cycle
# for a month that never ran one.
TEST_MONTH = "2099-07"


@pytest.fixture
def clean_test_month():
    """A throwaway reporting month, empty before and after the test.

    The stage rows reference ``reporting_periods``, so the throwaway
    period has to exist.  It is removed again with the rows.
    """
    month = f"{TEST_MONTH}-01"

    def _wipe():
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM cycle_stage_runs WHERE reporting_month = %s",
                    (month,),
                )
                cur.execute(
                    "DELETE FROM reporting_periods WHERE reporting_month = %s",
                    (month,),
                )

    _wipe()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reporting_periods (reporting_month, entity)
                VALUES (%s, '2000')
                ON CONFLICT (reporting_month) DO NOTHING
                """,
                (month,),
            )
    yield TEST_MONTH
    _wipe()


# ---------------------------------------------------------------------------
# The stage vocabulary
# ---------------------------------------------------------------------------


def test_the_stage_list_is_the_fixed_vocabulary_of_the_cycle():
    """The dashboard and the ledger must agree on the stages."""
    assert [name for name, _ in progress.CYCLE_STAGES] == [
        "ERP_IMPORT",
        "FREEZE",
        "DISPATCH",
        "DISCOVER",
        "PARSE",
        "CORRECTION",
        "STATUS",
        "MASTER_EXCEL",
        "DATA_READY",
    ]


def test_every_stage_has_a_human_label():
    for name, label in progress.CYCLE_STAGES:
        assert label, f"{name} has no label"
        assert label != name, f"{name} shows its own name as the label"


# ---------------------------------------------------------------------------
# The month validation
# ---------------------------------------------------------------------------


def test_a_malformed_month_is_rejected():
    with pytest.raises(trigger.TriggerError):
        trigger.validate_month("2026-13")


def test_a_day_is_rejected_as_a_month():
    with pytest.raises(trigger.TriggerError):
        trigger.validate_month("2026-11-01")


def test_a_month_without_a_padding_zero_is_rejected():
    with pytest.raises(trigger.TriggerError):
        trigger.validate_month("2026-1")


def test_an_empty_month_is_rejected():
    with pytest.raises(trigger.TriggerError):
        trigger.validate_month("   ")


def test_a_valid_month_is_accepted():
    assert trigger.validate_month("2026-11") == "2026-11"


# ---------------------------------------------------------------------------
# The next month
# ---------------------------------------------------------------------------


def test_the_next_month_follows_the_reference():
    from datetime import datetime

    assert trigger.next_month(datetime(2026, 11, 15)) == "2026-12"
    assert trigger.next_month(datetime(2026, 12, 31)) == "2027-01"
    assert trigger.next_month(datetime(2026, 1, 5)) == "2026-02"


# ---------------------------------------------------------------------------
# The progress ledger
# ---------------------------------------------------------------------------


def test_the_progress_of_a_month_always_lists_every_stage():
    """A stage that never ran has no row, but it must still be listed.

    Otherwise the dashboard would silently hide the stages that have
    not been reached.
    """
    result = progress.cycle_progress(LIVE_MONTH)

    assert len(result["stages"]) == len(progress.CYCLE_STAGES)
    assert result["total_stage_count"] == len(progress.CYCLE_STAGES)


def test_a_stage_that_never_ran_is_not_reported_as_successful():
    result = progress.cycle_progress("1999-01")

    assert result["overall_status"] == "NOT_STARTED"
    assert result["completed_stage_count"] == 0
    assert result["current_stage"] == "ERP_IMPORT"

    for stage in result["stages"]:
        assert stage["status"] == "NOT_STARTED"


def test_a_finished_stage_is_recorded_and_counted(clean_test_month):
    month = clean_test_month
    progress.start_stage(month, "ERP_IMPORT", dag_run_id="test_run")
    progress.finish_stage(
        month,
        "ERP_IMPORT",
        detail={"row_count": 500},
    )

    result = progress.cycle_progress(month)

    assert result["overall_status"] == "IN_PROGRESS"
    assert result["completed_stage_count"] >= 1

    stage = next(
        s for s in result["stages"] if s["stage"] == "ERP_IMPORT"
    )

    assert stage["status"] == "SUCCESS"
    assert stage["detail"]["row_count"] == 500
    assert stage["started_at"] is not None
    assert stage["finished_at"] is not None


def test_a_failed_stage_is_recorded_with_its_error(clean_test_month):
    month = clean_test_month
    progress.start_stage(month, "FREEZE", dag_run_id="test_run")
    progress.finish_stage(
        month,
        "FREEZE",
        status="FAILED",
        error_message="The ERP returned no PO records",
    )

    result = progress.cycle_progress(month)

    assert result["overall_status"] == "FAILED"
    assert result["failed_stage_count"] >= 1

    stage = next(s for s in result["stages"] if s["stage"] == "FREEZE")

    assert stage["status"] == "FAILED"
    assert "no PO records" in stage["error_message"]


def test_a_retried_stage_replaces_its_row(clean_test_month):
    """A retry must not leave two rows for the same stage."""
    month = clean_test_month
    progress.start_stage(month, "DISPATCH", dag_run_id="first")
    progress.finish_stage(month, "DISPATCH", detail={"request_count": 3})

    progress.start_stage(month, "DISPATCH", dag_run_id="second")

    runs = progress.stage_runs(month)

    assert runs["DISPATCH"]["dag_run_id"] == "second"
    assert runs["DISPATCH"]["status"] == "RUNNING"


def test_the_current_stage_is_the_first_one_that_has_not_succeeded(clean_test_month):
    month = clean_test_month
    progress.start_stage(month, "ERP_IMPORT")
    progress.finish_stage(month, "ERP_IMPORT")

    result = progress.cycle_progress(month)

    assert result["current_stage"] == "FREEZE"


# ---------------------------------------------------------------------------
# The evidence
# ---------------------------------------------------------------------------


def test_the_email_evidence_reports_the_counters():
    evidence = progress.email_evidence(LIVE_MONTH)

    assert evidence["reporting_month"] == LIVE_MONTH
    assert evidence["request_count"] >= 0
    assert evidence["sent_count"] >= 0
    assert evidence["reply_count"] >= 0


def test_the_erp_import_evidence_reports_the_batches():
    imports = progress.erp_import_evidence(LIVE_MONTH)

    assert isinstance(imports, list)

    for row in imports:
        assert "status" in row
        assert "total_rows" in row


def test_the_master_export_evidence_reports_the_exports():
    exports = progress.master_export_evidence(LIVE_MONTH)

    assert isinstance(exports, list)
