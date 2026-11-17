
from pathlib import Path
from collections import Counter
from datetime import datetime
import hashlib
import os
import shutil

import openpyxl
import psycopg
from psycopg import sql
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MOCK_ERP_ROOT = PROJECT_ROOT / "mock-erp"

EXCEL_PATH = MOCK_ERP_ROOT / "data" / "Capex_PO.xlsx"
BACKUP_DIR = MOCK_ERP_ROOT / "backups"

load_dotenv(PROJECT_ROOT / ".env")

DB_CONFIG = {
    "host": "localhost",
    "port": 5433,
    "dbname": "mock_erp_db",
    "user": "accrualflow",
    "password": os.getenv("ACCRUALFLOW_DB_PASSWORD"),
}


def read_excel():
    if not EXCEL_PATH.exists():
        raise FileNotFoundError(EXCEL_PATH)

    wb = openpyxl.load_workbook(
        EXCEL_PATH,
        read_only=True,
        data_only=True,
    )

    try:
        rows = wb.active.iter_rows(values_only=True)
        headers = [
            str(value).strip()
            for value in next(rows)
        ]

        if len(headers) != len(set(headers)):
            raise ValueError("Duplicate Excel column names")

        records = [
            dict(zip(headers, row))
            for row in rows
            if any(value is not None for value in row)
        ]

        return headers, records

    finally:
        wb.close()


def validate(headers, records, db_columns):
    if not records:
        raise ValueError("Excel contains no PO records")

    required = {
        "po_number",
        "po_line_number",
        "po_status",
    }

    missing = required - set(headers)

    if missing:
        raise ValueError(f"Missing columns: {missing}")

    unknown = set(headers) - set(db_columns)

    if unknown:
        raise ValueError(f"Unknown database columns: {unknown}")

    if any(
        record["po_number"] is None
        or record["po_line_number"] is None
        for record in records
    ):
        raise ValueError("PO Number or PO Line is missing")

    keys = [
        (
            str(record["po_number"]),
            int(record["po_line_number"]),
        )
        for record in records
    ]

    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate PO Number + PO Line")

    return len(records)


def backup_excel():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    file_hash = hashlib.sha256(
        EXCEL_PATH.read_bytes()
    ).hexdigest()[:12]

    backup_path = (
        BACKUP_DIR
        / f"Capex_PO_{timestamp}_{file_hash}.xlsx"
    )

    shutil.copy2(EXCEL_PATH, backup_path)

    print(f"Source Excel archived: {backup_path}")


def import_po():
    if not DB_CONFIG["password"]:
        raise RuntimeError(
            "ACCRUALFLOW_DB_PASSWORD is not configured"
        )

    headers, records = read_excel()

    # Archive the source Excel before updating the database.
    backup_excel()

    with psycopg.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'capex_po'
                ORDER BY ordinal_position
            """)

            db_columns = [
                row[0] for row in cur.fetchall()
            ]

            if not db_columns:
                raise RuntimeError("capex_po table not found")

            total = validate(headers, records, db_columns)

            cur.execute("SELECT COUNT(*) FROM capex_po")
            old_count = cur.fetchone()[0]

            # Keep a database-level backup in the same transaction.
            # It is replaced on each successful import.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS
                capex_po_pre_refresh_backup
                (LIKE capex_po INCLUDING ALL)
            """)

            cur.execute(
                "TRUNCATE TABLE capex_po_pre_refresh_backup"
            )

            cur.execute("""
                INSERT INTO capex_po_pre_refresh_backup
                SELECT * FROM capex_po
            """)

            # Full refresh, preserving the table schema.
            cur.execute("DELETE FROM capex_po")

            statement = sql.SQL(
                "INSERT INTO {} ({}) VALUES ({})"
            ).format(
                sql.Identifier("capex_po"),
                sql.SQL(", ").join(
                    map(sql.Identifier, headers)
                ),
                sql.SQL(", ").join(
                    sql.Placeholder() for _ in headers
                ),
            )

            values = [
                tuple(record[col] for col in headers)
                for record in records
            ]

            cur.executemany(statement, values)

            cur.execute("SELECT COUNT(*) FROM capex_po")
            new_count = cur.fetchone()[0]

            if new_count != total:
                raise RuntimeError(
                    f"Import mismatch: {new_count} != {total}"
                )

            statuses = Counter(
                str(record["po_status"]).strip()
                for record in records
            )

            print(f"Previous PO lines: {old_count}")
            print(f"Imported PO lines: {new_count}")
            print(f"PO Status counts: {dict(statuses)}")

        # Commit after all database operations succeed.

    print("Mock ERP Full Refresh completed successfully!")


if __name__ == "__main__":
    import_po()
