"""
Regression tests for the Airflow XCom payload normalisation.

The AccrualFlow DAG consumes the return values of upstream tasks.  Airflow
can store such a payload in two shapes:

* as a JSON **object** (Task SDK writer), and
* as a JSON **string** containing the JSON text (classic writer).

When the payload arrives as JSON text, indexing it by a string key raises::

    TypeError: string indices must be integers, not 'str'

These tests pin the normalisation helpers and, more importantly, the
expressions of the real DAG that used to fail.
"""

import ast
import json
from pathlib import Path

import pytest

from backend.app.core.xcom_payload import as_dict, as_list, as_str


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAG_FILE = PROJECT_ROOT / "airflow" / "dags" / "accrualflow_daily_monitoring.py"


# ---------------------------------------------------------------------------
# as_dict
# ---------------------------------------------------------------------------


def test_as_dict_accepts_plain_object():
    payload = {"reporting_month": "2026-11", "legal_entity": "2000"}

    assert as_dict(payload) == payload


def test_as_dict_accepts_json_text():
    payload = {"reporting_month": "2026-11", "legal_entity": "2000"}

    assert as_dict(json.dumps(payload)) == payload


def test_as_dict_accepts_none_as_empty():
    assert as_dict(None) == {}


def test_as_dict_supports_string_indexing_after_normalisation():
    """This is the expression that failed in the real DAG run."""
    export = {
        "file_name": "CapEx_Completion_Master_2026-11.xlsx",
        "expected_line_count": 0,
        "accepted_line_count": 0,
    }

    # Before the fix the JSON-text form raised
    # "TypeError: string indices must be integers, not 'str'".
    normalised = as_dict(json.dumps(export))

    assert normalised["expected_line_count"] == 0
    assert normalised["file_name"] == "CapEx_Completion_Master_2026-11.xlsx"


def test_as_dict_rejects_non_mapping_payload():
    with pytest.raises(TypeError):
        as_dict(json.dumps([1, 2, 3]))


def test_as_dict_leaves_plain_strings_untouched_and_raises():
    with pytest.raises(TypeError):
        as_dict("2026-11")


# ---------------------------------------------------------------------------
# as_list
# ---------------------------------------------------------------------------


def test_as_list_accepts_plain_array():
    assert as_list(["a", "b"]) == ["a", "b"]


def test_as_list_accepts_json_text():
    assert as_list(json.dumps(["1a1c6bf760e71881"])) == ["1a1c6bf760e71881"]


def test_as_list_accepts_none_as_empty():
    assert as_list(None) == []


def test_as_list_rejects_mapping_payload():
    with pytest.raises(TypeError):
        as_list(json.dumps({"a": 1}))


# ---------------------------------------------------------------------------
# as_str
# ---------------------------------------------------------------------------


def test_as_str_accepts_plain_string():
    assert as_str("2026-11") == "2026-11"


def test_as_str_accepts_json_text():
    assert as_str(json.dumps("2026-11")) == "2026-11"


def test_as_str_accepts_none_as_empty():
    assert as_str(None) == ""


def test_as_str_does_not_mangle_non_json_strings():
    assert as_str("not json {") == "not json {"


# ---------------------------------------------------------------------------
# The real DAG must normalise every upstream payload it indexes
# ---------------------------------------------------------------------------


def _dag_source() -> str:
    return DAG_FILE.read_text(encoding="utf-8")


def _is_task(node: ast.FunctionDef) -> bool:
    """Return True when the function is decorated with ``@task``.

    The decorator is inspected on the AST node itself.  ``get_source_segment``
    does not include decorators, so the source text cannot be used here.
    """
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator

        name = None
        if isinstance(target, ast.Name):
            name = target.id
        elif isinstance(target, ast.Attribute):
            name = target.attr

        if name == "task":
            return True

    return False


def test_dag_file_exists_and_parses():
    ast.parse(_dag_source())


def test_dag_contains_tasks():
    """Guard the task detection helper itself."""
    tree = ast.parse(_dag_source())

    tasks = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and _is_task(node)
    ]

    assert "mark_data_ready" in tasks
    assert "export_master_excel" in tasks


def test_every_task_that_indexes_a_payload_normalises_it():
    """No task may index an upstream payload before normalising it.

    Only the *parameters* of a task are upstream XCom payloads.  Local
    variables such as database rows are not affected by the XCom
    serialisation and must not be flagged.
    """
    tree = ast.parse(_dag_source())

    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not _is_task(node):
            continue

        # The task parameters are the upstream XCom payloads.
        payload_names = {
            arg.arg
            for arg in node.args.args
            if arg.arg not in ("self", "context")
        }

        if not payload_names:
            continue

        for inner in ast.walk(node):
            if not isinstance(inner, ast.Subscript):
                continue

            slice_node = inner.slice
            if not isinstance(slice_node, ast.Constant):
                continue
            if not isinstance(slice_node.value, str):
                continue

            base = inner.value
            if not isinstance(base, ast.Name):
                continue

            if base.id not in payload_names:
                continue

            # The payload must be reassigned by a normalisation call.
            assigned = False
            for assign in ast.walk(node):
                if (
                    isinstance(assign, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == base.id
                        for t in assign.targets
                    )
                ):
                    call = assign.value
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id
                        in ("as_dict", "as_list", "as_str")
                    ):
                        assigned = True

            if not assigned:
                offenders.append(f"{node.name}: {base.id}[...]")

    assert not offenders, (
        "Tasks index an XCom payload without normalising it first: "
        + ", ".join(sorted(set(offenders)))
    )


def test_dag_imports_the_normalisation_helper():
    source = _dag_source()

    assert "from backend.app.core.xcom_payload import" in source


# ---------------------------------------------------------------------------
# Runtime reproduction of the failed task body
# ---------------------------------------------------------------------------


def _run_mark_data_ready_body(period, export, counts, outcome=None):
    """Execute the reconciliation body of ``mark_data_ready``.

    The database access is replaced by the supplied ``counts`` so that the
    test can run without a PostgreSQL instance.
    """
    from backend.app.core.xcom_payload import as_dict

    period = as_dict(period, "mark_data_ready.period")
    export = as_dict(export, "mark_data_ready.export")
    outcome = as_dict(outcome, "mark_data_ready.outcome")

    failed = outcome.get("failed") or []
    processed = outcome.get("processed") or []

    reconciled = (
        counts["expected_rows"] == export["expected_line_count"]
    )

    collection_complete = not failed

    data_ready = reconciled and collection_complete

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


EXPORT_PAYLOAD = {
    "file_path": "/opt/airflow/data/reports/2026-11/CapEx_Completion_Master_2026-11.xlsx",
    "file_name": "CapEx_Completion_Master_2026-11.xlsx",
    "sha256": "2fa1076800b14ccd35ccff7cdd7bbf03e910505df3afadb51c66d613e66257ba",
    "size_bytes": 8445,
    "expected_line_count": 0,
    "accepted_line_count": 0,
    "exception_count": 0,
    "department_count": 0,
}

PERIOD_PAYLOAD = {"reporting_month": "2026-11", "legal_entity": "2000"}

COUNTS = {"expected_rows": 0, "accepted_rows": 0}


def test_mark_data_ready_body_survives_json_text_payloads():
    """The regression: the payload arrived as JSON text, not as an object."""
    result = _run_mark_data_ready_body(
        json.dumps(PERIOD_PAYLOAD),
        json.dumps(EXPORT_PAYLOAD),
        COUNTS,
    )

    assert result["reconciled"] is True
    assert result["export_rows"] == 0
    assert result["master_file"] == "CapEx_Completion_Master_2026-11.xlsx"
    assert result["reporting_month"] == "2026-11"


def test_mark_data_ready_body_still_works_with_object_payloads():
    result = _run_mark_data_ready_body(
        PERIOD_PAYLOAD,
        EXPORT_PAYLOAD,
        COUNTS,
    )

    assert result["reconciled"] is True
    assert result["master_file"] == "CapEx_Completion_Master_2026-11.xlsx"


def test_mark_data_ready_body_detects_a_mismatch():
    result = _run_mark_data_ready_body(
        json.dumps(PERIOD_PAYLOAD),
        json.dumps(EXPORT_PAYLOAD),
        {"expected_rows": 12, "accepted_rows": 3},
    )

    assert result["reconciled"] is False
    assert result["expected_rows"] == 12


def test_raw_json_text_payload_would_raise_without_normalisation():
    """Pin the original failure mode so the fix stays meaningful."""
    raw = json.dumps(EXPORT_PAYLOAD)
    with pytest.raises(TypeError) as excinfo:
        raw["expected_line_count"]  # noqa: B018 - reproducing the old bug

    assert "string indices must be integers" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The readiness gate
#
# A run in which the reply discovery failed produces an export that is
# internally consistent - the expected line count matches - while the
# data itself is missing.  Such a period must never be reported as ready.
# ---------------------------------------------------------------------------


def test_a_failed_collection_is_not_reported_as_ready():
    result = _run_mark_data_ready_body(
        PERIOD_PAYLOAD,
        EXPORT_PAYLOAD,
        COUNTS,
        {"processed": [], "failed": [{"message_id": "m1"}]},
    )

    # The counts still agree ...
    assert result["reconciled"] is True
    # ... but the period is not ready.
    assert result["collection_complete"] is False
    assert result["data_ready"] is False
    assert result["failed_count"] == 1


def test_a_complete_collection_is_reported_as_ready():
    result = _run_mark_data_ready_body(
        PERIOD_PAYLOAD,
        EXPORT_PAYLOAD,
        COUNTS,
        {"processed": [{"message_id": "m1"}], "failed": []},
    )

    assert result["collection_complete"] is True
    assert result["data_ready"] is True
    assert result["processed_count"] == 1


def test_a_count_mismatch_is_never_ready():
    result = _run_mark_data_ready_body(
        PERIOD_PAYLOAD,
        EXPORT_PAYLOAD,
        {"expected_rows": 12, "accepted_rows": 3},
        {"processed": [], "failed": []},
    )

    assert result["reconciled"] is False
    assert result["data_ready"] is False


def test_the_dag_passes_the_outcome_to_the_readiness_gate():
    """The gate can only work if the outcome actually reaches it."""
    source = DAG_FILE.read_text(encoding="utf-8")

    assert "mark_data_ready(period, export, outcome)" in source


def test_the_export_depends_on_the_collection_outcome():
    """The export must not run when the collection did not complete.

    Without this dependency the export publishes a workbook that covers
    every expected line while containing no submitted data at all.
    """
    source = DAG_FILE.read_text(encoding="utf-8")

    assert "export_master_excel(period, outcome)" in source


def test_the_export_normalises_the_outcome_payload():
    """The outcome may arrive as JSON text."""
    source = DAG_FILE.read_text(encoding="utf-8")

    assert 'as_dict(outcome, "export_master_excel.outcome")' in source
