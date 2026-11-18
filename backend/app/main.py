"""
The AccrualFlow FastAPI application.

Most endpoints are read only.  They expose no write operation and they
never return a raw filesystem path that could be used to traverse the
host.  The archive metadata is exposed through a dedicated download route
that resolves the path against the configured data root.

There is exactly one write path: the manual trigger of the monthly
collection cycle.  The owner starts one run per reporting month, and
the trigger does nothing but ask Airflow to start it.  The pipeline
itself still lives in the DAG, where every task is retryable.
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.app.core.config import (
    DATA_DIR,
    REPORTING_TIMEZONE,
)
from backend.app.services.cycle_progress_service import (
    cycle_progress,
    email_evidence,
    erp_import_evidence,
    master_export_evidence,
)
from backend.app.services.cycle_trigger_service import (
    TriggerError,
    active_run,
    list_runs,
    next_month,
    trigger_month,
)
from backend.app.services.department_status_service import (
    list_department_statuses,
)
from backend.app.services.dashboard_service import (
    available_months,
    completion_trend_series,
    completion_trends,
    default_month,
    department_overview,
    is_test_period,
    population_closed_lines,
    population_exclusions,
    population_scope,
    population_status_exclusions,
    reporting_freshness,
    submission_audit,
    validation_exceptions,
)
from backend.app.services.submission_persistence_service import (
    _month_start,
    connect,
)


app = FastAPI(
    title="AccrualFlow AI",
    description="CapEx Accrual Automation API",
    version="0.3.0",
)

# The web dashboard.  It reads the same reporting views that Power BI
# reads, so the two can never disagree.
app.mount(
    "/dashboard",
    StaticFiles(directory=Path(__file__).parent / "static", html=True),
    name="dashboard",
)


@app.get("/")
def root():
    return {
        "application": "AccrualFlow AI",
        "status": "running",
        "reporting_timezone": REPORTING_TIMEZONE,
    }


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "accrualflow-backend",
    }


@app.get("/api/v1/purchase-orders")
def get_purchase_orders(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Retrieve purchase orders through the Mock ERP API."""
    import httpx

    from backend.app.services.mock_erp_client import MockERPClient

    try:
        client = MockERPClient()

        return client.get_purchase_orders(
            limit=limit,
            offset=offset,
        )

    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503,
            detail="Mock ERP service unavailable",
        ) from exc

    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail="Mock ERP returned an error",
        ) from exc


# ---------------------------------------------------------------------------
# Reporting endpoints
# ---------------------------------------------------------------------------


@app.get("/api/v1/departments/status")
def get_department_statuses(
    reporting_month: str = Query(..., description="YYYY-MM"),
    legal_entity: str | None = Query(default=None),
):
    """Return the current status of every department of a reporting month."""
    try:
        _month_start(reporting_month)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Reporting month must use YYYY-MM format",
        ) from exc

    try:
        return {
            "reporting_month": reporting_month,
            "departments": list_department_statuses(
                reporting_month, legal_entity
            ),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="The reporting database is unavailable",
        ) from exc


@app.get("/api/v1/submissions")
def get_submissions(
    reporting_month: str = Query(..., description="YYYY-MM"),
    legal_entity: str | None = Query(default=None),
    department_id: str | None = Query(default=None),
):
    """Return the submission history of a reporting month."""
    try:
        month = _month_start(reporting_month)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Reporting month must use YYYY-MM format",
        ) from exc

    clauses = ["ds.reporting_month = %s"]
    params: list = [month]

    if legal_entity:
        clauses.append("ds.legal_entity = %s")
        params.append(legal_entity)

    if department_id:
        clauses.append("ds.department_id = %s")
        params.append(department_id)

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        ds.submission_id,
                        ds.reporting_month,
                        ds.legal_entity,
                        ds.department_id,
                        ds.department_name,
                        ds.version,
                        ds.processing_status,
                        ds.gmail_message_id,
                        ds.gmail_thread_id,
                        ds.submitted_by,
                        ds.submitted_at,
                        ds.source_attachment_path,
                        ds.error_message
                    FROM department_submissions ds
                    WHERE {' AND '.join(clauses)}
                    ORDER BY
                        ds.legal_entity, ds.department_id, ds.version
                    """,
                    params,
                )
                return {
                    "reporting_month": reporting_month,
                    "submissions": list(cur.fetchall()),
                }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="The reporting database is unavailable",
        ) from exc


@app.get("/api/v1/exceptions")
def get_exceptions(
    reporting_month: str = Query(..., description="YYYY-MM"),
    legal_entity: str | None = Query(default=None),
    department_id: str | None = Query(default=None),
    open_only: bool = Query(default=True),
):
    """Return the validation exceptions of a reporting month."""
    try:
        month = _month_start(reporting_month)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Reporting month must use YYYY-MM format",
        ) from exc

    clauses = ["vi.reporting_month = %s"]
    params: list = [month]

    if legal_entity:
        clauses.append("vi.legal_entity = %s")
        params.append(legal_entity)

    if department_id:
        clauses.append("vi.department_id = %s")
        params.append(department_id)

    if open_only:
        clauses.append("vi.status = 'OPEN'")

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        vi.issue_id,
                        vi.reporting_month,
                        vi.legal_entity,
                        vi.department_id,
                        vi.po_number,
                        vi.po_line,
                        vi.exception_code,
                        vi.severity,
                        vi.detail,
                        vi.status,
                        vi.created_at
                    FROM validation_issues vi
                    WHERE {' AND '.join(clauses)}
                    ORDER BY
                        vi.legal_entity, vi.department_id,
                        vi.po_number, vi.po_line
                    """,
                    params,
                )
                return {
                    "reporting_month": reporting_month,
                    "exceptions": list(cur.fetchall()),
                }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="The reporting database is unavailable",
        ) from exc


@app.get("/api/v1/archives")
def get_archives(
    reporting_month: str = Query(..., description="YYYY-MM"),
):
    """Return the archive metadata of a reporting month."""
    try:
        month = _month_start(reporting_month)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Reporting month must use YYYY-MM format",
        ) from exc

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        ea.archive_id,
                        ea.gmail_message_id,
                        ea.gmail_thread_id,
                        ea.subject,
                        ea.sender_address,
                        ea.recipient_address,
                        ea.message_date,
                        ea.eml_path,
                        ea.eml_sha256,
                        ea.eml_size_bytes,
                        ea.archived_at,
                        (
                            SELECT COUNT(*)
                            FROM email_attachments att
                            WHERE att.archive_id = ea.archive_id
                        )::INTEGER AS attachment_count
                    FROM email_archives ea
                    WHERE ea.reporting_month = %s
                      AND ea.direction = 'INBOUND'
                    ORDER BY ea.archived_at
                    """,
                    (month,),
                )
                return {
                    "reporting_month": reporting_month,
                    "archives": list(cur.fetchall()),
                }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="The reporting database is unavailable",
        ) from exc


@app.get("/api/v1/exports")
def get_exports(
    reporting_month: str = Query(..., description="YYYY-MM"),
):
    """Return the Master Excel export history of a reporting month."""
    try:
        month = _month_start(reporting_month)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Reporting month must use YYYY-MM format",
        ) from exc

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        export_id,
                        reporting_month,
                        file_name,
                        file_sha256,
                        file_size_bytes,
                        expected_line_count,
                        accepted_line_count,
                        exception_count,
                        department_count,
                        status,
                        error_message,
                        created_at
                    FROM master_export_runs
                    WHERE reporting_month = %s
                    ORDER BY created_at DESC
                    """,
                    (month,),
                )
                return {
                    "reporting_month": reporting_month,
                    "exports": list(cur.fetchall()),
                }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="The reporting database is unavailable",
        ) from exc


# ---------------------------------------------------------------------------
# The web dashboard
# ---------------------------------------------------------------------------


def _dashboard(query, reader):
    """Run one reporting query and return its rows.

    The dashboard is read only.  A bad reporting month is a client error;
    an unreachable database is a service error.
    """
    try:
        return reader(query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="The reporting database is unavailable",
        ) from exc


@app.get("/api/v1/dashboard/months")
def get_dashboard_months():
    """Every reporting month that has a frozen population, newest first.

    ``default`` is the period the dashboard opens on.  It is the live
    period, never a test period, even though a test period in a far
    future year sorts first.
    """
    try:
        months = available_months()
        return {
            "months": months,
            "default": default_month(),
            "test_periods": [m for m in months if is_test_period(m)],
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="The reporting database is unavailable",
        ) from exc


@app.get("/api/v1/dashboard/department_overview")
def get_dashboard_overview(
    reporting_month: str = Query(..., description="YYYY-MM"),
):
    """Page 1 - one row per department with its line counts."""
    return _dashboard(
        reporting_month,
        lambda m: department_overview(m),
    )


@app.get("/api/v1/dashboard/validation_exceptions")
def get_dashboard_exceptions(
    reporting_month: str = Query(..., description="YYYY-MM"),
):
    """Page 2 - the line level validation exceptions."""
    return _dashboard(
        reporting_month,
        lambda m: validation_exceptions(m),
    )


@app.get("/api/v1/dashboard/submission_audit")
def get_dashboard_audit(
    reporting_month: str = Query(..., description="YYYY-MM"),
):
    """Page 3 - the submission versions with their email evidence."""
    return _dashboard(
        reporting_month,
        lambda m: submission_audit(m),
    )


@app.get("/api/v1/dashboard/completion_trends")
def get_dashboard_trends(
    reporting_month: str = Query(..., description="YYYY-MM"),
):
    """Page 4 - the PO lines of the month with their completion state."""
    return _dashboard(
        reporting_month,
        lambda m: completion_trends(m),
    )


@app.get("/api/v1/dashboard/trend_series")
def get_dashboard_trend_series():
    """Page 4 - the month by month acceptance series."""
    try:
        return completion_trend_series()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="The reporting database is unavailable",
        ) from exc


@app.get("/api/v1/dashboard/freshness")
def get_dashboard_freshness(
    reporting_month: str = Query(..., description="YYYY-MM"),
):
    """When the Master Excel of the month was last published."""
    return _dashboard(
        reporting_month,
        lambda m: reporting_freshness(m),
    )


@app.get("/api/v1/dashboard/population_scope")
def get_dashboard_population_scope(
    reporting_month: str = Query(..., description="YYYY-MM"),
):
    """Page 6 - every department, INCLUDED or EXCLUDED."""
    return _dashboard(
        reporting_month,
        lambda m: population_scope(m),
    )


@app.get("/api/v1/dashboard/population_exclusions")
def get_dashboard_population_exclusions(
    reporting_month: str = Query(..., description="YYYY-MM"),
):
    """Page 6 - the excluded departments and why."""
    return _dashboard(
        reporting_month,
        lambda m: population_exclusions(m),
    )


@app.get("/api/v1/dashboard/population_status_exclusions")
def get_dashboard_status_exclusions(
    reporting_month: str = Query(..., description="YYYY-MM"),
):
    """Page 6 - the PO lines that were filtered out by their status."""
    return _dashboard(
        reporting_month,
        lambda m: population_status_exclusions(m),
    )


@app.get("/api/v1/dashboard/population_closed_lines")
def get_dashboard_closed_lines(
    reporting_month: str = Query(..., description="YYYY-MM"),
):
    """Page 6 - the PO lines that closed during the month."""
    return _dashboard(
        reporting_month,
        lambda m: population_closed_lines(m),
    )


@app.get("/api/v1/archives/{archive_id}/eml")
def download_eml(archive_id: int):
    """Serve an archived raw message.

    The path is resolved against the configured data root so that no
    arbitrary host path can be read through this route.
    """
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT eml_path, eml_sha256
                    FROM email_archives
                    WHERE archive_id = %s
                    """,
                    (archive_id,),
                )
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="The reporting database is unavailable",
        ) from exc

    if row is None:
        raise HTTPException(status_code=404, detail="Archive not found")

    from pathlib import Path

    candidate = Path(row["eml_path"]).resolve()
    data_root = Path(DATA_DIR).resolve()

    if not candidate.is_relative_to(data_root):
        raise HTTPException(
            status_code=403,
            detail="The archive is outside of the managed data directory",
        )

    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Archive file not found")

    return FileResponse(
        candidate,
        media_type="message/rfc822",
        filename=candidate.name,
    )


# ---------------------------------------------------------------------------
# The monthly cycle - progress and the manual trigger
# ---------------------------------------------------------------------------


@app.get("/api/v1/cycle/progress")
def get_cycle_progress(
    reporting_month: str = Query(..., description="YYYY-MM"),
):
    """How far the monthly collection cycle of a reporting month has got.

    The result carries the fixed stage list with the state of each
    stage, so the dashboard never has to work out what comes next.
    """
    try:
        _month_start(reporting_month)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Reporting month must use YYYY-MM format",
        ) from exc

    try:
        return cycle_progress(reporting_month)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="The reporting database is unavailable",
        ) from exc


@app.get("/api/v1/cycle/evidence")
def get_cycle_evidence(
    reporting_month: str = Query(..., description="YYYY-MM"),
):
    """The raw evidence of the month.

    The progress view says where the cycle stands.  This says what the
    pipeline actually wrote: how many ERP rows were imported, how many
    requests went out, how many replies came back and how many
    corrections were prepared.
    """
    try:
        _month_start(reporting_month)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Reporting month must use YYYY-MM format",
        ) from exc

    try:
        return {
            "reporting_month": reporting_month,
            "email": email_evidence(reporting_month),
            "erp_imports": erp_import_evidence(reporting_month),
            "master_exports": master_export_evidence(reporting_month),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="The reporting database is unavailable",
        ) from exc


@app.get("/api/v1/cycle/runs")
def get_cycle_runs(limit: int = Query(default=10, ge=1, le=50)):
    """The most recent runs of the cycle.

    The Airflow API is consulted, so this endpoint reports the runs
    themselves rather than the progress ledger.
    """
    try:
        return {
            "runs": list_runs(limit=limit),
            "active_run": active_run(),
            "suggested_month": next_month(),
        }
    except TriggerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/cycle/trigger")
def post_cycle_trigger(
    reporting_month: str = Query(..., description="YYYY-MM"),
):
    """Start the monthly collection cycle for a reporting month.

    This is the only write endpoint of the API.  It does not run
    anything itself: it asks Airflow to start the DAG run, which then
    pulls the ERP snapshot, freezes the population, asks the
    departments to report and publishes the Master Excel.

    The run is asynchronous.  The dashboard follows the progress
    through ``/api/v1/cycle/progress``.
    """
    try:
        return trigger_month(reporting_month)
    except TriggerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc