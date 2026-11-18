"""
The progress ledger of the monthly collection cycle.

The owner starts one run per reporting month.  The run walks through a
fixed sequence of stages, and the dashboard shows where the cycle
currently stands.

Every stage writes one row into ``cycle_stage_runs``:

    ERP_IMPORT -> FREEZE -> DISPATCH -> DISCOVER
               -> PARSE -> CORRECTION -> STATUS
               -> MASTER_EXCEL -> DATA_READY

The row records when the stage started, when it finished, whether it
succeeded and what it produced.  The dashboard reads the rows and turns
them into a progress view, so the owner can see at a glance whether the
ERP download happened, whether the data was tidied up, whether the
emails went out and how far the replies got.

A stage that never ran has no row.  That is the honest answer: the
stage was not reached, not that it succeeded.
"""

from __future__ import annotations

import re
from datetime import date

from backend.app.services.submission_persistence_service import connect


# ---------------------------------------------------------------------------
# The stages of the cycle, in the order they run.
# ---------------------------------------------------------------------------

STAGE_ERP_IMPORT = "ERP_IMPORT"
STAGE_FREEZE = "FREEZE"
STAGE_DISPATCH = "DISPATCH"
STAGE_DISCOVER = "DISCOVER"
STAGE_PARSE = "PARSE"
STAGE_CORRECTION = "CORRECTION"
STAGE_STATUS = "STATUS"
STAGE_MASTER_EXCEL = "MASTER_EXCEL"
STAGE_DATA_READY = "DATA_READY"

# The fixed vocabulary of the progress view.  The order is the order of
# the cycle.
CYCLE_STAGES: tuple[tuple[str, str], ...] = (
    (STAGE_ERP_IMPORT, "Download from ERP"),
    (STAGE_FREEZE, "Tidy the data and freeze it"),
    (STAGE_DISPATCH, "Send the collection requests"),
    (STAGE_DISCOVER, "Find the replies"),
    (STAGE_PARSE, "Validate the replies"),
    (STAGE_CORRECTION, "Prepare the corrections"),
    (STAGE_STATUS, "Update the department status"),
    (STAGE_MASTER_EXCEL, "Publish the Master Excel"),
    (STAGE_DATA_READY, "Mark the month ready"),
)

# The stages that come after the reply has been collected.  They are the
# ones that only make sense once the departments have answered.
COLLECTION_STAGES: tuple[str, ...] = (
    STAGE_DISCOVER,
    STAGE_PARSE,
)

# The status values of a stage.
STATUS_RUNNING = "RUNNING"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"


# ---------------------------------------------------------------------------
# Writing a stage
# ---------------------------------------------------------------------------


def _month_start(reporting_month: str) -> str:
    """Normalise a reporting month into the first day of that month.

    Both ``YYYY-MM`` and ``YYYY-MM-DD`` are accepted, so that the
    function is safe to call on a value it already normalised.
    """
    value = (reporting_month or "").strip()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed = date.fromisoformat(value)
    else:
        parsed = date.fromisoformat(f"{value}-01")

    if parsed.strftime("%Y-%m") != value[:7]:
        raise ValueError(
            f"Reporting month must use YYYY-MM format, got {reporting_month!r}"
        )

    return parsed.isoformat()


def start_stage(
    reporting_month: str,
    stage: str,
    dag_run_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Record that a stage has started.

    A retried stage replaces its row, so the progress view always shows
    the current attempt rather than a stale one.
    """
    month = _month_start(reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cycle_stage_runs (
                    reporting_month, stage, dag_run_id,
                    status, detail, started_at, finished_at
                )
                VALUES (%s, %s, %s, 'RUNNING', %s::jsonb, NOW(), NULL)
                ON CONFLICT (reporting_month, stage)
                DO UPDATE SET
                    dag_run_id = EXCLUDED.dag_run_id,
                    status = 'RUNNING',
                    detail = EXCLUDED.detail,
                    error_message = NULL,
                    started_at = NOW(),
                    finished_at = NULL
                """,
                (month, stage, dag_run_id, _dump_json(detail or {})),
            )


def finish_stage(
    reporting_month: str,
    stage: str,
    status: str = STATUS_SUCCESS,
    detail: dict | None = None,
    error_message: str | None = None,
) -> None:
    """Record that a stage has finished.

    A stage that failed keeps its error message, so the dashboard can
    show what went wrong instead of only that something did.
    """
    month = _month_start(reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE cycle_stage_runs
                SET status = %s,
                    detail = detail || %s::jsonb,
                    error_message = %s,
                    finished_at = NOW()
                WHERE reporting_month = %s
                  AND stage = %s
                """,
                (
                    status,
                    _dump_json(detail or {}),
                    error_message,
                    month,
                    stage,
                ),
            )


def _dump_json(value: dict) -> str:
    """Serialise a detail dictionary for a JSONB column."""
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Reading the progress
# ---------------------------------------------------------------------------


def stage_runs(reporting_month: str) -> dict[str, dict]:
    """The stage rows of a reporting month, keyed by the stage name."""
    month = _month_start(reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    stage,
                    status,
                    detail,
                    error_message,
                    dag_run_id,
                    started_at,
                    finished_at
                FROM cycle_stage_runs
                WHERE reporting_month = %s
                """,
                (month,),
            )
            rows = list(cur.fetchall())

    return {row["stage"]: dict(row) for row in rows}


def cycle_progress(reporting_month: str) -> dict:
    """The progress of the monthly cycle of one reporting month.

    The result carries the fixed stage list with the state of each
    stage, so the dashboard never has to work out what comes next.
    """
    month = _month_start(reporting_month)
    runs = stage_runs(month)

    stages = []

    for name, label in CYCLE_STAGES:
        row = runs.get(name)

        if row is None:
            stages.append({
                "stage": name,
                "label": label,
                "status": "NOT_STARTED",
                "detail": {},
                "error_message": None,
                "started_at": None,
                "finished_at": None,
            })
            continue

        detail = row.get("detail") or {}

        if isinstance(detail, str):
            import json

            try:
                detail = json.loads(detail)
            except (TypeError, ValueError):
                detail = {}

        stages.append({
            "stage": name,
            "label": label,
            "status": row["status"],
            "detail": detail,
            "error_message": row.get("error_message"),
            "dag_run_id": row.get("dag_run_id"),
            "started_at": _iso(row.get("started_at")),
            "finished_at": _iso(row.get("finished_at")),
        })

    done = [s for s in stages if s["status"] == "SUCCESS"]
    failed = [s for s in stages if s["status"] == "FAILED"]
    running = [s for s in stages if s["status"] == "RUNNING"]

    # The current stage is the first one that has not succeeded yet.
    current = None
    for stage in stages:
        if stage["status"] != "SUCCESS":
            current = stage
            break

    if failed:
        overall = "FAILED"
    elif running:
        overall = "RUNNING"
    elif len(done) == len(stages):
        overall = "COMPLETE"
    elif done:
        overall = "IN_PROGRESS"
    else:
        overall = "NOT_STARTED"

    return {
        "reporting_month": reporting_month,
        "overall_status": overall,
        "completed_stage_count": len(done),
        "total_stage_count": len(stages),
        "current_stage": current["stage"] if current else None,
        "current_stage_label": current["label"] if current else None,
        "failed_stage_count": len(failed),
        "stages": stages,
    }


def _iso(value):
    if value is None:
        return None

    if hasattr(value, "isoformat"):
        return value.isoformat()

    return str(value)


# ---------------------------------------------------------------------------
# The evidence that the pipeline itself already wrote
# ---------------------------------------------------------------------------


def email_evidence(reporting_month: str) -> dict:
    """What the email ledger says about the month.

    This is the raw evidence, not the progress row.  It is what proves
    that requests were sent and that replies came back, independently of
    what the cycle claims.
    """
    month = _month_start(reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    status,
                    COUNT(*)::INTEGER AS request_count,
                    COUNT(gmail_message_id)::INTEGER AS sent_count
                FROM email_requests
                WHERE reporting_month = %s
                GROUP BY status
                """,
                (month,),
            )
            request_rows = list(cur.fetchall())

            cur.execute(
                """
                SELECT
                    processing_status,
                    COUNT(*)::INTEGER AS message_count
                FROM gmail_message_processing
                WHERE reporting_month = %s
                GROUP BY processing_status
                """,
                (month,),
            )
            reply_rows = list(cur.fetchall())

            cur.execute(
                """
                SELECT
                    status,
                    COUNT(*)::INTEGER AS correction_count
                FROM correction_requests
                WHERE reporting_month = %s
                GROUP BY status
                """,
                (month,),
            )
            correction_rows = list(cur.fetchall())

            cur.execute(
                """
                SELECT
                    COUNT(*)::INTEGER AS submission_count,
                    COUNT(*) FILTER (
                        WHERE processing_status = 'PERSISTED'
                    )::INTEGER AS persisted_count
                FROM department_submissions
                WHERE reporting_month = %s
                """,
                (month,),
            )
            submission_row = cur.fetchone() or {}

    requests_by_status = {
        row["status"]: {
            "request_count": row["request_count"],
            "sent_count": row["sent_count"],
        }
        for row in request_rows
    }

    replies_by_status = {
        row["processing_status"]: row["message_count"]
        for row in reply_rows
    }

    corrections_by_status = {
        row["status"]: row["correction_count"]
        for row in correction_rows
    }

    total_requests = sum(
        item["request_count"] for item in requests_by_status.values()
    )
    sent_requests = sum(
        item["sent_count"] for item in requests_by_status.values()
    )

    return {
        "reporting_month": reporting_month,
        "request_count": total_requests,
        "sent_count": sent_requests,
        "requests_by_status": requests_by_status,
        "replies_by_status": replies_by_status,
        "reply_count": sum(replies_by_status.values()),
        "corrections_by_status": corrections_by_status,
        "correction_count": sum(corrections_by_status.values()),
        "submission_count": submission_row.get("submission_count", 0),
        "persisted_count": submission_row.get("persisted_count", 0),
    }


def erp_import_evidence(reporting_month: str) -> list[dict]:
    """The ERP import batches of a month, newest first."""
    month = _month_start(reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    batch_id,
                    reporting_month,
                    source_system,
                    source_filename,
                    imported_at,
                    completed_at,
                    status,
                    total_rows,
                    imported_rows,
                    error_message
                FROM import_batch
                WHERE reporting_month = %s
                ORDER BY imported_at DESC
                """,
                (month,),
            )
            rows = list(cur.fetchall())

    return [
        {
            **dict(row),
            "imported_at": _iso(row.get("imported_at")),
            "completed_at": _iso(row.get("completed_at")),
        }
        for row in rows
    ]


def master_export_evidence(reporting_month: str) -> list[dict]:
    """The Master Excel exports of a month, newest first."""
    month = _month_start(reporting_month)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    export_id,
                    file_name,
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
                LIMIT 10
                """,
                (month,),
            )
            rows = list(cur.fetchall())

    return [
        {
            **dict(row),
            "created_at": _iso(row.get("created_at")),
        }
        for row in rows
    ]
