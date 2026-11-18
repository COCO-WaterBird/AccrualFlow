"""
The manual trigger of the monthly collection cycle.

The owner starts one run per reporting month.  This service is the
bridge between the dashboard button and the Airflow REST API.

It deliberately does **not** run the pipeline itself.  The pipeline
lives in the Airflow DAG, where every task is retryable and every run
is recorded.  The trigger only asks Airflow to start a run and then
reports what Airflow says about it.

Two things are guarded here:

* **The month is validated.**  A malformed month would be rejected by
  the DAG anyway, but the dashboard should say so immediately instead
  of creating a run that fails minutes later.
* **A run is never started twice for the same month while one is
  already active.**  Two concurrent runs for one month would both
  write to the same reporting period.  The check is advisory, not a
  lock: Airflow's ``max_active_runs`` is the real guard.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import httpx


# The Airflow REST API of the local stack.
#
# The host is the Airflow API server.  From the host itself that is
# localhost; from inside the Airflow network it is the service name.
# The port is the published port of the API server.
def _airflow_base_url() -> str:
    return os.getenv(
        "ACCRUALFLOW_AIRFLOW_API_URL",
        "http://127.0.0.1:8081",
    ).rstrip("/")


def _airflow_credentials() -> tuple[str, str]:
    username = os.getenv("ACCRUALFLOW_AIRFLOW_USER", "airflow")
    password = os.getenv("ACCRUALFLOW_AIRFLOW_PASSWORD", "")

    if not password:
        raise TriggerError(
            "The Airflow credentials are not configured. Set "
            "ACCRUALFLOW_AIRFLOW_PASSWORD."
        )

    return username, password


DAG_ID = "accrualflow_daily_monitoring"

# How long the token request and the trigger request may take.
REQUEST_TIMEOUT = 20.0


class TriggerError(Exception):
    """The run could not be started."""


def validate_month(reporting_month: str) -> str:
    """Validate a reporting month and return it.

    A malformed month is rejected here rather than deeper in the DAG,
    so that the owner gets the answer immediately.
    """
    value = (reporting_month or "").strip()

    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise TriggerError(
            f"The reporting month must be in YYYY-MM format, got {value!r}"
        )

    month = int(value[5:7])

    if not 1 <= month <= 12:
        raise TriggerError(
            f"The reporting month has an invalid month: {value!r}"
        )

    return value


def _token(client: httpx.Client, username: str, password: str) -> str:
    response = client.post(
        f"{_airflow_base_url()}/auth/token",
        json={"username": username, "password": password},
    )

    # The Airflow token endpoint answers with 201, not 200.
    if response.status_code not in (200, 201):
        raise TriggerError(
            "The Airflow API refused the credentials "
            f"({response.status_code})."
        )

    try:
        return response.json()["access_token"]
    except (KeyError, TypeError, ValueError) as exc:
        raise TriggerError(
            "The Airflow API did not return an access token."
        ) from exc


def _run_id(reporting_month: str) -> str:
    """A readable, unique run id for a manual monthly run."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"manual__{reporting_month}__{stamp}"


def trigger_month(reporting_month: str) -> dict:
    """Ask Airflow to start the cycle for one reporting month.

    Returns a summary of the run that was created.  The run itself is
    asynchronous: the dashboard shows the progress while it runs.
    """
    month = validate_month(reporting_month)

    username, password = _airflow_credentials()

    run_id = _run_id(month)

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            token = _token(client, username, password)

            response = client.post(
                f"{_airflow_base_url()}/api/v2/dags/{DAG_ID}/dagRuns",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "dag_run_id": run_id,
                    "logical_date": datetime.now(timezone.utc).isoformat(),
                    "conf": {"reporting_month": month},
                    "note": f"AccrualFlow monthly cycle for {month}",
                },
            )
    except httpx.RequestError as exc:
        raise TriggerError(
            f"The Airflow API is unreachable: {exc}"
        ) from exc

    if response.status_code not in (200, 201):
        detail = response.text[:400]

        raise TriggerError(
            f"Airflow refused to start the run ({response.status_code}): "
            f"{detail}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise TriggerError(
            "Airflow returned an unreadable response."
        ) from exc

    return {
        "dag_id": DAG_ID,
        "dag_run_id": payload.get("dag_run_id", run_id),
        "reporting_month": month,
        "state": payload.get("state", "queued"),
        "started_at": payload.get("start_date"),
        "logical_date": payload.get("logical_date"),
    }


def list_runs(limit: int = 10) -> list[dict]:
    """The most recent runs of the cycle, newest first.

    The dashboard uses it to show whether a run is active and how the
    last runs went.
    """
    username, password = _airflow_credentials()

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            token = _token(client, username, password)

            response = client.get(
                f"{_airflow_base_url()}/api/v2/dags/{DAG_ID}/dagRuns",
                headers={"Authorization": f"Bearer {token}"},
                params={"limit": limit, "order_by": "-logical_date"},
            )
    except httpx.RequestError as exc:
        raise TriggerError(
            f"The Airflow API is unreachable: {exc}"
        ) from exc

    if response.status_code != 200:
        raise TriggerError(
            f"Airflow refused to list the runs ({response.status_code})."
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise TriggerError(
            "Airflow returned an unreadable response."
        ) from exc

    runs = payload.get("dag_runs") or []

    return [
        {
            "dag_run_id": run.get("dag_run_id"),
            "state": run.get("state"),
            "run_type": run.get("run_type"),
            "logical_date": run.get("logical_date"),
            "start_date": run.get("start_date"),
            "end_date": run.get("end_date"),
            "triggered_by": run.get("triggered_by"),
            "conf": run.get("conf") or {},
        }
        for run in runs
    ]


def active_run() -> dict | None:
    """The run that is currently active, if there is one.

    A run is active while it is queued or running.  The dashboard uses
    it to keep the trigger button disabled while a cycle is in flight.
    """
    for run in list_runs(limit=10):
        if run.get("state") in ("queued", "running"):
            return run

    return None


def next_month(reference: datetime | None = None) -> str:
    """The reporting month that follows the given one.

    The default reference is today.  The result is the first day of the
    month after the reference month, which is the month the owner is
    most likely collecting for.
    """
    if reference is None:
        reference = datetime.now()

    first = reference.replace(day=1)

    return (first + timedelta(days=32)).replace(day=1).strftime("%Y-%m")
