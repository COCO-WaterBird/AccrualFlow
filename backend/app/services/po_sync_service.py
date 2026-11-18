
import argparse
import hashlib
import json
import os
from datetime import date
from decimal import Decimal

import psycopg
from psycopg import sql
from dotenv import load_dotenv

from backend.app.services.mock_erp_client import MockERPClient
from backend.app.core.config import (
    ACCRUALFLOW_DB_NAME,
    DB_HOST,
    DB_PORT,
    DB_USER,
)


load_dotenv()

FIELDS = [
    "legal_entity",
    "po_number",
    "po_line_number",
    "po_status",
    "supplier_code",
    "supplier_name",
    "requester",
    "buyer",
    "cost_center",
    "budget_code",
    "description",
    "currency",
    "po_amount",
    "po_line_amount",
    "cumulative_gr_amount",
    "cumulative_invoice_amount",
    "completion_progress",
    "monthly_completion_estimate_pct",
    "po_note",
]

DECIMAL_FIELDS = {
    "po_amount",
    "po_line_amount",
    "cumulative_gr_amount",
    "cumulative_invoice_amount",
    "completion_progress",
    "monthly_completion_estimate_pct",
}


def parse_reporting_month(value):
    try:
        month = date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise ValueError(
            "Reporting month must use YYYY-MM format"
        ) from exc

    return month


def fetch_all_purchase_orders():
    client = MockERPClient()
    records = []
    offset = 0
    limit = 100
    expected_total = None

    while True:
        result = client.get_purchase_orders(
            limit=limit,
            offset=offset,
        )

        if expected_total is None:
            expected_total = result["total"]
        elif result["total"] != expected_total:
            raise RuntimeError(
                "ERP total changed during pagination"
            )

        page = result["data"]

        if not page:
            break

        records.extend(page)
        offset += len(page)

        print(
            f"Fetched {len(records)} / "
            f"{expected_total} PO Lines"
        )

        if offset >= expected_total:
            break

    if not records:
        raise ValueError("ERP returned no PO records")

    if len(records) != expected_total:
        raise ValueError(
            "ERP pagination returned incomplete data"
        )

    keys = [
        (
            str(record["po_number"]),
            int(record["po_line_number"]),
        )
        for record in records
    ]

    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate PO Lines from ERP")

    for record in records:
        missing = set(FIELDS) - set(record)

        if missing:
            raise ValueError(
                f"ERP record missing fields: {missing}"
            )

    return records


def normalize_record(record):
    normalized = {}

    for field in FIELDS:
        value = record[field]

        if value is not None and field in DECIMAL_FIELDS:
            value = Decimal(str(value))

        if field == "po_line_number":
            value = int(value)

        normalized[field] = value

    return normalized


def calculate_data_hash(records):
    canonical = json.dumps(
        records,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


def sync_purchase_orders(reporting_month):
    """Pull the ERP master data into the AccrualFlow database.

    The import is recorded in ``import_batch`` so that the dashboard can
    show when the data was last pulled and how many rows it contained.

    The import is idempotent: an identical file that was already
    imported successfully creates no second batch, and the existing
    batch is reported back instead.

    Returns a summary dictionary.
    """
    password = os.getenv("ACCRUALFLOW_DB_PASSWORD")

    if not password:
        raise RuntimeError(
            "ACCRUALFLOW_DB_PASSWORD is not configured"
        )

    month = parse_reporting_month(reporting_month)

    # Fetch the full ERP dataset before opening
    # the AccrualFlow database transaction.
    raw_records = fetch_all_purchase_orders()

    records = [
        normalize_record(record)
        for record in raw_records
    ]

    records.sort(
        key=lambda r: (
            r["po_number"],
            r["po_line_number"],
        )
    )

    data_hash = calculate_data_hash(records)

    print(f"Reporting Month: {reporting_month}")
    print(f"PO Lines: {len(records)}")
    print(f"Data SHA-256: {data_hash}")

    with psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=ACCRUALFLOW_DB_NAME,
        user=DB_USER,
        password=password,
    ) as conn:

        with conn.cursor() as cur:

            # Prevent concurrent sync jobs.
            cur.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (20261109,),
            )

            # Detect a previously completed identical batch.
            cur.execute(
                """
                SELECT batch_id, imported_at, imported_rows
                FROM import_batch
                WHERE reporting_month = %s
                  AND source_system = %s
                  AND file_hash = %s
                  AND status = 'SUCCESS'
                LIMIT 1
                """,
                (month, "Mock ERP", data_hash),
            )

            existing = cur.fetchone()

            if existing:
                print(
                    "Identical successful batch already exists: "
                    f"{existing[0]}"
                )
                print("No duplicate history created.")

                return {
                    "reporting_month": reporting_month,
                    "batch_id": existing[0],
                    "row_count": existing[2] or len(records),
                    "data_hash": data_hash,
                    "imported_at": (
                        existing[1].isoformat()
                        if existing[1]
                        else None
                    ),
                    "reused_existing_batch": True,
                }

            # Create a new import batch.
            cur.execute(
                """
                INSERT INTO import_batch (
                    reporting_month,
                    source_system,
                    source_filename,
                    file_hash,
                    status,
                    total_rows
                )
                VALUES (%s, %s, %s, %s, 'PENDING', %s)
                RETURNING batch_id
                """,
                (
                    month,
                    "Mock ERP",
                    "Mock ERP API",
                    data_hash,
                    len(records),
                ),
            )

            batch_id = cur.fetchone()[0]

            # Save immutable historical PO snapshots.
            history_values = [
                (
                    batch_id,
                    record["po_number"],
                    record["po_line_number"],
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        default=str,
                    ),
                )
                for record in records
            ]

            cur.executemany(
                """
                INSERT INTO capex_po_history (
                    batch_id,
                    po_number,
                    po_line_number,
                    po_data
                )
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                history_values,
            )

            # Replace current PO snapshot.
            cur.execute("DELETE FROM capex_po")

            columns = sql.SQL(", ").join(
                sql.Identifier(field)
                for field in FIELDS
            )

            placeholders = sql.SQL(", ").join(
                sql.Placeholder()
                for _ in FIELDS
            )

            statement = sql.SQL(
                "INSERT INTO capex_po ({}) VALUES ({})"
            ).format(columns, placeholders)

            values = [
                tuple(record[field] for field in FIELDS)
                for record in records
            ]

            cur.executemany(statement, values)

            cur.execute(
                "SELECT COUNT(*) FROM capex_po"
            )

            actual_count = cur.fetchone()[0]

            if actual_count != len(records):
                raise RuntimeError(
                    f"PO count mismatch: "
                    f"{actual_count} != {len(records)}"
                )

            # Mark batch as successfully completed.
            cur.execute(
                """
                UPDATE import_batch
                SET status = 'SUCCESS',
                    completed_at = NOW(),
                    imported_rows = %s
                WHERE batch_id = %s
                """,
                (actual_count, batch_id),
            )

            print(f"Batch ID: {batch_id}")
            print(f"Historical PO Lines: {len(records)}")
            print(f"Current PO Lines: {actual_count}")

    print("AccrualFlow PO Sync completed successfully!")

    return {
        "reporting_month": reporting_month,
        "batch_id": batch_id,
        "row_count": actual_count,
        "data_hash": data_hash,
        "imported_at": None,
        "reused_existing_batch": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--month",
        required=True,
        help="Financial reporting month, YYYY-MM",
    )

    args = parser.parse_args()

    sync_purchase_orders(args.month)
