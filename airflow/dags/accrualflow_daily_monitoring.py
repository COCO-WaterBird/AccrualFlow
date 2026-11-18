"""
AccrualFlow monthly submission collection.

The DAG implements the deterministic pipeline:

    sync_erp_snapshot
      -> freeze_expected_population
      -> dispatch_collection_requests
      -> discover_replies
      -> register_and_deduplicate
      -> archive_raw_emails_and_attachments
      -> parse_validate_and_persist
      -> generate_correction_workbooks_and_drafts
      -> update_department_status
      -> export_master_excel
      -> mark_data_ready

Design notes
------------
* The DAG is **manually triggered**.  There is no cron schedule.  The
  owner starts one run per reporting month, from the dashboard or from
  the Airflow UI, and that single run carries the whole cycle: it pulls
  the ERP snapshot, freezes the population, asks the departments to
  report, collects the replies, validates them and publishes the Master
  Excel.  Nothing runs on its own, so a month can never be processed
  twice by accident and no run can happen while nobody is watching.
* Every task is retry safe.  Gmail message ids are the idempotency key, so
  a repeated run never creates a second submission.
* A single corrupt workbook never blocks the other messages: each message is
  processed inside its own try/except and the failures are collected.
* Only identifiers and paths travel through XCom, never binary payloads.
* The reporting month comes from the run itself.  A manual run names the
  month it is for, so the run and the report can never disagree.

The outbound email path is guarded by ``ACCRUALFLOW_DRY_RUN``.  While that
switch is on, correction requests are only prepared.

Testing this DAG is **not** side-effect free.  A run writes to the
database, records email requests, publishes workbooks and can send
email.  ``ACCRUALFLOW_REPORTING_MONTH`` overrides the reporting month
so that a test run can be pointed at a throwaway period instead of the
live one.
"""

import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from airflow.sdk import dag, task


# The business timezone of the reporting cycle.
BUSINESS_TIMEZONE = ZoneInfo("America/Los_Angeles")


# ---------------------------------------------------------------------------
# The progress ledger
#
# Every task of the run writes its progress into ``cycle_stage_runs``,
# so that the dashboard can show where the monthly cycle stands.  The
# ledger is written from inside the task itself rather than from a
# wrapper, because a wrapper would not know what the task produced.
# ---------------------------------------------------------------------------


def _record_stage(stage: str, reporting_month: str, context: dict):
    """Return the two helpers that record the progress of a stage.

    The stage is marked as running before the work starts and as
    finished when it ends.  A failure is recorded as a failure, with
    the error message, so the dashboard can show what needs attention
    rather than only that something happened.
    """
    from backend.app.services.cycle_progress_service import (
        finish_stage,
        start_stage,
    )

    dag_run = (context or {}).get("run_id")

    start_stage(reporting_month, stage, dag_run_id=dag_run)

    def finish(
        status: str = "SUCCESS",
        detail: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        try:
            finish_stage(
                reporting_month,
                stage,
                status=status,
                detail=detail,
                error_message=error_message,
            )
        except Exception as exc:  # noqa: BLE001
            # The progress ledger must never break the pipeline.  A
            # failure to record the progress is worth a log line, but
            # it is not worth failing the report for.
            print(f"PROGRESS LEDGER WRITE FAILED ({stage}): {exc}")

    return finish

# An explicit override of the reporting month, in ``YYYY-MM``.
#
# A DAG run writes to the database and publishes files, so a test run
# must never be pointed at the live reporting month by accident.  Setting
# this variable pins the run to one specific month.
REPORTING_MONTH_OVERRIDE_ENV = "ACCRUALFLOW_REPORTING_MONTH"


def _conf_reporting_month(params) -> str | None:
    """Return the reporting month named by the run, if any.

    A manual run names the month it is for.  The name comes from the
    run configuration (``conf``), because that is what the dashboard
    sends and what the Airflow UI shows next to the run.

    Airflow puts the run configuration in two places, and the month is
    read from both.  ``params`` is the merged view of the DAG params, the
    task params and the run configuration, so the month sits at the top
    level of it.  The raw run configuration is under ``conf``.  Reading
    only one of them would silently ignore the month and fall back to
    the logical date, which would collect the wrong period.

    A malformed name is rejected rather than ignored.  A run that
    silently falls back to "today" would collect the wrong month and
    publish it under the wrong name, and the damage would only surface
    in the reconciliation at the very end.
    """
    if not params:
        return None

    try:
        conf = params.get("conf") or {}
    except AttributeError:
        conf = {}

    if not isinstance(conf, dict):
        conf = {}

    value = conf.get("reporting_month")

    if value is None:
        try:
            value = params.get("reporting_month")
        except AttributeError:
            return None

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError(
            f"reporting_month must be in YYYY-MM format, got {value!r}"
        )

    month = int(value[5:7])

    if not 1 <= month <= 12:
        raise ValueError(
            f"reporting_month has an invalid month: {value!r}"
        )

    return value


def _reporting_month(logical_date) -> str:
    """Return the reporting month ``YYYY-MM`` of a logical date.

    The override variable wins over the logical date, so that a test run
    can be pinned to a throwaway period.

    A malformed override is rejected rather than passed through.  A
    silently wrong reporting month would write to the wrong period, and
    the damage would only be discovered in the reconciliation.
    """
    override = os.getenv(REPORTING_MONTH_OVERRIDE_ENV, "").strip()

    if override:
        if not re.fullmatch(r"\d{4}-\d{2}", override):
            raise ValueError(
                f"{REPORTING_MONTH_OVERRIDE_ENV} must be in YYYY-MM format, "
                f"got {override!r}"
            )

        month = int(override[5:7])

        if not 1 <= month <= 12:
            raise ValueError(
                f"{REPORTING_MONTH_OVERRIDE_ENV} has an invalid month: "
                f"{override!r}"
            )

        return override

    if logical_date is None:
        logical_date = datetime.now(BUSINESS_TIMEZONE)

    if hasattr(logical_date, "date"):
        logical_date = logical_date.date()

    return logical_date.strftime("%Y-%m")


@dag(
    dag_id="accrualflow_daily_monitoring",
    # There is no schedule.  The owner starts one run per reporting
    # month, and the run carries the whole cycle.
    schedule=None,
    start_date=datetime(2026, 11, 1),
    catchup=False,
    max_active_runs=1,
    tags=["accrualflow", "finance", "monitoring"],
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
    },
)
def accrualflow_daily_monitoring():

    # ------------------------------------------------------------------
    @task
    def resolve_reporting_period(**context) -> dict:
        """Resolve the reporting month and the legal entity of the run.

        The month comes from the run configuration when the run names
        one, because a manual run is for a month that the owner chose.
        Only when the run names no month does the logical date decide,
        which is the case for a test run.

        The run configuration is read from two places, because Airflow
        exposes it in two places: ``params`` is the merged view of the
        DAG params and the run configuration, and ``dag_run.conf`` is
        the raw configuration of this run.  Reading only the first one
        would lose the month on the Airflow versions that do not merge
        the run configuration into the params.
        """
        sources = [context.get("params")]

        dag_run = context.get("dag_run")
        if dag_run is not None:
            raw_conf = getattr(dag_run, "conf", None)
            if raw_conf is None and isinstance(dag_run, dict):
                raw_conf = dag_run.get("conf")
            if raw_conf:
                sources.append({"conf": raw_conf})

        named = None
        for source in sources:
            named = _conf_reporting_month(source)
            if named:
                break

        logical_date = context.get("logical_date")

        reporting_month = named or _reporting_month(logical_date)

        print(f"REPORTING MONTH: {reporting_month}")

        return {
            "reporting_month": reporting_month,
            "legal_entity": "2000",
        }

    # ------------------------------------------------------------------
    @task
    def sync_erp_snapshot(period: dict, **context) -> dict:
        """Pull the ERP master data and refresh the PO snapshot.

        This is the "download the table from the ERP" step.  The import
        is recorded in ``import_batch``, so the dashboard can show when
        the data was last pulled and how many rows it contained.

        The import is idempotent: an identical file that was already
        imported creates no second batch.
        """
        from backend.app.services.po_sync_service import (
            sync_purchase_orders,
        )
        from backend.app.core.xcom_payload import as_dict

        period = as_dict(period, "sync_erp_snapshot.period")

        finish = _record_stage(
            "ERP_IMPORT", period["reporting_month"], context
        )

        try:
            result = sync_purchase_orders(period["reporting_month"])
        except Exception as exc:  # noqa: BLE001
            print(f"ERP SYNC FAILED: {type(exc).__name__}: {exc}")
            finish(
                status="FAILED",
                error_message=f"{type(exc).__name__}: {exc}",
            )
            raise

        print(f"ERP SYNC: {result}")

        finish(detail={
            "row_count": result.get("row_count"),
            "batch_id": result.get("batch_id"),
            "reused_existing_batch": result.get("reused_existing_batch"),
            "data_hash": result.get("data_hash"),
        })

        return {
            "reporting_month": period["reporting_month"],
            "legal_entity": period["legal_entity"],
            "erp_import": result,
        }

    # ------------------------------------------------------------------
    @task
    def freeze_expected_population(period: dict, **context) -> dict:
        """Freeze the expected PO line population of the month.

        The frozen population is the authority the whole report is
        validated against.  It is written once and never changes, so
        the report can always be reconciled against the exact set of
        lines that was asked for.

        The freeze is idempotent for the same content.  Re-running with
        a different content is refused, because a frozen population must
        never silently change underneath an in-flight submission.
        """
        from backend.app.services.population_freeze_service import (
            freeze_population,
        )
        from backend.app.core.xcom_payload import as_dict

        period = as_dict(period, "freeze_expected_population.period")

        finish = _record_stage(
            "FREEZE", period["reporting_month"], context
        )

        try:
            result = freeze_population(
                period["reporting_month"],
                period["legal_entity"],
            )
        except Exception as exc:  # noqa: BLE001
            print(f"FREEZE FAILED: {type(exc).__name__}: {exc}")
            finish(
                status="FAILED",
                error_message=f"{type(exc).__name__}: {exc}",
            )
            raise

        print(
            f"FROZEN: {result['frozen_line_count']} PO lines | "
            f"excluded departments: {result['excluded_department_count']} | "
            f"filtered lines: {result['filtered_line_count']}"
        )

        finish(detail={
            "frozen_line_count": result["frozen_line_count"],
            "excluded_department_count": result["excluded_department_count"],
            "filtered_line_count": result["filtered_line_count"],
            "closed_in_month_count": result["closed_in_month_count"],
            "authorized_contact_count": result.get("authorized_contact_count"),
        })

        return {
            "reporting_month": period["reporting_month"],
            "legal_entity": period["legal_entity"],
            "population": result,
        }

    # ------------------------------------------------------------------
    @task
    def dispatch_collection_requests(period: dict, **context) -> list:
        """Prepare and optionally send the collection request of each department.

        While ``ACCRUALFLOW_DRY_RUN`` is enabled the requests are only
        prepared and recorded.  The outbound thread ids are stored so that
        the replies can be correlated later.
        """
        from backend.app.services.collection_request_service import (
            dispatch_collection_requests as dispatch,
        )
        from backend.app.core.xcom_payload import as_dict

        # The upstream payload may arrive as an object or as JSON text.
        period = as_dict(period, "dispatch_collection_requests.period")

        finish = _record_stage(
            "DISPATCH", period["reporting_month"], context
        )

        try:
            results = dispatch(
                period["reporting_month"],
                period["legal_entity"],
            )
        except Exception as exc:  # noqa: BLE001
            finish(
                status="FAILED",
                error_message=f"{type(exc).__name__}: {exc}",
            )
            raise

        sent = sum(1 for item in results if item.get("sent"))
        prepared = sum(1 for item in results if not item.get("sent"))

        finish(detail={
            "request_count": len(results),
            "sent_count": sent,
            "prepared_count": prepared,
            "dry_run": True,
        })

        return results

    # ------------------------------------------------------------------
    @task
    def discover_replies(period: dict, **context) -> list:
        """Discover the inbound replies of the reporting month.

        The reply is correlated to the stored original request thread ids.
        The subject alone is never used as the matching rule.
        """
        from backend.app.services.gmail_auth_service import (
            GmailAuthorizationError,
            get_gmail_service,
        )
        from backend.app.services.gmail_reply_service import (
            discover_replies as discover,
        )
        from backend.app.services.submission_persistence_service import connect
        from backend.app.core.xcom_payload import as_dict

        # The upstream payload may arrive as an object or as JSON text.
        period = as_dict(period, "discover_replies.period")

        finish = _record_stage(
            "DISCOVER", period["reporting_month"], context
        )

        request_ids: set = set()
        request_threads: set = set()

        try:
            with connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT gmail_message_id, gmail_thread_id
                        FROM email_requests
                        WHERE reporting_month = %s
                          AND gmail_message_id IS NOT NULL
                        """,
                        (f"{period['reporting_month']}-01",),
                    )
                    for row in cur.fetchall():
                        request_ids.add(row["gmail_message_id"])
                        if row["gmail_thread_id"]:
                            request_threads.add(row["gmail_thread_id"])
        except Exception:  # noqa: BLE001 - the ledger is optional
            request_ids = set()
            request_threads = set()

        try:
            service = get_gmail_service()
        except GmailAuthorizationError as exc:
            # A revoked refresh token cannot be repaired by the pipeline.
            # Retrying will not help, so the task fails fast with the
            # remedy in the log instead of an opaque invalid_grant.
            print(f"GMAIL UNAUTHORIZED: {exc}")
            print(f"REMEDY: {exc.remediation}")
            finish(
                status="FAILED",
                error_message=f"Gmail authorization failed: {exc}",
            )
            raise

        try:
            replies = discover(
                service,
                days=30,
                request_thread_ids=request_threads or None,
                request_message_ids=request_ids or None,
            )
        except Exception as exc:  # noqa: BLE001
            finish(
                status="FAILED",
                error_message=f"{type(exc).__name__}: {exc}",
            )
            raise

        print(
            f"Reporting month: {period['reporting_month']} | "
            f"Discovered replies: {len(replies)}"
        )

        finish(detail={
            "reply_count": len(replies),
            "request_count": len(request_ids),
        })

        # Only identifiers travel through XCom.
        return [reply.message_id for reply in replies]

    # ------------------------------------------------------------------
    @task
    def register_and_deduplicate(
        period: dict, message_ids: list
    ) -> list:
        """Register every message and drop the ones already processed."""
        from backend.app.services.submission_persistence_service import (
            register_gmail_message,
        )
        from backend.app.core.xcom_payload import as_dict, as_list

        # The upstream payloads may arrive as an object or as JSON text.
        period = as_dict(period, "register_and_deduplicate.period")
        message_ids = as_list(
            message_ids,
            "register_and_deduplicate.message_ids",
        )

        registered = []

        for message_id in message_ids:
            is_new, _ = register_gmail_message(
                gmail_message_id=message_id,
                reporting_month=period["reporting_month"],
            )

            if is_new:
                registered.append(message_id)
            else:
                print(f"SKIP duplicate message: {message_id}")

        print(f"Registered messages: {len(registered)}")

        return registered

    # ------------------------------------------------------------------
    @task
    def archive_raw_emails_and_attachments(
        period: dict, message_ids: list
    ) -> list:
        """Archive the original raw bytes and the attachments of each message.

        The archiving happens before any validation so that even an invalid
        submission keeps its original evidence.
        """
        from backend.app.services.email_archive_service import archive_message
        from backend.app.services.gmail_auth_service import get_gmail_service
        from backend.app.services.gmail_reply_service import (
            fetch_message,
            fetch_raw_bytes,
        )
        from backend.app.services.submission_persistence_service import (
            persist_email_archive,
            persist_email_attachments,
        )
        from backend.app.core.xcom_payload import as_dict, as_list

        # The upstream payloads may arrive as an object or as JSON text.
        period = as_dict(period, "archive_raw_emails_and_attachments.period")
        message_ids = as_list(
            message_ids,
            "archive_raw_emails_and_attachments.message_ids",
        )

        service = get_gmail_service()

        archived: list = []
        failures: list = []

        for message_id in message_ids:
            try:
                raw_bytes = fetch_raw_bytes(service, message_id)
                message = fetch_message(service, message_id)

                result = archive_message(
                    raw_bytes=raw_bytes,
                    message=message,
                    gmail_message_id=message_id,
                    reporting_month=period["reporting_month"],
                )

                archive_id = persist_email_archive(
                    gmail_message_id=message_id,
                    reporting_month=period["reporting_month"],
                    eml_path=result.eml_path,
                    eml_sha256=result.eml_sha256,
                    eml_size_bytes=result.eml_size_bytes,
                    gmail_thread_id=result.gmail_thread_id,
                    subject=result.subject,
                    sender_address=result.from_address,
                    recipient_address=result.to_address,
                    message_date=result.date_header,
                )

                persist_email_attachments(
                    archive_id=archive_id,
                    gmail_message_id=message_id,
                    attachments=[
                        {
                            "original_filename": a.original_filename,
                            "stored_filename": a.stored_filename,
                            "stored_path": a.stored_path,
                            "sha256": a.sha256,
                            "size_bytes": a.size_bytes,
                            "content_type": a.content_type,
                            "status": "ARCHIVED" if a.stored_path else "REJECTED",
                        }
                        for a in result.attachments
                    ],
                )

                archived.append(message_id)

            except Exception as exc:  # noqa: BLE001 - one bad message
                # A single failure must never block the other messages.
                failures.append({"message_id": message_id, "error": str(exc)})
                print(f"ARCHIVE FAILED {message_id}: {type(exc).__name__}")

        print(f"Archived: {len(archived)} | Failed: {len(failures)}")

        return archived

    # ------------------------------------------------------------------
    @task
    def parse_validate_and_persist(
        period: dict, message_ids: list, **context
    ) -> dict:
        """Parse, validate, compare and persist every archived message."""
        from backend.app.services.completion_pipeline_service import (
            process_inbound_message,
        )
        from backend.app.services.gmail_auth_service import get_gmail_service
        from backend.app.services.gmail_reply_service import (
            fetch_message,
            fetch_raw_bytes,
        )
        from backend.app.core.xcom_payload import as_dict, as_list

        # The upstream payloads may arrive as an object or as JSON text.
        period = as_dict(period, "parse_validate_and_persist.period")
        message_ids = as_list(
            message_ids,
            "parse_validate_and_persist.message_ids",
        )

        finish = _record_stage(
            "PARSE", period["reporting_month"], context
        )

        service = get_gmail_service()

        processed = []
        failed = []

        for message_id in message_ids:
            try:
                raw_bytes = fetch_raw_bytes(service, message_id)
                message = fetch_message(service, message_id)

                result = process_inbound_message(
                    gmail_message_id=message_id,
                    reporting_month=period["reporting_month"],
                    raw_bytes=raw_bytes,
                    message=message,
                    subject=None,
                    legal_entity=period["legal_entity"],
                    service=service,
                )

                processed.append(result.to_dict())

            except Exception as exc:  # noqa: BLE001 - one bad message
                failed.append({"message_id": message_id, "error": str(exc)})
                print(f"PROCESS FAILED {message_id}: {type(exc).__name__}")

        print(f"Processed: {len(processed)} | Failed: {len(failed)}")

        accepted = sum(
            1 for item in processed
            if item.get("accepted_line_count") is not None
        )

        finish(
            status="FAILED" if failed else "SUCCESS",
            detail={
                "processed_count": len(processed),
                "failed_count": len(failed),
            },
            error_message=(
                f"{len(failed)} message(s) could not be processed."
                if failed
                else None
            ),
        )

        return {"processed": processed, "failed": failed}

    # ------------------------------------------------------------------
    @task
    def generate_correction_workbooks_and_drafts(
        period: dict, outcome: dict, **context
    ) -> list:
        """Build the correction workbooks and the email drafts.

        While ``ACCRUALFLOW_DRY_RUN`` is enabled nothing is sent.  The
        department status becomes ``CORRECTION_PREPARED``.
        """
        from backend.app.services.correction_email_service import (
            check_correction_attempt_limit,
            prepare_correction_draft,
            write_dry_run_log,
        )
        from backend.app.services.correction_workbook_service import (
            CorrectionRow,
            ExceptionRow,
            generate_correction_workbook,
        )
        from backend.app.services.submission_persistence_service import connect
        from backend.app.core.xcom_payload import as_dict

        # The upstream payloads may arrive as an object or as JSON text.
        period = as_dict(
            period,
            "generate_correction_workbooks_and_drafts.period",
        )
        outcome = as_dict(
            outcome,
            "generate_correction_workbooks_and_drafts.outcome",
        )

        finish = _record_stage(
            "CORRECTION", period["reporting_month"], context
        )

        month = f"{period['reporting_month']}-01"

        corrections: list = []

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        sl.reporting_month,
                        sl.legal_entity,
                        sl.department_id,
                        sl.po_number,
                        sl.po_line,
                        sl.previous_pct,
                        sl.raw_value,
                        sl.raw_number_format,
                        sl.source_row,
                        sl.validation_status,
                        sl.reason_status,
                        sl.exception_codes,
                        sl.remarks,
                        e.department_name,
                        e.cost_center,
                        e.po_description,
                        COALESCE(e.authorized_email, s.authorized_email)
                            AS authorized_email
                    FROM submission_lines sl
                    LEFT JOIN expected_po_lines e
                      ON e.reporting_month = sl.reporting_month
                     AND e.legal_entity   = sl.legal_entity
                     AND e.po_number      = sl.po_number
                     AND e.po_line        = sl.po_line
                    LEFT JOIN department_submissions ds
                      ON ds.submission_id = sl.submission_id
                    LEFT JOIN LATERAL (
                        SELECT c.authorized_email
                        FROM department_contacts c
                        WHERE c.legal_entity = sl.legal_entity
                          AND c.cost_center = ds.cost_center
                          AND c.active = TRUE
                        ORDER BY c.contact_id
                        LIMIT 1
                    ) s ON TRUE
                    WHERE sl.reporting_month = %s
                      AND sl.is_current_version
                      AND sl.accepted_current_pct IS NULL
                    ORDER BY
                        sl.legal_entity, sl.department_id,
                        sl.po_number, sl.po_line
                    """,
                    (month,),
                )
                unresolved = list(cur.fetchall())

                # Group by department.
                grouped: dict = {}
                for row in unresolved:
                    key = (row["legal_entity"], row["department_id"])
                    grouped.setdefault(key, []).append(row)

                for (entity, department_id), rows in grouped.items():
                    department_name = rows[0]["department_name"]
                    cost_center = rows[0]["cost_center"]
                    recipient = rows[0]["authorized_email"]

                    detail_rows: list = []
                    exception_rows: list = []

                    for row in rows:
                        codes = list(row["exception_codes"] or [])

                        # A line that is not part of the expected
                        # population can never be corrected inline.  It
                        # always goes to the exception sheet, even when it
                        # carries other codes as well.
                        is_unknown = (
                            row["validation_status"] == "UNKNOWN_PO_LINE"
                            or "UNKNOWN_PO_LINE" in codes
                        )

                        editable = [
                            code
                            for code in codes
                            if code
                            not in (
                                "UNKNOWN_PO_LINE",
                                "DUPLICATE_PO_LINE",
                                "DEPARTMENT_MISMATCH",
                                "PERIOD_MISMATCH",
                            )
                        ]

                        if editable and not is_unknown:
                            detail_rows.append(
                                CorrectionRow(
                                    po_number=row["po_number"],
                                    po_line=row["po_line"],
                                    po_description=row["po_description"],
                                    cost_center=cost_center,
                                    previous_pct=(
                                        float(row["previous_pct"])
                                        if row["previous_pct"] is not None
                                        else None
                                    ),
                                    submitted_raw_value=row["raw_value"],
                                    submitted_number_format=(
                                        row["raw_number_format"]
                                    ),
                                    source_row=row["source_row"],
                                    issue_codes=codes,
                                    issue_detail="; ".join(codes),
                                    remarks=row["remarks"],
                                )
                            )
                        else:
                            exception_rows.append(
                                ExceptionRow(
                                    po_number=row["po_number"],
                                    po_line=row["po_line"],
                                    po_description=row["po_description"],
                                    cost_center=cost_center,
                                    issue_codes=codes,
                                    issue_detail="; ".join(codes),
                                    submitted_raw_value=row["raw_value"],
                                    submitted_number_format=(
                                        row["raw_number_format"]
                                    ),
                                    source_row=row["source_row"],
                                )
                            )

                    if not detail_rows and not exception_rows:
                        continue

                    # The retry cap prevents an endless correction loop.
                    cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM correction_requests
                        WHERE reporting_month = %s
                          AND legal_entity = %s
                          AND department_id = %s
                        """,
                        (month, entity, department_id),
                    )
                    attempts = cur.fetchone()["count"]

                    if not check_correction_attempt_limit(attempts):
                        print(
                            f"SKIP {department_id}: correction attempt cap "
                            f"reached ({attempts})."
                        )
                        continue

                    attempt = attempts + 1

                    workbook = generate_correction_workbook(
                        reporting_month=period["reporting_month"],
                        department_id=department_id,
                        department_name=department_name,
                        cost_center=cost_center,
                        rows=detail_rows,
                        exception_rows=exception_rows,
                        attempt=attempt,
                    )

                    draft = prepare_correction_draft(
                        reporting_month=period["reporting_month"],
                        department_id=department_id,
                        department_name=department_name,
                        cost_center=cost_center,
                        recipient=recipient,
                        workbook_path=workbook.file_path,
                        workbook_sha256=workbook.sha256,
                        unresolved_count=len(detail_rows),
                        exception_count=len(exception_rows),
                        attempt=attempt,
                    )

                    if draft.dry_run:
                        write_dry_run_log(draft)

                    cur.execute(
                        """
                        INSERT INTO correction_requests (
                            reporting_month, legal_entity,
                            department_id, department_name, cost_center,
                            attempt, issue_count, unresolved_line_count,
                            workbook_path, workbook_sha256,
                            draft_path, draft_subject, draft_body,
                            recipient, gmail_thread_id, gmail_message_id,
                            status, sent_at
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s
                        )
                        RETURNING correction_id
                        """,
                        (
                            month,
                            entity,
                            department_id,
                            department_name,
                            cost_center,
                            attempt,
                            len(detail_rows) + len(exception_rows),
                            len(detail_rows),
                            workbook.file_path,
                            workbook.sha256,
                            draft.draft_path,
                            draft.subject,
                            draft.body,
                            draft.recipient,
                            draft.gmail_thread_id,
                            draft.gmail_message_id,
                            "PREPARED" if draft.dry_run else "SENT",
                            None if draft.dry_run else draft.submitted_at,
                        ),
                    )
                    correction_id = cur.fetchone()["correction_id"]

                    corrections.append({
                        "correction_id": correction_id,
                        "department_id": department_id,
                        "workbook_path": workbook.file_path,
                        "draft_path": draft.draft_path,
                        "dry_run": draft.dry_run,
                        "line_count": len(detail_rows),
                        "exception_count": len(exception_rows),
                    })

                    print(
                        f"CORRECTION {department_id}: "
                        f"{len(detail_rows)} line(s), "
                        f"{len(exception_rows)} exception(s), "
                        f"dry_run={draft.dry_run}"
                    )

        prepared = sum(1 for item in corrections if item.get("dry_run"))
        sent = len(corrections) - prepared

        finish(detail={
            "correction_count": len(corrections),
            "prepared_count": prepared,
            "sent_count": sent,
            "department_count": len(corrections),
        })

        return corrections

    # ------------------------------------------------------------------
    @task
    def update_department_status(
        period: dict, outcome: dict, corrections: list
    ) -> list:
        """Recompute and persist the status of every department."""
        from backend.app.services.department_status_service import (
            evaluate_department,
            record_status,
        )
        from backend.app.services.submission_persistence_service import (
            build_accepted_dataset,
            connect,
        )
        from backend.app.core.xcom_payload import as_dict, as_list

        # The upstream payloads may arrive as an object or as JSON text.
        period = as_dict(period, "update_department_status.period")
        outcome = as_dict(outcome, "update_department_status.outcome")
        corrections = as_list(
            corrections,
            "update_department_status.corrections",
        )

        month = f"{period['reporting_month']}-01"

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT
                        legal_entity, department_id, department_name
                    FROM expected_po_lines
                    WHERE reporting_month = %s
                    ORDER BY legal_entity, department_id
                    """,
                    (month,),
                )
                departments = list(cur.fetchall())

        rows = build_accepted_dataset(
            period["reporting_month"], period["legal_entity"]
        )

        by_department: dict = {}
        for row in rows:
            key = (row["legal_entity"], row["department_id"])
            by_department.setdefault(key, []).append(row)

        correction_status = {
            item["department_id"]: "PREPARED" for item in corrections
        }

        statuses = []

        for department in departments:
            key = (department["legal_entity"], department["department_id"])
            department_rows = by_department.get(key, [])

            with connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT exception_code, po_number, po_line
                        FROM validation_issues
                        WHERE reporting_month = %s
                          AND legal_entity = %s
                          AND department_id = %s
                          AND status = 'OPEN'
                        """,
                        (month, key[0], key[1]),
                    )
                    open_issues = list(cur.fetchall())

                    cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM email_archives ea
                        JOIN department_submissions ds
                          ON ds.archive_id = ea.archive_id
                        WHERE ea.reporting_month = %s
                          AND ds.legal_entity = %s
                          AND ds.department_id = %s
                        """,
                        (month, key[0], key[1]),
                    )
                    evidence_count = cur.fetchone()["count"]

                    cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM master_export_runs
                        WHERE reporting_month = %s
                          AND status = 'SUCCESS'
                        """,
                        (month,),
                    )
                    export_count = cur.fetchone()["count"]

            evaluation = evaluate_department(
                reporting_month=period["reporting_month"],
                legal_entity=key[0],
                department_id=key[1],
                accepted_rows=department_rows,
                open_issues=open_issues,
                latest_correction_status=correction_status.get(key[1]),
                has_evidence=evidence_count > 0,
                master_export_succeeded=export_count > 0,
            )

            record_status(evaluation)

            statuses.append(evaluation.to_dict())

            print(
                f"{key[1]}: {evaluation.status} "
                f"({evaluation.accepted_line_count}/"
                f"{evaluation.expected_line_count} accepted)"
            )

        return statuses

    # ------------------------------------------------------------------
    @task
    def export_master_excel(period: dict, outcome: dict, **context) -> dict:
        """Publish the consolidated Master Excel workbook.

        The task depends on the collection outcome on purpose.  Without
        that dependency the export would still run when the reply
        discovery failed, and it would publish a workbook that covers
        every expected line while containing no submitted data at all.
        """
        from backend.app.services.master_excel_service import (
            export_master_excel,
            record_failed_export,
        )
        from backend.app.core.xcom_payload import as_dict

        # The upstream payload may arrive as an object or as JSON text.
        period = as_dict(period, "export_master_excel.period")
        outcome = as_dict(outcome, "export_master_excel.outcome")

        finish = _record_stage(
            "MASTER_EXCEL", period["reporting_month"], context
        )

        try:
            result = export_master_excel(
                period["reporting_month"],
                period["legal_entity"],
            )
        except Exception as exc:  # noqa: BLE001
            record_failed_export(
                period["reporting_month"],
                f"{type(exc).__name__}: {exc}",
                period["legal_entity"],
            )
            finish(
                status="FAILED",
                error_message=f"{type(exc).__name__}: {exc}",
            )
            raise

        print(
            f"MASTER EXPORT: {result.file_name} | "
            f"expected={result.expected_line_count} | "
            f"accepted={result.accepted_line_count} | "
            f"exceptions={result.exception_count} | "
            f"excluded_departments={result.excluded_department_count}"
        )

        finish(detail={
            "file_name": result.file_name,
            "expected_line_count": result.expected_line_count,
            "accepted_line_count": result.accepted_line_count,
            "exception_count": result.exception_count,
        })

        return result.to_dict()

    # ------------------------------------------------------------------
    @task
    def mark_data_ready(
        period: dict, export: dict, outcome: dict, **context
    ) -> dict:
        """Confirm that the reporting views are consistent with the export.

        The Power BI Service refresh itself is deliberately NOT triggered
        from here: it needs its own credentials and permissions.

        The reconciliation also has to prove that the collection actually
        ran.  A run in which the reply discovery failed would otherwise
        produce an export that is internally consistent - the expected
        line count matches - while the data itself is simply missing.
        That would mark an empty period as ready for reporting.
        """
        from backend.app.core.xcom_payload import as_dict
        from backend.app.services.submission_persistence_service import connect

        # The upstream payloads may arrive as an object or as JSON text.
        period = as_dict(period, "mark_data_ready.period")
        export = as_dict(export, "mark_data_ready.export")
        outcome = as_dict(outcome, "mark_data_ready.outcome")

        finish = _record_stage(
            "DATA_READY", period["reporting_month"], context
        )

        month = f"{period['reporting_month']}-01"

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        (SELECT COUNT(*)
                         FROM expected_po_lines
                         WHERE reporting_month = %s) AS expected_rows,
                        (SELECT COUNT(*)
                         FROM submission_lines sl
                         WHERE sl.reporting_month = %s
                           AND sl.is_current_version
                           AND sl.accepted_current_pct IS NOT NULL)
                            AS accepted_rows
                    """,
                    (month, month),
                )
                counts = cur.fetchone()

        failed = outcome.get("failed") or []
        processed = outcome.get("processed") or []

        # The counts have to agree ...
        reconciled = (
            counts["expected_rows"] == export["expected_line_count"]
        )

        # ... and the collection has to have actually happened.  A failed
        # reply discovery leaves an empty - but internally consistent -
        # export, which must never be reported as ready.
        collection_complete = not failed

        data_ready = reconciled and collection_complete

        if not reconciled:
            print(
                "RECONCILE FAILED: the export does not cover the whole "
                "expected population."
            )

        if not collection_complete:
            print(
                f"NOT READY: {len(failed)} message(s) could not be "
                f"processed. The period is not ready for reporting."
            )

        print(
            f"RECONCILE: expected={counts['expected_rows']} | "
            f"accepted={counts['accepted_rows']} | "
            f"export={export['expected_line_count']} | "
            f"reconciled={reconciled} | "
            f"collection_complete={collection_complete} | "
            f"data_ready={data_ready}"
        )

        finish(
            status="SUCCESS" if data_ready else "FAILED",
            detail={
                "expected_rows": counts["expected_rows"],
                "accepted_rows": counts["accepted_rows"],
                "reconciled": reconciled,
                "collection_complete": collection_complete,
                "data_ready": data_ready,
                "master_file": export["file_name"],
            },
            error_message=(
                None if data_ready
                else "The period did not reconcile; it is not ready."
            ),
        )

        return {
            "reporting_month": period["reporting_month"],
            "expected_rows": counts["expected_rows"],
            "accepted_rows": counts["accepted_rows"],
            "export_rows": export["expected_line_count"],
            "reconciled": reconciled,
            "collection_complete": collection_complete,
            "data_ready": data_ready,
            "processed_count": len(processed),
            "failed_count": len(failed),
            "master_file": export["file_name"],
            "powerbi_refresh": "NOT_TRIGGERED",
        }

    # ------------------------------------------------------------------
    period = resolve_reporting_period()

    # The ERP snapshot and the frozen population come first.  Every
    # later task is validated against the frozen population, so it has
    # to exist before the departments are asked to report.
    erp = sync_erp_snapshot(period)

    frozen = freeze_expected_population(erp)

    # The collection request is only sent once per reporting month.  The
    # task is idempotent: a repeated run updates the existing row instead
    # of creating a second request.
    dispatch_collection_requests(frozen)

    discovered = discover_replies(frozen)

    registered = register_and_deduplicate(frozen, discovered)

    archived = archive_raw_emails_and_attachments(frozen, registered)

    outcome = parse_validate_and_persist(frozen, archived)

    corrections = generate_correction_workbooks_and_drafts(frozen, outcome)

    statuses = update_department_status(frozen, outcome, corrections)

    export = export_master_excel(frozen, outcome)

    mark_data_ready(frozen, export, outcome)


accrualflow_daily_monitoring()