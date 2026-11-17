-- ===========================================================================
-- AccrualFlow MVP - reporting periods, expected PO line population,
-- department contacts and monthly completion baselines.
--
-- The expected population is a FROZEN snapshot per reporting period.  It is
-- the authority for completeness checking: a PO line that is missing from a
-- department's reply is MISSING, and a PO line that is not part of the
-- snapshot is UNKNOWN.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS reporting_periods (
    reporting_month DATE PRIMARY KEY,

    entity VARCHAR(50) NOT NULL DEFAULT '2000',

    population_frozen BOOLEAN NOT NULL DEFAULT FALSE,
    population_frozen_at TIMESTAMPTZ,
    population_source VARCHAR(100),
    population_row_count INTEGER,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_reporting_month_first_day
        CHECK (EXTRACT(DAY FROM reporting_month) = 1)
);

COMMENT ON TABLE reporting_periods IS
    'One row per reporting month. population_frozen marks the moment the '
    'expected PO line population became authoritative for that month.';


CREATE TABLE IF NOT EXISTS department_contacts (
    contact_id BIGSERIAL PRIMARY KEY,

    legal_entity VARCHAR(50) NOT NULL,
    cost_center VARCHAR(50) NOT NULL,
    department VARCHAR(255) NOT NULL,

    department_owner VARCHAR(255),
    authorized_email VARCHAR(255) NOT NULL,
    delegate_email VARCHAR(255),

    active BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_department_contact
        UNIQUE (legal_entity, cost_center, authorized_email)
);

CREATE INDEX IF NOT EXISTS idx_department_contact_cc
ON department_contacts (legal_entity, cost_center);

COMMENT ON TABLE department_contacts IS
    'Authorized department recipients. A reply is only accepted from an '
    'address listed here (or an explicit delegate).';


CREATE TABLE IF NOT EXISTS expected_po_lines (
    expected_id BIGSERIAL PRIMARY KEY,

    reporting_month DATE NOT NULL
        REFERENCES reporting_periods(reporting_month),

    legal_entity VARCHAR(50) NOT NULL,

    department_id VARCHAR(100) NOT NULL,
    department_name VARCHAR(255) NOT NULL,

    cost_center VARCHAR(50) NOT NULL,
    department_owner VARCHAR(255),
    authorized_email VARCHAR(255),

    po_number VARCHAR(50) NOT NULL,
    po_line INTEGER NOT NULL,

    po_description TEXT,
    po_line_amount NUMERIC(18, 2),
    cumulative_gr NUMERIC(18, 2),
    currency VARCHAR(10),

    previous_completion_pct NUMERIC(9, 6),
    previous_baseline_period DATE,
    baseline_status VARCHAR(30) NOT NULL DEFAULT 'MISSING',

    snapshot_version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_expected_po_line
        UNIQUE (reporting_month, legal_entity, po_number, po_line),

    CONSTRAINT chk_baseline_status
        CHECK (baseline_status IN ('PRESENT', 'MISSING', 'NOT_COMPARABLE')),

    CONSTRAINT chk_expected_completion_range
        CHECK (
            previous_completion_pct IS NULL
            OR (
                previous_completion_pct >= 0
                AND previous_completion_pct <= 1
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_expected_po_lines_period_dept
ON expected_po_lines (reporting_month, legal_entity, department_id);

CREATE INDEX IF NOT EXISTS idx_expected_po_lines_key
ON expected_po_lines (reporting_month, legal_entity, po_number, po_line);

COMMENT ON TABLE expected_po_lines IS
    'Frozen expected PO line population per reporting month. Department '
    'ownership is preserved historically and never re-derived.';


CREATE TABLE IF NOT EXISTS monthly_completion_baselines (
    baseline_id BIGSERIAL PRIMARY KEY,

    reporting_month DATE NOT NULL
        REFERENCES reporting_periods(reporting_month),

    legal_entity VARCHAR(50) NOT NULL,
    po_number VARCHAR(50) NOT NULL,
    po_line INTEGER NOT NULL,

    accepted_completion_pct NUMERIC(9, 6) NOT NULL,

    source_submission_id BIGINT,
    finalized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finalized_by VARCHAR(255),

    CONSTRAINT uq_monthly_baseline
        UNIQUE (reporting_month, legal_entity, po_number, po_line),

    CONSTRAINT chk_baseline_range
        CHECK (
            accepted_completion_pct >= 0
            AND accepted_completion_pct <= 1
        )
);

CREATE INDEX IF NOT EXISTS idx_monthly_baseline_key
ON monthly_completion_baselines (
    reporting_month, legal_entity, po_number, po_line
);

COMMENT ON TABLE monthly_completion_baselines IS
    'Finalized accepted completion percentage per PO line per month. This is '
    'the only legitimate source of the previous-month baseline.';