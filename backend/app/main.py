"""
Read-only FastAPI endpoints for status, submissions, exceptions, archive
metadata and export history.

The API is intentionally read only.  It exposes no write operation and it
never returns a raw filesystem path that could be used to traverse the
host.  The archive metadata is exposed through a dedicated download route
that resolves the path against the configured data root.
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from backend.app.core.config import (
    DATA_DIR,
    REPORTING_TIMEZONE,
)
from backend.app.services.department_status_service import (
    list_department_statuses,
)
from backend.app.services.submission_persistence_service import (
    _month_start,
    connect,
)


app = FastAPI(
    title="AccrualFlow AI",
    description="CapEx Accrual Automation API",
    version="0.2.0",
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