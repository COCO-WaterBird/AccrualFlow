
CREATE TABLE IF NOT EXISTS capex_po_history (
    history_id BIGSERIAL PRIMARY KEY,

    batch_id BIGINT NOT NULL
        REFERENCES import_batch(batch_id),

    po_number VARCHAR(100) NOT NULL,
    po_line_number INTEGER NOT NULL,

    po_data JSONB NOT NULL,

    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_po_history_batch_line
        UNIQUE (batch_id, po_number, po_line_number)
);

CREATE INDEX IF NOT EXISTS idx_po_history_po
ON capex_po_history (po_number, po_line_number);

CREATE INDEX IF NOT EXISTS idx_po_history_batch
ON capex_po_history (batch_id);
