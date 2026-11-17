-- ===========================================================================
-- AccrualFlow MVP - the PO status scope of the frozen population.
--
-- The owner decided that the monthly report covers the OPEN PO lines only.
-- A line that is filtered out is recorded on the exclusion ledger, so the
-- population never silently understates the commitment book.
--
-- A PO line that closes DURING a reporting month is not dropped from that
-- month.  It stays in the population, marked as closed, so the month in
-- which the work finished still shows the line and its final accepted
-- value.  From the NEXT month on, the line leaves the population and moves
-- to the ledger.
--
-- These columns make that state visible on the frozen line itself.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- The PO status of a frozen line, and the closed marker.
-- ---------------------------------------------------------------------------
ALTER TABLE expected_po_lines
    ADD COLUMN IF NOT EXISTS po_status VARCHAR(30);

ALTER TABLE expected_po_lines
    ADD COLUMN IF NOT EXISTS lifecycle_status VARCHAR(30) NOT NULL DEFAULT 'ACTIVE';

COMMENT ON COLUMN expected_po_lines.po_status IS
    'The ERP po_status of the line at the moment the population was frozen.';

COMMENT ON COLUMN expected_po_lines.lifecycle_status IS
    'ACTIVE           the line is a live part of the reporting scope.'
    'CLOSED_IN_MONTH  the line closed during this reporting month and is '
    '                 retained here, marked as closed, so the month in '
    '                 which the work finished still shows the line.';


-- The constraint is added separately so that an existing table can be
-- migrated without a data rewrite.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_expected_lifecycle_status'
          AND conrelid = 'expected_po_lines'::regclass
    ) THEN
        ALTER TABLE expected_po_lines
            ADD CONSTRAINT chk_expected_lifecycle_status
            CHECK (
                lifecycle_status IN ('ACTIVE', 'CLOSED_IN_MONTH')
            );
    END IF;
END $$;


-- The reporting views and the Master Excel read the status of a line.
CREATE INDEX IF NOT EXISTS idx_expected_po_lines_status
ON expected_po_lines (reporting_month, legal_entity, po_status);
