-- ===========================================================================
-- AccrualFlow - the PO status scope of the expected population.
--
-- The owner decided that the monthly report covers the OPEN PO lines only.
-- Every line that is filtered out has to be visible, so it is recorded on
-- the same ledger as the excluded departments.
--
-- A PO line that closes DURING a reporting month is not simply dropped.
-- It stays in that month's population, marked as closed, so that the month
-- in which the work finished still shows the line and its final accepted
-- value.  From the NEXT month on, the line leaves the population and moves
-- to the exclusion ledger.
--
-- The tables below record both decisions and make them auditable.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- The PO status scope that was applied when the population was frozen.
--
-- One row per frozen population.  It states which statuses were included,
-- so a reader of the report can always tell what the population meant.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS population_status_scope (
    scope_id BIGSERIAL PRIMARY KEY,

    reporting_month DATE NOT NULL
        REFERENCES reporting_periods(reporting_month),

    legal_entity VARCHAR(50) NOT NULL DEFAULT '2000',

    -- The status filter that was applied, for example 'Open'.
    included_status VARCHAR(50) NOT NULL,

    -- The statuses that were filtered out, for example 'On Hold,Closed'.
    excluded_statuses VARCHAR(255),

    -- Whether a line that closes during the month stays in that month's
    -- population, marked as closed.
    retain_closed_in_last_active_month BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_population_status_scope
        UNIQUE (reporting_month, legal_entity)
);

COMMENT ON TABLE population_status_scope IS
    'The PO status filter that was applied when a population was frozen. '
    'It states what the population meant, so the report is always '
    'interpretable.';


-- ---------------------------------------------------------------------------
-- The PO lines that were filtered out of a population by their status.
--
-- This is the PO line counterpart of population_exclusions.  That table
-- holds whole departments; this one holds individual PO lines.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS population_status_exclusions (
    exclusion_id BIGSERIAL PRIMARY KEY,

    reporting_month DATE NOT NULL
        REFERENCES reporting_periods(reporting_month),

    legal_entity VARCHAR(50) NOT NULL DEFAULT '2000',

    cost_center VARCHAR(50) NOT NULL,
    department_name VARCHAR(255),
    manager VARCHAR(255),

    po_number VARCHAR(50) NOT NULL,
    po_line INTEGER NOT NULL,
    po_status VARCHAR(30) NOT NULL,
    po_description TEXT,
    po_line_amount NUMERIC(18, 2),
    currency VARCHAR(10),

    -- The month in which the line was last part of the population, when
    -- that can be established from the ERP history.  NULL when the line
    -- was never active.
    last_active_month DATE,

    -- Whether finance has been told about the exclusion.
    reported_to_finance BOOLEAN NOT NULL DEFAULT FALSE,
    reported_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_population_status_exclusion
        UNIQUE (
            reporting_month, legal_entity,
            po_number, po_line, po_status
        )
);

CREATE INDEX IF NOT EXISTS idx_population_status_exclusions_month
ON population_status_exclusions (reporting_month, legal_entity, po_status);

COMMENT ON TABLE population_status_exclusions IS
    'The PO lines that were filtered out of a frozen population by their '
    'ERP status, with the reason. Every line that was filtered out is '
    'recorded here, so the population never silently understates the '
    'commitment book.';


-- ---------------------------------------------------------------------------
-- The PO lines that closed during the reporting month.
--
-- These lines are IN the population of their last active month.  They are
-- recorded here so that the report can mark them, and so that the next
-- month can exclude them with a traceable reason.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS population_closed_lines (
    closed_id BIGSERIAL PRIMARY KEY,

    reporting_month DATE NOT NULL
        REFERENCES reporting_periods(reporting_month),

    legal_entity VARCHAR(50) NOT NULL DEFAULT '2000',

    cost_center VARCHAR(50) NOT NULL,
    department_name VARCHAR(255),

    po_number VARCHAR(50) NOT NULL,
    po_line INTEGER NOT NULL,
    po_description TEXT,
    po_line_amount NUMERIC(18, 2),
    currency VARCHAR(10),

    -- The ERP status at the time the population was frozen.
    po_status VARCHAR(30) NOT NULL DEFAULT 'Closed',

    -- The month in which the line was last active.  For a line that
    -- closed during the reporting month, this is the reporting month
    -- itself: the line finished partway through it.
    last_active_month DATE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_population_closed_line
        UNIQUE (reporting_month, legal_entity, po_number, po_line)
);

CREATE INDEX IF NOT EXISTS idx_population_closed_lines_month
ON population_closed_lines (reporting_month, legal_entity, cost_center);

COMMENT ON TABLE population_closed_lines IS
    'The PO lines that closed during a reporting month. They remain in that '
    'month''s population, marked as closed, so the month in which the work '
    'finished still shows the line and its final accepted value.';
