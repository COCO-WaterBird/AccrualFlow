-- AccrualFlow Mock ERP Database
-- Version 1.0

CREATE TABLE IF NOT EXISTS capex_po (
    legal_entity VARCHAR(20),
    po_number VARCHAR(30) NOT NULL,
    po_line_number INTEGER NOT NULL,
    po_status VARCHAR(30),
    supplier_code VARCHAR(30),
    supplier_name VARCHAR(200),
    requester VARCHAR(100),
    buyer VARCHAR(100),
    cost_center VARCHAR(30),
    budget_code VARCHAR(30),
    description TEXT,
    currency VARCHAR(3) DEFAULT 'USD',
    po_amount NUMERIC(18,2),
    po_line_amount NUMERIC(18,2),
    cumulative_gr_amount NUMERIC(18,2),
    cumulative_invoice_amount NUMERIC(18,2),
    completion_progress NUMERIC(10,4),
    monthly_completion_estimate_pct NUMERIC(10,4),
    po_note TEXT,

    PRIMARY KEY (po_number, po_line_number)
);

CREATE TABLE IF NOT EXISTS costcenter_mapping (
    legal_entity VARCHAR(20),
    employee_email_prefix VARCHAR(100),
    cost_center VARCHAR(30),
    department VARCHAR(100),
    role VARCHAR(50),
    manager VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS idx_capex_requester
ON capex_po (legal_entity, requester);

CREATE INDEX IF NOT EXISTS idx_employee_lookup
ON costcenter_mapping (legal_entity, employee_email_prefix);
