"""
Freeze the expected PO line population for one reporting month.

This is the command line entry point.  The logic itself lives in
``backend/app/services/population_freeze_service.py``, because the
Airflow container mounts only the ``backend`` directory and the monthly
run calls the freeze from inside the DAG.

Usage
-----
    python3 -m scripts.freeze_population --month 2026-11
    python3 -m scripts.freeze_population --month 2026-11 --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.services.population_freeze_service import (  # noqa: E402
    DEFAULT_ENTITY,
    find_exclusions,
    freeze_population,
    load_mapping_rows,
    load_po_rows,
)


__all__ = [
    "DEFAULT_ENTITY",
    "find_exclusions",
    "freeze_population",
    "load_mapping_rows",
    "load_po_rows",
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze the expected PO line population for a reporting month."
        )
    )
    parser.add_argument(
        "--month",
        required=True,
        help="Reporting month in YYYY-MM format.",
    )
    parser.add_argument(
        "--entity",
        default=DEFAULT_ENTITY,
        help=f"Legal entity (default: {DEFAULT_ENTITY}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be frozen without writing to the database.",
    )

    args = parser.parse_args()

    freeze_population(
        reporting_month=args.month,
        legal_entity=args.entity,
        dry_run=args.dry_run,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
