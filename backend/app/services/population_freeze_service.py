"""
Freeze the expected PO line population for one reporting month.

This is the production entry point that connects the ERP master data to
the frozen population the whole pipeline is validated against.

    capex_po  x  costcenter_mapping
              |
              v
      expected_po_lines (frozen)

It lives in ``backend/app/services`` rather than in ``scripts/`` because
the Airflow container mounts only the ``backend`` directory.  The monthly
run calls it from inside the DAG, and the CLI in
``scripts/freeze_population.py`` is a thin wrapper around it.

Rules
-----
* **Recipient.**  The authorised recipient of a department is its
  ``manager`` from ``costcenter_mapping``.  That address is stored as
  both ``department_owner`` and ``authorized_email``.
* **Departments without PO lines are excluded.**  A cost centre that has
  no PO line in the ERP snapshot has nothing to report on.  The exclusion
  is never silent: it is written to the exclusion ledger, to a CSV for
  finance, and printed.
* **The previous baseline is never invented.**  It comes only from the
  finalised baselines of the previous month.  A missing baseline stays
  ``MISSING``.
* **Freezing is idempotent.**  Re-running with the same content is a
  no-op.  Re-running with different content is refused by
  ``freeze_expected_population``.
"""

from __future__ import annotations

import csv
from pathlib import Path

from backend.app.core.config import POPULATION_DIR
from backend.app.services.completion_pipeline_service import (
    build_expected_population_from_rows,
)
from backend.app.services.population_status_scope_service import (
    INCLUDED_STATUS,
    LIFECYCLE_CLOSED_IN_MONTH,
    apply_status_scope,
    record_status_scope,
)
from backend.app.services.submission_persistence_service import (
    connect,
    freeze_expected_population,
)


DEFAULT_ENTITY = "2000"


# ---------------------------------------------------------------------------
# Marking the closed lines
# ---------------------------------------------------------------------------


def _mark_closed_lines(population: list, closed_keys: set) -> list:
    """Mark the lines that closed during the reporting month.

    A line that closed during the month stays in that month's
    population, so the month in which the work finished still shows the
    line and its final accepted value.  It is marked as closed so that
    the reader knows it will not be asked again next month.
    """
    for row in population:
        key = (row.po_number, row.po_line)

        if key in closed_keys:
            row.lifecycle_status = LIFECYCLE_CLOSED_IN_MONTH

    return population


# ---------------------------------------------------------------------------
# Source data
# ---------------------------------------------------------------------------


def load_mapping_rows(legal_entity: str) -> list[dict]:
    """Load the cost centre -> department -> manager mapping.

    One row per cost centre.  The mapping table stores one row per
    requester, so the rows are collapsed to one entry per cost centre.
    The manager of a cost centre must be unique; a genuine conflict is
    reported instead of being resolved silently.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cost_center,
                    department,
                    manager,
                    COUNT(*) AS requester_count
                FROM costcenter_mapping
                WHERE legal_entity = %s
                  AND cost_center IS NOT NULL
                GROUP BY cost_center, department, manager
                ORDER BY cost_center
                """,
                (legal_entity,),
            )
            rows = list(cur.fetchall())

    by_cost_center: dict[str, dict] = {}
    conflicts: list[str] = []

    for row in rows:
        cost_center = str(row["cost_center"])
        manager = (row["manager"] or "").strip()

        existing = by_cost_center.get(cost_center)

        if existing is None:
            by_cost_center[cost_center] = {
                "cost_center": cost_center,
                "department": row["department"],
                "manager": manager,
                "requester_count": int(row["requester_count"]),
            }
            continue

        if existing["manager"] != manager:
            conflicts.append(
                f"{cost_center}: {existing['manager']} vs {manager}"
            )
            continue

        existing["requester_count"] += int(row["requester_count"])

    if conflicts:
        raise RuntimeError(
            "A cost centre has more than one manager; the recipient would "
            "be ambiguous: " + "; ".join(sorted(set(conflicts)))
        )

    return list(by_cost_center.values())


def load_po_rows(legal_entity: str) -> list[dict]:
    """Load every PO line of the ERP snapshot, joined to its department.

    The mapping table holds one row per requester, so the join goes
    through a collapsed mapping.  Joining the raw table would multiply
    every PO line by the number of requesters of its cost centre.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.po_number,
                    p.po_line_number AS po_line,
                    p.cost_center,
                    p.description,
                    p.po_line_amount,
                    p.cumulative_gr_amount,
                    p.currency,
                    p.po_status,
                    m.department,
                    m.manager
                FROM capex_po p
                LEFT JOIN (
                    SELECT DISTINCT ON (cost_center)
                        cost_center, department, manager
                    FROM costcenter_mapping
                    WHERE legal_entity = %s
                    ORDER BY cost_center, manager
                ) m ON m.cost_center = p.cost_center
                WHERE p.legal_entity = %s
                ORDER BY p.po_number, p.po_line_number
                """,
                (legal_entity, legal_entity),
            )
            return list(cur.fetchall())


# ---------------------------------------------------------------------------
# Exclusion detection
# ---------------------------------------------------------------------------


def find_exclusions(
    mapping_rows: list[dict],
    po_rows: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Split the source data into the included rows and the exclusions.

    Returns ``(included_rows, exclusions)``.

    A department is excluded when

    * it has no PO line in the ERP snapshot, or
    * it has no manager, so no authorised recipient can be determined.

    PO lines whose cost centre has no mapping entry are also excluded and
    reported separately, because they would otherwise be invisible.
    """
    po_count_by_cost_center: dict[str, int] = {}

    for row in po_rows:
        cost_center = str(row["cost_center"] or "")
        po_count_by_cost_center[cost_center] = (
            po_count_by_cost_center.get(cost_center, 0) + 1
        )

    exclusions: list[dict] = []
    included_rows: list[dict] = []

    mapped_cost_centers = {
        str(row["cost_center"]) for row in mapping_rows
    }

    # 1. Departments from the mapping that have no PO line at all.
    for row in mapping_rows:
        cost_center = str(row["cost_center"])
        count = po_count_by_cost_center.get(cost_center, 0)

        if count == 0:
            exclusions.append(
                {
                    "cost_center": cost_center,
                    "department": row["department"],
                    "manager": row["manager"],
                    "reason": "NO_PO_LINES",
                    "po_line_count": 0,
                }
            )
            continue

        if not row["manager"]:
            exclusions.append(
                {
                    "cost_center": cost_center,
                    "department": row["department"],
                    "manager": "",
                    "reason": "NO_MANAGER",
                    "po_line_count": count,
                }
            )
            continue

    # 2. PO lines whose cost centre is not in the mapping at all.
    unmapped_cost_centers = {
        cost_center
        for cost_center in po_count_by_cost_center
        if cost_center and cost_center not in mapped_cost_centers
    }

    for cost_center in sorted(unmapped_cost_centers):
        exclusions.append(
            {
                "cost_center": cost_center,
                "department": "",
                "manager": "",
                "reason": "COST_CENTER_NOT_IN_MAPPING",
                "po_line_count": po_count_by_cost_center[cost_center],
            }
        )

    excluded_cost_centers = {
        item["cost_center"] for item in exclusions
    }

    for row in po_rows:
        cost_center = str(row["cost_center"] or "")

        if cost_center in excluded_cost_centers:
            continue

        included_rows.append(row)

    return included_rows, exclusions


# ---------------------------------------------------------------------------
# Reporting for finance
# ---------------------------------------------------------------------------


def write_exclusion_report(
    reporting_month: str,
    exclusions: list[dict],
    included_count: int,
) -> Path:
    """Write the exclusion ledger as a CSV for finance.

    The file lists every department that was left out of the frozen
    population and why.  A frozen population is a financial statement
    about who was asked to report, so the exclusions have to be visible to
    the people who sign off on the numbers.
    """
    output_dir = POPULATION_DIR / reporting_month
    output_dir.mkdir(parents=True, exist_ok=True)

    report_path = output_dir / "excluded_departments.csv"

    with report_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "cost_center",
                "department",
                "manager",
                "reason",
                "po_line_count",
            ],
        )
        writer.writeheader()

        for item in sorted(
            exclusions,
            key=lambda entry: (entry["cost_center"], entry["reason"]),
        ):
            writer.writerow(item)

        if not exclusions:
            writer.writerow(
                {
                    "cost_center": "",
                    "department": "",
                    "manager": "",
                    "reason": "NONE",
                    "po_line_count": 0,
                }
            )

    print()
    print(f"Frozen PO lines : {included_count}")
    print(f"Excluded        : {len(exclusions)}")
    print(f"Exclusion report: {report_path}")

    if exclusions:
        print()
        print("EXCLUDED DEPARTMENTS (finance review required):")
        for item in sorted(
            exclusions,
            key=lambda entry: (entry["cost_center"], entry["reason"]),
        ):
            print(
                f"  {item['cost_center']:<12} "
                f"{item['department'] or '-':<32} "
                f"{item['reason']:<28} "
                f"po_lines={item['po_line_count']}"
            )

    return report_path


def record_exclusions(
    reporting_month: str,
    legal_entity: str,
    exclusions: list[dict],
) -> None:
    """Persist the exclusion ledger so it is queryable and auditable.

    Re-running replaces the ledger of that month, so the ledger always
    matches the population that is actually frozen.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM population_exclusions
                WHERE reporting_month = %s
                  AND legal_entity = %s
                """,
                (f"{reporting_month}-01", legal_entity),
            )

            for item in exclusions:
                cur.execute(
                    """
                    INSERT INTO population_exclusions (
                        reporting_month, legal_entity,
                        cost_center, department_name, manager,
                        exclusion_reason, po_line_count
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (
                        reporting_month, legal_entity,
                        cost_center, exclusion_reason
                    )
                    DO UPDATE SET
                        department_name = EXCLUDED.department_name,
                        manager = EXCLUDED.manager,
                        po_line_count = EXCLUDED.po_line_count
                    """,
                    (
                        f"{reporting_month}-01",
                        legal_entity,
                        item["cost_center"],
                        item["department"],
                        item["manager"],
                        item["reason"],
                        item["po_line_count"],
                    ),
                )


def sync_department_contacts(
    reporting_month: str,
    legal_entity: str,
    included_rows: list[dict],
) -> int:
    """Seed the authorised recipient whitelist from the included departments.

    Only departments that are part of the frozen population become
    authorised contacts.  A department that was excluded is never
    authorised, because it is never asked to report.

    The whitelist is rebuilt from scratch for the included departments so
    that a stale contact can never keep receiving requests.
    """
    contacts: dict[tuple[str, str], dict] = {}

    for row in included_rows:
        cost_center = str(row["cost_center"] or "")
        manager = (row["manager"] or "").strip()

        if not cost_center or not manager:
            continue

        key = (cost_center, manager.lower())
        existing = contacts.get(key)

        if existing is None:
            contacts[key] = {
                "cost_center": cost_center,
                "department": row["department"] or "",
                "manager": manager,
            }
            continue

        if existing["department"] and row["department"]:
            existing["department"] = existing["department"]

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM department_contacts
                WHERE legal_entity = %s
                """,
                (legal_entity,),
            )

            for contact in contacts.values():
                cur.execute(
                    """
                    INSERT INTO department_contacts (
                        legal_entity, cost_center, department,
                        authorized_email, active
                    )
                    VALUES (%s, %s, %s, %s, TRUE)
                    ON CONFLICT (legal_entity, cost_center, authorized_email)
                    DO UPDATE SET
                        department = EXCLUDED.department,
                        active = TRUE
                    """,
                    (
                        legal_entity,
                        contact["cost_center"],
                        contact["department"],
                        contact["manager"],
                    ),
                )

    return len(contacts)


def write_population_csv(
    reporting_month: str,
    rows: list[dict],
) -> Path:
    """Write the frozen population itself as a CSV for finance."""
    output_dir = POPULATION_DIR / reporting_month
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / "expected_population.csv"

    fieldnames = [
        "cost_center",
        "department",
        "manager",
        "po_number",
        "po_line",
        "po_status",
        "po_line_amount",
        "cumulative_gr_amount",
        "currency",
        "description",
    ]

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            writer.writerow(
                {
                    "cost_center": row["cost_center"],
                    "department": row["department"],
                    "manager": row["manager"],
                    "po_number": row["po_number"],
                    "po_line": row["po_line"],
                    "po_status": row["po_status"],
                    "po_line_amount": row["po_line_amount"],
                    "cumulative_gr_amount": row["cumulative_gr_amount"],
                    "currency": row["currency"],
                    "description": row["description"],
                }
            )

    return output_path


def write_status_exclusion_report(
    reporting_month: str,
    status_exclusions: list,
    included_count: int,
) -> Path:
    """Write the filtered PO lines as a CSV for finance.

    These are the individual PO lines that the status filter removed.
    They are reported separately from the excluded departments, because a
    department can be in the reporting scope and still have some of its
    lines filtered out.
    """
    output_dir = POPULATION_DIR / reporting_month
    output_dir.mkdir(parents=True, exist_ok=True)

    report_path = output_dir / "excluded_po_lines.csv"

    fieldnames = [
        "cost_center",
        "department",
        "manager",
        "po_number",
        "po_line",
        "po_status",
        "exclusion_reason",
        "po_line_amount",
        "currency",
        "last_active_month",
        "description",
    ]

    with report_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()

        for item in sorted(
            status_exclusions,
            key=lambda entry: (entry.cost_center, entry.po_number, entry.po_line),
        ):
            writer.writerow({
                "cost_center": item.cost_center,
                "department": item.department_name,
                "manager": item.manager,
                "po_number": item.po_number,
                "po_line": item.po_line,
                "po_status": item.po_status,
                "exclusion_reason": item.exclusion_reason,
                "po_line_amount": item.po_line_amount,
                "currency": item.currency,
                "last_active_month": (
                    item.last_active_month.isoformat()
                    if item.last_active_month
                    else ""
                ),
                "description": item.po_description,
            })

    print(f"Status scope    : {INCLUDED_STATUS} only")
    print(f"Filtered lines  : {len(status_exclusions)}")
    print(f"Line report     : {report_path}")

    if status_exclusions:
        print()
        print("FILTERED PO LINES (finance review required):")
        for item in sorted(
            status_exclusions,
            key=lambda entry: (entry.po_status, entry.cost_center),
        ):
            print(
                f"  {item.po_status:<9} "
                f"{item.cost_center:<12} "
                f"{item.po_number}/{item.po_line:<4} "
                f"{item.exclusion_reason}"
            )

    return report_path


# ---------------------------------------------------------------------------
# The freeze itself
# ---------------------------------------------------------------------------


def freeze_population(
    reporting_month: str,
    legal_entity: str = DEFAULT_ENTITY,
    dry_run: bool = False,
) -> dict:
    """Freeze the expected population for one reporting month.

    Returns a summary of what was frozen, so that the caller can report
    it without querying the database again.
    """
    print(f"Reporting month: {reporting_month}")
    print(f"Legal entity   : {legal_entity}")

    mapping_rows = load_mapping_rows(legal_entity)
    po_rows = load_po_rows(legal_entity)

    print(f"Mapping entries: {len(mapping_rows)}")
    print(f"ERP PO lines   : {len(po_rows)}")

    # The department scope: a department without PO lines has nothing to
    # report on and is excluded.
    included_rows, exclusions = find_exclusions(mapping_rows, po_rows)

    # The PO status scope: only the Open lines are in the reporting
    # scope.  The filtered lines go on the ledger.
    scope = apply_status_scope(reporting_month, included_rows, legal_entity)

    closed_keys = {
        (item.po_number, item.po_line) for item in scope.closed_in_month
    }

    status_rows = [
        row
        for row in included_rows
        if (str(row["po_number"]), int(row["po_line"])) not in closed_keys
        and (row.get("po_status") or "").strip().lower()
        == INCLUDED_STATUS.lower()
    ]

    # A line that closed during this reporting month stays in that month's
    # population, marked as closed, so the month in which the work
    # finished still shows the line and its final accepted value.
    population_source_rows = list(status_rows) + [
        row
        for row in included_rows
        if (str(row["po_number"]), int(row["po_line"])) in closed_keys
    ]

    population = build_expected_population_from_rows(
        population_source_rows,
        reporting_month,
        legal_entity,
    )

    # Mark the lines that closed during this month.
    population = _mark_closed_lines(population, closed_keys)

    baseline_present = sum(
        1 for row in population if row.baseline_status == "PRESENT"
    )
    print(
        f"Population rows: {len(population)} "
        f"(baseline present: {baseline_present}, "
        f"missing: {len(population) - baseline_present})"
    )

    write_exclusion_report(
        reporting_month,
        exclusions,
        len(population),
    )
    write_status_exclusion_report(
        reporting_month,
        scope.excluded,
        len(population),
    )
    population_csv = write_population_csv(
        reporting_month,
        population_source_rows,
    )
    print(f"Population CSV  : {population_csv}")

    summary = {
        "reporting_month": reporting_month,
        "legal_entity": legal_entity,
        "frozen_line_count": len(population),
        "baseline_present_count": baseline_present,
        "excluded_department_count": len(exclusions),
        "filtered_line_count": len(scope.excluded),
        "closed_in_month_count": len(scope.closed_in_month),
        "population_csv": str(population_csv),
        "exclusions": exclusions,
    }

    if dry_run:
        print()
        print("DRY RUN: nothing was written to the database.")
        summary["frozen"] = False
        return summary

    frozen = freeze_expected_population(
        reporting_month,
        population,
        source="Mock ERP + costcenter_mapping",
        entity=legal_entity,
    )

    record_exclusions(reporting_month, legal_entity, exclusions)
    record_status_scope(reporting_month, legal_entity, scope)

    contacts = sync_department_contacts(
        reporting_month,
        legal_entity,
        population_source_rows,
    )

    print()
    print(f"FROZEN: {frozen} PO lines for {reporting_month}")
    print(f"AUTHORIZED CONTACTS: {contacts} department contacts")

    summary["frozen"] = True
    summary["authorized_contact_count"] = contacts

    return summary
