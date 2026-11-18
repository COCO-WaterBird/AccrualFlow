-- ===========================================================================
-- AccrualFlow - the monthly cycle progress ledger.
--
-- The owner starts one run per reporting month.  The run walks through a
-- fixed sequence of stages, and the dashboard shows where the cycle
-- currently stands.
--
-- One row per (reporting month, stage).  The row records when the stage
-- started, when it finished, whether it succeeded, and how many items it
-- touched.  That is enough to answer the questions the owner actually
-- asks: did the ERP download happen, was the data tidied up, were the
-- emails sent, and how far did the replies get.
--
-- The stage names are fixed.  They are the vocabulary of the dashboard,
-- so they must not drift.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS cycle_stage_runs (
    stage_run_id BIGSERIAL PRIMARY KEY,

    reporting_month DATE NOT NULL
        REFERENCES reporting_periods(reporting_month),

    -- The stage this row describes.
    --
    --   ERP_IMPORT        pull the master data from the ERP
    --   FREEZE            freeze the expected PO line population
    --   DISPATCH          prepare and send the collection requests
    --   DISCOVER          find the replies
    --   PARSE             parse, validate and persist the replies
    --   CORRECTION        prepare the correction workbooks and drafts
    --   STATUS            recompute the department status
    --   MASTER_EXCEL      publish the consolidated workbook
    --   DATA_READY        reconcile and mark the period ready
    stage VARCHAR(40) NOT NULL,

    -- The Airflow run this stage belongs to, when it came from one.
    dag_run_id VARCHAR(255),

    -- The outcome.  A stage that never ran has no row at all.
    status VARCHAR(20) NOT NULL DEFAULT 'RUNNING',

    -- The counters the stage produced, as JSON.  The shape differs per
    -- stage, so it is not broken into columns.
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,

    error_message TEXT,

    started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMP WITH TIME ZONE,

    CONSTRAINT chk_cycle_stage_runs_status CHECK (
        status::text = ANY (
            ARRAY[
                'RUNNING'::varchar,
                'SUCCESS'::varchar,
                'FAILED'::varchar,
                'SKIPPED'::varchar
            ]
        )
    ),

    CONSTRAINT chk_cycle_stage_runs_stage CHECK (
        stage::text = ANY (
            ARRAY[
                'ERP_IMPORT'::varchar,
                'FREEZE'::varchar,
                'DISPATCH'::varchar,
                'DISCOVER'::varchar,
                'PARSE'::varchar,
                'CORRECTION'::varchar,
                'STATUS'::varchar,
                'MASTER_EXCEL'::varchar,
                'DATA_READY'::varchar
            ]
        )
    )
);

-- One live row per (month, stage).  A stage that is retried replaces
-- its row instead of adding a second one, so the dashboard never shows
-- two rows for the same stage.
CREATE UNIQUE INDEX IF NOT EXISTS uq_cycle_stage_runs_month_stage
    ON cycle_stage_runs (reporting_month, stage);

CREATE INDEX IF NOT EXISTS idx_cycle_stage_runs_month
    ON cycle_stage_runs (reporting_month, started_at DESC);

COMMENT ON TABLE cycle_stage_runs IS
    'The progress ledger of the monthly collection cycle. One row per '
    'stage of the run, so that the dashboard can show how far the '
    'reporting month has progressed and which stage needs attention.';

COMMENT ON COLUMN cycle_stage_runs.stage IS
    'The stage of the cycle: ERP_IMPORT, FREEZE, DISPATCH, DISCOVER, '
    'PARSE, CORRECTION, STATUS, MASTER_EXCEL or DATA_READY.';

COMMENT ON COLUMN cycle_stage_runs.detail IS
    'The counters the stage produced, as JSON. The shape depends on the '
    'stage: row counts for the ERP import, line counts for the freeze, '
    'request counts for the dispatch, and so on.';
