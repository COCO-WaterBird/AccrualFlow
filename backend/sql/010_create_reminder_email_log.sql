
CREATE TABLE IF NOT EXISTS reminder_email_log (
    email_id BIGSERIAL PRIMARY KEY,

    attachment_id BIGINT NOT NULL
        REFERENCES reminder_attachment_archive(attachment_id),

    reporting_month DATE NOT NULL,

    recipient VARCHAR(255) NOT NULL,
    subject TEXT NOT NULL,

    status VARCHAR(20) NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN ('DRAFT', 'SENT', 'FAILED')),

    sent_at TIMESTAMPTZ,
    due_date DATE,

    provider_message_id TEXT,
    error_message TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT reminder_email_sent_fields_check
        CHECK (
            status <> 'SENT'
            OR (sent_at IS NOT NULL AND due_date IS NOT NULL)
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS
    uq_reminder_email_initial_send
ON reminder_email_log (reporting_month, attachment_id);
