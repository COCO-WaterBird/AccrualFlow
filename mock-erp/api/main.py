
import os
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Query


app = FastAPI(
    title="AccrualFlow Mock ERP API",
    description="Mock ERP API for CapEx Purchase Orders",
    version="0.1.0",
)


def get_db_connection():
    """Connect to Mock ERP PostgreSQL database."""

    password = os.getenv("ACCRUALFLOW_DB_PASSWORD")

    if not password:
        raise RuntimeError(
            "ACCRUALFLOW_DB_PASSWORD is not configured"
        )

    return psycopg.connect(
        host="localhost",
        port=5433,
        dbname="mock_erp_db",
        user="accrualflow",
        password=password,
        row_factory=dict_row,
    )


@app.get("/")
def root():
    return {
        "application": "AccrualFlow Mock ERP",
        "status": "running",
    }


@app.get("/api/v1/purchase-orders")
def get_purchase_orders(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """
    Retrieve CapEx PO Lines with pagination.
    """

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:

                cur.execute(
                    "SELECT COUNT(*) AS total FROM capex_po"
                )
                total = cur.fetchone()["total"]

                cur.execute(
                    """
                    SELECT *
                    FROM capex_po
                    ORDER BY po_number, po_line_number
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )

                records = cur.fetchall()

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "data": records,
        }

    except (psycopg.Error, RuntimeError) as exc:
        # Log details locally, but avoid exposing
        # database credentials or internals to clients.
        print(f"Mock ERP database error: {type(exc).__name__}")

        raise HTTPException(
            status_code=503,
            detail="Mock ERP database unavailable",
        ) from exc
