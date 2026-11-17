
import argparse
import hashlib
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path

import psycopg
from openpyxl import load_workbook

from backend.app.core.config import (
    DB_HOST,
    DB_PORT,
    DB_USER,
    DB_PASSWORD,
)

SOURCE_ROOT = Path("backend/output/reminders")
ARCHIVE_ROOT = Path("backend/archive/reminders")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_po_count(path):
    wb = load_workbook(path, read_only=True)
    try:
        ws = wb.active
        headers = [cell.value for cell in ws[1]]

        if headers[:2] != ["PO Number", "PO Line"]:
            raise ValueError(f"Invalid PO columns: {path}")

        keys = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row[0] is not None:
                keys.append((str(row[0]), str(row[1])))

        if len(keys) != len(set(keys)):
            raise ValueError(f"Duplicate PO lines in {path}")

        return len(keys)
    finally:
        wb.close()


def archive_reminders(reporting_month):
    month_date = date.fromisoformat(reporting_month + "-01")
    source_dir = SOURCE_ROOT / reporting_month

    files = sorted(source_dir.glob("*.xlsx"))
    if not files:
        raise RuntimeError(f"No Excel files in {source_dir}")

    with psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname="accrualflow_db",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT batch_id
                FROM import_batch
                WHERE reporting_month = %s
                  AND status = 'SUCCESS'
                ORDER BY completed_at DESC NULLS LAST,
                         batch_id DESC
                LIMIT 1
                """,
                (month_date,),
            )
            result = cur.fetchone()
            if result is None:
                raise RuntimeError(
                    f"No successful batch for {reporting_month}"
                )
            batch_id = result[0]

            cur.execute(
                """
                SELECT DISTINCT
                    legal_entity, manager,
                    cost_center, department
                FROM costcenter_mapping
                WHERE manager IS NOT NULL
                """
            )
            mappings = cur.fetchall()

            mapping_by_key = {}
            for entity, manager, cc, department in mappings:
                key = (str(entity), str(cc))
                if key in mapping_by_key:
                    raise RuntimeError(
                        f"Ambiguous manager mapping: {key}"
                    )
                mapping_by_key[key] = (manager, department)

            archived = 0
            skipped = 0

            for source in files:
                # File name:
                # YYYY-MM_entity_costcenter_department.xlsx
                prefix = reporting_month + "_"
                if not source.name.startswith(prefix):
                    raise ValueError(
                        f"Unexpected filename: {source.name}"
                    )

                parts = source.stem[len(prefix):].split("_", 2)
                if len(parts) != 3:
                    raise ValueError(
                        f"Invalid filename: {source.name}"
                    )

                entity, cost_center, _ = parts
                key = (entity, cost_center)

                if key not in mapping_by_key:
                    raise RuntimeError(
                        f"No manager mapping for {key}"
                    )

                manager, department = mapping_by_key[key]
                file_hash = sha256_file(source)
                file_size = source.stat().st_size
                po_count = get_po_count(source)

                if po_count == 0:
                    raise ValueError(
                        f"Empty reminder Excel: {source.name}"
                    )

                # Serialize version allocation for this group.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"reminder:{reporting_month}:{entity}:"
                     f"{manager}:{cost_center}",),
                )

                cur.execute(
                    """
                    SELECT attachment_id, version
                    FROM reminder_attachment_archive
                    WHERE reporting_month = %s
                      AND legal_entity = %s
                      AND manager = %s
                      AND cost_center = %s
                      AND file_hash = %s
                    ORDER BY version DESC
                    LIMIT 1
                    """,
                    (
                        month_date, entity, manager,
                        cost_center, file_hash,
                    ),
                )
                existing = cur.fetchone()

                if existing:
                    print(
                        f"SKIP: {source.name} "
                        f"(existing attachment {existing[0]}, "
                        f"v{existing[1]})"
                    )
                    skipped += 1
                    continue

                cur.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) + 1
                    FROM reminder_attachment_archive
                    WHERE reporting_month = %s
                      AND legal_entity = %s
                      AND manager = %s
                      AND cost_center = %s
                    """,
                    (month_date, entity, manager, cost_center),
                )
                version = cur.fetchone()[0]

                destination_dir = (
                    ARCHIVE_ROOT / reporting_month
                    / f"v{version}"
                )
                destination_dir.mkdir(
                    parents=True, exist_ok=True
                )
                destination = destination_dir / source.name

                # Never overwrite an archived file.
                if destination.exists():
                    raise FileExistsError(
                        f"Archive already exists: {destination}"
                    )

                # Stage a byte-for-byte copy.
                fd, temp_name = tempfile.mkstemp(
                    prefix=".archive_",
                    suffix=".tmp",
                    dir=destination_dir,
                )
                os.close(fd)
                temp_path = Path(temp_name)

                try:
                    shutil.copyfile(source, temp_path)

                    if sha256_file(temp_path) != file_hash:
                        raise RuntimeError(
                            f"Hash mismatch: {source.name}"
                        )

                    # Exclusive creation prevents overwriting.
                    with open(temp_path, "rb") as src:
                        with open(destination, "xb") as dst:
                            shutil.copyfileobj(src, dst)

                    if sha256_file(destination) != file_hash:
                        raise RuntimeError(
                            f"Archive verification failed: "
                            f"{destination}"
                        )

                    cur.execute(
                        """
                        INSERT INTO reminder_attachment_archive (
                            batch_id,
                            reporting_month,
                            legal_entity,
                            manager,
                            cost_center,
                            department,
                            file_name,
                            file_path,
                            file_hash,
                            file_size_bytes,
                            po_line_count,
                            version
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s
                        )
                        RETURNING attachment_id
                        """,
                        (
                            batch_id, month_date, entity,
                            manager, cost_center, department,
                            source.name,
                            str(destination.resolve()),
                            file_hash, file_size,
                            po_count, version,
                        ),
                    )
                    attachment_id = cur.fetchone()[0]

                except Exception:
                    # Remove an incomplete new archive copy.
                    destination.unlink(missing_ok=True)
                    raise
                finally:
                    temp_path.unlink(missing_ok=True)

                print(
                    f"ARCHIVED: {source.name} "
                    f"-> attachment {attachment_id}, v{version}"
                )
                archived += 1

            print(
                f"\nBatch: {batch_id} | "
                f"Archived: {archived} | Skipped: {skipped}"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--month", required=True,
        help="Reporting month in YYYY-MM format",
    )
    args = parser.parse_args()
    archive_reminders(args.month)
