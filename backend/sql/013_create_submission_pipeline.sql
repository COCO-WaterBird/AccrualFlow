-- ===========================================================================
-- AccrualFlow MVP - email requests, inbound processing, raw archives,
-- attachments, department submissions, submission lines, validation issues,
-- correction requests and department status history.
--
-- Design notes
-- ------------
-- * The Gmail message id is the idempotency key for inbound processing.
-- * Attachment content hashes deduplicate identical workbooks.
-- * Every submission version is preserved.  Accepted values live in a
--   separate column from the raw submitted values.
-- * Department status, submission status, line validation status and
--   reason status are kept in separate columns.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Outbound collection request
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS email_requests (
    request_id BIGSERIAL PRIMARY KEY,

    reporting_month DATE NOT NULL
        REFERENCES reporting_periods(reporting_month),

    legal_entity VARCHAR(50) NOT NULL,
    cost_center VARCHAR(50) NOT NULL,
    department_id VARCHAR(100) NOT NULL,
    department_name VARCHAR(255) NOT NULL,

    recipient VARCHAR(255) NOT NULL,
    subject TEXT NOT NULL,
    body TEXT,

    template_attachment_path TEXT,
    template_attachment_sha256 VARCHAR(64),

    gmail_message_id TEXT,
    gmail_thread_id TEXT,
    provider_message_id TEXT,

    status VARCHAR(30) NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN (
            'DRAFT',
            'DRY_RUN',
            'PREPARED',
            'SENT',
            'FAILED'
        )),

    sent_at TIMESTAMPTZ,
    due_date DATE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_email_request_send
        UNIQUE (reporting_month, legal_entity, cost_center)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_email_request_gmail_message
ON email_requests (gmail_message_id)
WHERE gmail_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_email_request_month
ON email_requests (reporting_month, status);


-- ---------------------------------------------------------------------------
-- Inbound processing ledger (idempotency)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gmail_message_processing (
    processing_id BIGSERIAL PRIMARY KEY,

    gmail_message_id TEXT NOT NULL,
    gmail_thread_id TEXT,

    reporting_month DATE NOT NULL,

    sender_address VARCHAR(255),
    recipient_address VARCHAR(255),
    subject TEXT,
    message_date TIMESTAMPTZ,

    authorization_status VARCHAR(30) NOT NULL DEFAULT 'PENDING'
        CHECK (authorization_status IN (
            'PENDING',
            'AUTHORIZED',
            'UNAUTHORIZED',
            'DELEGATED',
            'NOT_APPLICABLE'
        )),

    processing_status VARCHAR(40) NOT NULL DEFAULT 'DISCOVERED'
        CHECK (processing_status IN (
            'DISCOVERED',
            'REGISTERED',
            'ARCHIVED',
            'PARSED',
            'VALIDATED',
            'PERSISTED',
            'SKIPPED_ORIGINAL_REQUEST',
            'SKIPPED_UNAUTHORIZED',
            'SKIPPED_DUPLICATE',
            'FAILED'
        )),

    error_message TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,

    CONSTRAINT uq_gmail_message_processing
        UNIQUE (gmail_message_id)
);

CREATE INDEX IF NOT EXISTS idx_gmail_processing_month
ON gmail_message_processing (reporting_month, processing_status);


-- ---------------------------------------------------------------------------
-- Raw evidence
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS email_archives (
    archive_id BIGSERIAL PRIMARY KEY,

    gmail_message_id TEXT NOT NULL,
    gmail_thread_id TEXT,

    reporting_month DATE NOT NULL,

    subject TEXT,
    sender_address VARCHAR(255),
    recipient_address VARCHAR(255),
    message_date TIMESTAMPTZ,

    eml_path TEXT NOT NULL,
    eml_sha256 VARCHAR(64) NOT NULL,
    eml_size_bytes BIGINT NOT NULL,

    direction VARCHAR(20) NOT NULL DEFAULT 'INBOUND'
        CHECK (direction IN ('INBOUND', 'OUTBOUND')),

    archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_email_archive
        UNIQUE (gmail_message_id, direction)
);

CREATE INDEX IF NOT EXISTS idx_email_archive_month
ON email_archives (reporting_month, direction);

CREATE TABLE IF NOT EXISTS email_attachments (
    email_attachment_id BIGSERIAL PRIMARY KEY,

    archive_id BIGINT NOT NULL
        REFERENCES email_archives(archive_id) ON DELETE CASCADE,

    gmail_message_id TEXT NOT NULL,

    original_filename TEXT NOT NULL,
    stored_filename TEXT,
    stored_path TEXT,
    content_sha256 VARCHAR(64),
    size_bytes BIGINT NOT NULL DEFAULT 0,
    content_type TEXT,

    status VARCHAR(30) NOT NULL DEFAULT 'ARCHIVED'
        CHECK (status IN (
            'ARCHIVED',
            'REJECTED',
            'UNREADABLE',
            'TOO_LARGE',
            'DUPLICATE',
            'UNSUPPORTED_TYPE'
        )),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_email_attachments_message
ON email_attachments (gmail_message_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_email_attachments_content
ON email_attachments (content_sha256, gmail_message_id)
WHERE content_sha256 IS NOT NULL;


-- ---------------------------------------------------------------------------
-- Department submissions (one row per message / version)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS department_submissions (
    submission_id BIGSERIAL PRIMARY KEY,

    gmail_message_id TEXT NOT NULL,
    gmail_thread_id TEXT,
    archive_id BIGINT
        REFERENCES email_archives(archive_id),

    reporting_month DATE NOT NULL
        REFERENCES reporting_periods(reporting_month),

    legal_entity VARCHAR(50) NOT NULL,
    department_id VARCHAR(100) NOT NULL,
    department_name VARCHAR(255) NOT NULL,
    cost_center VARCHAR(50),

    version INTEGER NOT NULL DEFAULT 1,

    source_attachment_sha256 VARCHAR(64),
    source_attachment_path TEXT,
    source_sheet TEXT,

    submitted_by VARCHAR(255),
    submitted_at TIMESTAMPTZ,

    processing_status VARCHAR(40) NOT NULL DEFAULT 'RECEIVED'
        CHECK (processing_status IN (
            'RECEIVED',
            'ARCHIVED',
            'PARSED',
            'VALIDATED',
            'PERSISTED',
            'PARTIALLY_ACCEPTED',
            'FAILED',
            'DUPLICATE'
        )),

    error_message TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_department_submission_version
        UNIQUE (reporting_month, department_id, version),

    CONSTRAINT uq_department_submission_message
        UNIQUE (gmail_message_id, version)
);

CREATE INDEX IF NOT EXISTS idx_department_submissions_month
ON department_submissions (reporting_month, legal_entity, department_id);


-- ---------------------------------------------------------------------------
-- Submission lines
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS submission_lines (
    submission_line_id BIGSERIAL PRIMARY KEY,

    submission_id BIGINT NOT NULL
        REFERENCES department_submissions(submission_id) ON DELETE CASCADE,

    reporting_month DATE NOT NULL,
    legal_entity VARCHAR(50) NOT NULL,
    department_id VARCHAR(100) NOT NULL,

    po_number VARCHAR(50) NOT NULL,
    po_line INTEGER NOT NULL,

    source_sheet TEXT,
    source_row INTEGER,

    raw_value TEXT,
    raw_number_format TEXT,
    raw_data_type TEXT,

    normalized_pct NUMERIC(9, 6),
    suggested_pct NUMERIC(9, 6),

    format_detection VARCHAR(40),
    format_confirmation_status VARCHAR(30) NOT NULL DEFAULT 'NOT_REQUIRED'
        CHECK (format_confirmation_status IN (
            'NOT_REQUIRED',
            'REQUIRED',
            'CONFIRMED',
            'REJECTED'
        )),

    previous_pct NUMERIC(9, 6),
    change_percentage_points NUMERIC(12, 4),

    remarks TEXT,

    reason_status VARCHAR(30) NOT NULL DEFAULT 'NOT_APPLICABLE'
        CHECK (reason_status IN (
            'NOT_APPLICABLE',
            'DECREASE_REASON_MISSING',
            'DECREASE_REASON_PROVIDED',
            'REVIEW_REQUIRED'
        )),

    validation_status VARCHAR(40) NOT NULL
        CHECK (validation_status IN (
            'VALID',
            'MISSING',
            'INVALID',
            'REVIEW_REQUIRED',
            'FORMAT_CONFIRMATION_REQUIRED',
            'UNKNOWN_PO_LINE',
            'DUPLICATE_PO_LINE'
        )),

    exception_codes TEXT[] NOT NULL DEFAULT '{}',

    confidence VARCHAR(20),
    evidence TEXT[] NOT NULL DEFAULT '{}',

    accepted_current_pct NUMERIC(9, 6),
    accepted_at TIMESTAMPTZ,

    is_current_version BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_submission_line
        UNIQUE (submission_id, po_number, po_line, source_row)
);

CREATE INDEX IF NOT EXISTS idx_submission_lines_key
ON submission_lines (reporting_month, legal_entity, po_number, po_line);

CREATE INDEX IF NOT EXISTS idx_submission_lines_submission
ON submission_lines (submission_id);

CREATE INDEX IF NOT EXISTS idx_submission_lines_accepted
ON submission_lines (reporting_month, department_id)
WHERE accepted_current_pct IS NOT NULL;


-- ---------------------------------------------------------------------------
-- Validation issues
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS validation_issues (
    issue_id BIGSERIAL PRIMARY KEY,

    reporting_month DATE NOT NULL,
    legal_entity VARCHAR(50) NOT NULL,
    department_id VARCHAR(100) NOT NULL,

    submission_id BIGINT
        REFERENCES department_submissions(submission_id) ON DELETE CASCADE,
    submission_line_id BIGINT
        REFERENCES submission_lines(submission_line_id) ON DELETE CASCADE,

    po_number VARCHAR(50),
    po_line INTEGER,

    exception_code VARCHAR(60) NOT NULL,
    severity VARCHAR(20) NOT NULL DEFAULT 'WARNING'
        CHECK (severity IN ('INFO', 'WARNING', 'ERROR')),

    detail TEXT,
    evidence TEXT[] NOT NULL DEFAULT '{}',

    status VARCHAR(20) NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'RESOLVED', 'SUPERSEDED')),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_validation_issues_month
ON validation_issues (reporting_month, department_id, status);

CREATE INDEX IF NOT EXISTS idx_validation_issues_code
ON validation_issues (exception_code);


-- ---------------------------------------------------------------------------
-- Correction requests
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS correction_requests (
    correction_id BIGSERIAL PRIMARY KEY,

    reporting_month DATE NOT NULL,
    legal_entity VARCHAR(50) NOT NULL,
    department_id VARCHAR(100) NOT NULL,
    department_name VARCHAR(255) NOT NULL,
    cost_center VARCHAR(50),

    attempt INTEGER NOT NULL DEFAULT 1,

    issue_count INTEGER NOT NULL DEFAULT 0,
    unresolved_line_count INTEGER NOT NULL DEFAULT 0,

    workbook_path TEXT,
    workbook_sha256 VARCHAR(64),
    draft_path TEXT,
    draft_subject TEXT,
    draft_body TEXT,

    recipient VARCHAR(255),
    gmail_thread_id TEXT,
    gmail_message_id TEXT,

    status VARCHAR(30) NOT NULL DEFAULT 'PREPARED'
        CHECK (status IN (
            'PREPARED',
            'SENT',
            'FAILED',
            'RESOLVED',
            'SUPERSEDED'
        )),

    prepared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    error_message TEXT,

    CONSTRAINT uq_correction_attempt
        UNIQUE (reporting_month, legal_entity, department_id, attempt)
);

CREATE INDEX IF NOT EXISTS idx_correction_requests_month
ON correction_requests (reporting_month, legal_entity, department_id, status);


-- ---------------------------------------------------------------------------
-- Department status history
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS department_status_history (
    status_id BIGSERIAL PRIMARY KEY,

    reporting_month DATE NOT NULL,
    legal_entity VARCHAR(50) NOT NULL,
    department_id VARCHAR(100) NOT NULL,
    department_name VARCHAR(255) NOT NULL,
    cost_center VARCHAR(50),

    status VARCHAR(40) NOT NULL
        CHECK (status IN (
            'NOT_SUBMITTED',
            'RECEIVED',
            'VALIDATING',
            'VALIDATION_FAILED',
            'FORMAT_CONFIRMATION_REQUIRED',
            'CORRECTION_PREPARED',
            'CORRECTION_REQUESTED',
            'RESUBMITTED',
            'DATA_COMPLETE_REVIEW_PENDING',
            'COMPLETED'
        )),

    previous_status VARCHAR(40),

    expected_line_count INTEGER NOT NULL DEFAULT 0,
    accepted_line_count INTEGER NOT NULL DEFAULT 0,
    missing_line_count INTEGER NOT NULL DEFAULT 0,
    invalid_line_count INTEGER NOT NULL DEFAULT 0,
    review_line_count INTEGER NOT NULL DEFAULT 0,

    reason TEXT,
    changed_by VARCHAR(255) DEFAULT 'system',

    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_department_status_history_key
ON department_status_history (
    reporting_month, legal_entity, department_id, changed_at DESC
);


-- ---------------------------------------------------------------------------
-- Master export runs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS master_export_runs (
    export_id BIGSERIAL PRIMARY KEY,

    reporting_month DATE NOT NULL,

    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_sha256 VARCHAR(64) NOT NULL,
    file_size_bytes BIGINT NOT NULL,

    expected_line_count INTEGER NOT NULL DEFAULT 0,
    accepted_line_count INTEGER NOT NULL DEFAULT 0,
    exception_count INTEGER NOT NULL DEFAULT 0,
    department_count INTEGER NOT NULL DEFAULT 0,

    status VARCHAR(20) NOT NULL DEFAULT 'SUCCESS'
        CHECK (status IN ('SUCCESS', 'FAILED')),

    error_message TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_master_export_runs_month
ON master_export_runs (reporting_month, created_at DESC);