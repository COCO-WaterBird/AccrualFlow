
CREATE TABLE IF NOT EXISTS reminder_attachment_archive (
    attachment_id BIGSERIAL PRIMARY KEY,

    batch_id BIGINT NOT NULL
        REFERENCES import_batch(batch_id),

    reporting_month DATE NOT NULL,

    legal_entity VARCHAR(50) NOT NULL,
    manager VARCHAR(255) NOT NULL,
    cost_center VARCHAR(100) NOT NULL,
    department VARCHAR(255),

    file_name VARCHAR(255) NOT NULL,
    file_path TEXT NOT NULL UNIQUE,
    file_hash VARCHAR(64) NOT NULL,
    file_size_bytes BIGINT NOT NULL,

    po_line_count INTEGER NOT NULL,

    version INTEGER NOT NULL DEFAULT 1,

    status VARCHAR(30) NOT NULL DEFAULT 'GENERATED'
        CHECK (status IN (
            'GENERATED',
            'SENT',
            'SUPERSEDED'
        )),

    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ,

    UNIQUE (
        reporting_month,
        legal_entity,
        manager,
        cost_center,
        version
    ),

    CHECK (EXTRACT(DAY FROM reporting_month) = 1),
    CHECK (file_size_bytes >= 0),
    CHECK (po_line_count > 0),
    CHECK (version > 0)
);

CREATE INDEX IF NOT EXISTS idx_reminder_archive_month
ON reminder_attachment_archive(reporting_month);

CREATE INDEX IF NOT EXISTS idx_reminder_archive_batch
ON reminder_attachment_archive(batch_id);
