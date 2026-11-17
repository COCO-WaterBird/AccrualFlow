CREATE UNIQUE INDEX IF NOT EXISTS
    uq_import_batch_source_month_hash_success
ON import_batch (
    source_system,
    reporting_month,
    file_hash
)
WHERE status = 'SUCCESS'
  AND file_hash IS NOT NULL;
