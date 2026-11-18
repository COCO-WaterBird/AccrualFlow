#!/bin/bash
# Run the AccrualFlow DAG against a throwaway reporting month.
#
# The month is pinned with ACCRUALFLOW_REPORTING_MONTH so that a test run
# never writes into the live reporting period.  The default is the
# pre-seeded demo period, so the run has a frozen population to work on.
#
# Usage: scripts/run_dag_test.sh [YYYY-MM]
#
# Run it from a terminal where the Docker socket is reachable, not from
# inside a sandboxed shell.
set -uo pipefail

MONTH="${1:-2098-11}"
CONTAINER="accrualflow-airflow-airflow-scheduler-1"

echo "=== Running the DAG against ${MONTH}"

docker exec \
  -e ACCRUALFLOW_REPORTING_MONTH="${MONTH}" \
  "${CONTAINER}" \
  airflow dags test accrualflow_daily_monitoring "${MONTH}-01"
status=$?

echo
echo "=== RECONCILE"
docker exec "${CONTAINER}" sh -c \
  'grep -hoE "RECONCILE: .*" /opt/airflow/logs/dag_id=accrualflow_daily_monitoring/run_id=manual__*/task_id=mark_data_ready/attempt=1.log 2>/dev/null | tail -1'

echo "=== EXIT: ${status}"
exit "${status}"
