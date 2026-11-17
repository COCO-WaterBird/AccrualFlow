-- ===========================================================================
-- AccrualFlow - population freeze ledger.
--
-- The frozen expected population is a financial statement about WHO was
-- asked to report.  A department that was left out of the population is
-- therefore a fact that must be recorded, not a silent detail.
--
-- This table is the queryable ledger of every department that was
-- excluded from a frozen population, and why.  The same information is
-- written to a CSV under the runtime population directory so that finance
-- can receive it without database access.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS population_exclusions (
    exclusion_id BIGSERIAL PRIMARY KEY,

    reporting_month DATE NOT NULL,

    legal_entity VARCHAR(50) NOT NULL,

    cost_center VARCHAR(50) NOT NULL,
    department_name VARCHAR(255),
    manager VARCHAR(255),

    -- NO_PO_LINES             the department has no PO line in the ERP snapshot
    -- NO_MANAGER              no authorised recipient can be determined
    -- COST_CENTER_NOT_IN_MAPPING  PO lines exist but the cost centre is unmapped
    exclusion_reason VARCHAR(40) NOT NULL
        CHECK (exclusion_reason IN (
            'NO_PO_LINES',
            'NO_MANAGER',
            'COST_CENTER_NOT_IN_MAPPING'
        )),

    po_line_count INTEGER NOT NULL DEFAULT 0,

    reported_to_finance BOOLEAN NOT NULL DEFAULT FALSE,
    reported_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_population_exclusion
        UNIQUE (
            reporting_month, legal_entity,
            cost_center, exclusion_reason
        )
);

CREATE INDEX IF NOT EXISTS idx_population_exclusions_month
ON population_exclusions (reporting_month, legal_entity, exclusion_reason);

COMMENT ON TABLE population_exclusions IS
    'Departments that were excluded from a frozen expected population. '
    'Every row is a department that finance was NOT asked to report for '
    'that month, and the reason why.';
