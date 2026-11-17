from pathlib import Path
from collections import defaultdict
from decimal import Decimal
import os

import openpyxl
import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

FILES = {
    "capex_po" : DATA / "Capex_PO.xlsx",
    "costcenter_mapping" : DATA / "Costcenter_Mapping.xlsx",
}

def read_excel(path):
    wb = openpyxl.load_workbook(path, read_only = True, data_only = True)
    try:
        rows = wb.active.iter_rows(values_only=True)
        headers = list(next(rows))
        records = [
            dict(zip(headers,row))
            for row in rows
            if any(v is not None for v in row)
        ]
        return headers, records
    finally:
        wb.close()

def validate(po_rows, mapping_rows):
    po_keys = [
        (
            str(row["po_number"]),
            int(row["po_line_number"])
        )
        for row in po_rows
    ]

    if len(po_keys) != len(set(po_keys)):
        raise ValueError(
            "Duplicate PO Line Number found"
            "within the same PO Number."
        )

def insert_rows(cur, table, columns, records):
    """Insert Excel records into a PostgreSQL table."""

    statement = sql.SQL(
        "INSERT INTO {} ({}) VALUES ({})"
    ).format(
        sql.Identifier(table),
        sql.SQL(", ").join(
            map(sql.Identifier, columns)
        ),
        sql.SQL(", ").join(
            sql.Placeholder() for _ in columns
        )
    )

    values = [
        tuple(row[col] for col in columns)
        for row in records
    ]

    if values:
        cur.executemany(statement, values)

    print(f"Inserted {len(values)} rows into {table}")


# ============================================
# 5. Main Import Workflow
# ============================================

def main():

    print("Starting Mock ERP Excel import...")

    # Step 1: Read Excel files
    datasets = {}

    for table, path in FILES.items():

        if not path.exists():
            raise FileNotFoundError(
                f"Excel file not found: {path}"
            )

        datasets[table] = read_excel(path)

    po_columns, po_rows = datasets["capex_po"]

    mapping_columns, mapping_rows = (
        datasets["costcenter_mapping"]
    )

    # Step 2: Validate PO Line uniqueness
    validate(po_rows, mapping_rows)

    # Step 3: Read database password
    password = os.environ.get(
        "ACCRUALFLOW_DB_PASSWORD"
    )

    if not password:
        raise RuntimeError(
            "Set ACCRUALFLOW_DB_PASSWORD first."
        )

    # Step 4: Connect to Mock ERP database
    with psycopg.connect(
        host="localhost",
        port=5433,
        dbname="mock_erp_db",
        user="accrualflow",
        password=password
    ) as conn:

        with conn.cursor() as cur:

            # Prevent accidental duplicate imports
            for table in FILES:

                cur.execute(
                    sql.SQL(
                        "SELECT COUNT(*) FROM {}"
                    ).format(
                        sql.Identifier(table)
                    )
                )

                count = cur.fetchone()[0]

                if count > 0:
                    raise RuntimeError(
                        f"{table} already contains "
                        f"{count} rows. Import stopped."
                    )

            # Step 5: Import CapEx PO data
            insert_rows(
                cur,
                "capex_po",
                po_columns,
                po_rows
            )

            # Step 6: Import employee mapping data
            insert_rows(
                cur,
                "costcenter_mapping",
                mapping_columns,
                mapping_rows
            )

        # Transaction commits automatically
        # when the connection context exits
        # without an exception.

    print("Import committed successfully!")


# ============================================
# 6. Program Entry Point
# ============================================

if __name__ == "__main__":
    main()