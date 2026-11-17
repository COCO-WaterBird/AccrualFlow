
CREATE TABLE IF NOT EXISTS import_batch (
    batch_id BIGSERIAL PRIMARY KEY,

    reporting_month DATE NOT NULL,

    source_system VARCHAR(50) NOT NULL DEFAULT 'Mock ERP',

    source_filename VARCHAR(255) NOT NULL,

    file_hash VARCHAR(64),

    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    completed_at TIMESTAMPTZ,

    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',

    total_rows INTEGER,

    imported_rows INTEGER,

    error_message TEXT,

    CONSTRAINT chk_import_batch_status
        CHECK (
            status IN (
                'PENDING',
                'SUCCESS',
                'FAILED'
            )
        ),

    CONSTRAINT chk_reporting_month
        CHECK (
            EXTRACT(DAY FROM reporting_month) = 1
        )
);

CREATE INDEX IF NOT EXISTS idx_import_batch_month
ON import_batch (reporting_month, imported_at DESC);
