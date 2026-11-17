
CREATE TABLE IF NOT EXISTS capex_completion_submission (
    submission_id BIGSERIAL PRIMARY KEY,

    reporting_month DATE NOT NULL,

    po_number VARCHAR(50) NOT NULL,
    po_line INTEGER NOT NULL,
    cost_center VARCHAR(50) NOT NULL,

    po_line_amount NUMERIC(18, 2),
    cumulative_gr NUMERIC(18, 2),

    last_completion NUMERIC(8, 6),
    current_completion NUMERIC(8, 6),

    comment TEXT,

    validation_status VARCHAR(30) NOT NULL,
    review_status VARCHAR(30) NOT NULL DEFAULT 'PENDING',

    source_message_id TEXT,
    source_attachment TEXT,
    excel_row INTEGER,

    submitted_at TIMESTAMPTZ DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    reviewed_by VARCHAR(255),
    review_comment TEXT,

    CONSTRAINT uq_completion_submission
        UNIQUE (
            reporting_month,
            po_number,
            po_line
        ),

    CONSTRAINT chk_validation_status
        CHECK (
            validation_status IN (
                'VALID',
                'MISSING',
                'INVALID',
                'REVIEW_REQUIRED'
            )
        ),

    CONSTRAINT chk_review_status
        CHECK (
            review_status IN (
                'PENDING',
                'APPROVED',
                'REJECTED'
            )
        )
);
