
import os
from pathlib import Path
import psycopg
from openpyxl import load_workbook
from backend.app.core.config import (
    DB_HOST,
    DB_PORT,
    DB_USER,
    DB_NAME,
    DB_PASSWORD,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]

EXCEL_PATH = (
    PROJECT_ROOT
    / "backend"
    / "data"
    / "Costcenter_Mapping.xlsx"
)

FIELDS = [
    "legal_entity",
    "employee_email_prefix",
    "cost_center",
    "department",
    "role",
    "manager",
]


def import_mapping():
    password = os.getenv("ACCRUALFLOW_DB_PASSWORD")

    if not password:
        raise RuntimeError(
            "ACCRUALFLOW_DB_PASSWORD is not configured"
        )

    wb = load_workbook(EXCEL_PATH, read_only=True, data_only=True)

    try:
        ws = wb.active
        rows = ws.iter_rows(values_only=True)

        headers = [str(value).strip() for value in next(rows)]
        missing = set(FIELDS) - set(headers)

        if missing:
            raise ValueError(f"Missing Excel columns: {missing}")

        indexes = [headers.index(field) for field in FIELDS]

        records = [
            tuple(
                str(row[i]).strip() if row[i] is not None else None
                for i in indexes
            )
            for row in rows
            if any(value is not None for value in row)
        ]
    finally:
        wb.close()

    if not records:
        raise ValueError("Mapping Excel contains no records")

    with psycopg.connect(
        host="localhost",
        port=5433,
        dbname="accrualflow_db",
        user="accrualflow",
        password=password,
    ) as conn:

        with conn.cursor() as cur:
            cur.execute("DELETE FROM costcenter_mapping")

            cur.executemany(
                """
                INSERT INTO costcenter_mapping (
                    legal_entity,
                    employee_email_prefix,
                    cost_center,
                    department,
                    role,
                    manager
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                records,
            )

    print(f"Imported {len(records)} mapping records")


if __name__ == "__main__":
    import_mapping()
