
CREATE TABLE IF NOT EXISTS capex_po (
    legal_entity VARCHAR(20),
    po_number VARCHAR(30) NOT NULL,
    po_line_number INTEGER NOT NULL,
    po_status VARCHAR(30),
    supplier_code VARCHAR(50),
    supplier_name TEXT,
    requester VARCHAR(100),
    buyer VARCHAR(100),
    cost_center VARCHAR(50),
    budget_code VARCHAR(50),
    description TEXT,
    currency VARCHAR(10),
    po_amount NUMERIC(18, 2),
    po_line_amount NUMERIC(18, 2),
    cumulative_gr_amount NUMERIC(18, 2),
    cumulative_invoice_amount NUMERIC(18, 2),
    completion_progress NUMERIC(8, 6),
    monthly_completion_estimate_pct NUMERIC(8, 6),
    po_note TEXT,
    synced_at TIMESTAMPTZ DEFAULT NOW(),

    PRIMARY KEY (po_number, po_line_number)
);
