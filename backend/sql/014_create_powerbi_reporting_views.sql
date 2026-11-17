-- ===========================================================================
-- AccrualFlow Power BI reporting layer.
--
-- These views are READ ONLY and are the intended source for Power BI.
-- The Master Excel workbook is exported from the same accepted data but is
-- NOT the primary Power BI source.
--
-- The reporting role is expected to have SELECT only:
--     GRANT USAGE ON SCHEMA public TO accrualflow_pbi;
--     GRANT SELECT ON ALL TABLES IN SCHEMA public TO accrualflow_pbi;
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- Page 1 - Department submission overview
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_pbi_department_submission_overview AS
SELECT
    rp.reporting_month,
    e.legal_entity,
    e.department_id,
    e.department_name,
    e.cost_center,
    e.department_owner,
    e.authorized_email,

    COUNT(*)::INTEGER                                      AS expected_line_count,
    COUNT(sl.submission_line_id) FILTER (
        WHERE sl.accepted_current_pct IS NOT NULL
    )::INTEGER                                             AS accepted_line_count,
    COUNT(sl.submission_line_id) FILTER (
        WHERE sl.accepted_current_pct IS NOT NULL
    )::INTEGER                                             AS submitted_line_count,

    COUNT(sl.submission_line_id) FILTER (
        WHERE sl.validation_status = 'MISSING'
    )::INTEGER                                             AS missing_line_count,
    COUNT(sl.submission_line_id) FILTER (
        WHERE sl.validation_status = 'INVALID'
    )::INTEGER                                             AS invalid_line_count,
    COUNT(sl.submission_line_id) FILTER (
        WHERE sl.validation_status = 'FORMAT_CONFIRMATION_REQUIRED'
    )::INTEGER                                             AS format_confirmation_count,
    COUNT(sl.submission_line_id) FILTER (
        WHERE sl.reason_status IN (
            'DECREASE_REASON_MISSING',
            'DECREASE_REASON_PROVIDED',
            'REVIEW_REQUIRED'
        )
    )::INTEGER                                             AS review_required_count,

    ROUND(
        100.0 * COUNT(sl.submission_line_id) FILTER (
            WHERE sl.accepted_current_pct IS NOT NULL
        ) / NULLIF(COUNT(*), 0),
        2
    )                                                      AS line_acceptance_rate_pct,

    COALESCE(dsh.status, 'NOT_SUBMITTED')                  AS department_status,
    dsh.changed_at                                         AS status_changed_at,
    dsh.previous_status                                    AS previous_department_status,

    ds.submission_id                                       AS latest_submission_id,
    ds.version                                             AS latest_submission_version,
    ds.submitted_at                                        AS latest_submission_at,
    ds.processing_status                                   AS latest_processing_status,

    cr.correction_id                                       AS latest_correction_id,
    cr.status                                              AS latest_correction_status,
    cr.sent_at                                             AS latest_correction_sent_at,
    cr.prepared_at                                         AS latest_correction_prepared_at,

    me.file_name                                           AS latest_master_export_file,
    me.created_at                                          AS latest_master_export_at

FROM expected_po_lines e
JOIN reporting_periods rp
  ON rp.reporting_month = e.reporting_month

LEFT JOIN submission_lines sl
  ON sl.reporting_month = e.reporting_month
 AND sl.legal_entity   = e.legal_entity
 AND sl.po_number      = e.po_number
 AND sl.po_line        = e.po_line
 AND sl.is_current_version

LEFT JOIN LATERAL (
    SELECT
        s.submission_id,
        s.version,
        s.submitted_at,
        s.processing_status
    FROM department_submissions s
    WHERE s.reporting_month = e.reporting_month
      AND s.legal_entity   = e.legal_entity
      AND s.department_id   = e.department_id
    ORDER BY s.version DESC
    LIMIT 1
) ds ON TRUE

LEFT JOIN LATERAL (
    SELECT
        c.correction_id,
        c.status,
        c.sent_at,
        c.prepared_at
    FROM correction_requests c
    WHERE c.reporting_month = e.reporting_month
      AND c.legal_entity   = e.legal_entity
      AND c.department_id   = e.department_id
    ORDER BY c.attempt DESC
    LIMIT 1
) cr ON TRUE

LEFT JOIN LATERAL (
    SELECT h.status, h.changed_at, h.previous_status
    FROM department_status_history h
    WHERE h.reporting_month = e.reporting_month
      AND h.legal_entity   = e.legal_entity
      AND h.department_id   = e.department_id
    ORDER BY h.changed_at DESC, h.status_id DESC
    LIMIT 1
) dsh ON TRUE

LEFT JOIN LATERAL (
    SELECT m.file_name, m.created_at
    FROM master_export_runs m
    WHERE m.reporting_month = e.reporting_month
      AND m.status = 'SUCCESS'
    ORDER BY m.created_at DESC
    LIMIT 1
) me ON TRUE

GROUP BY
    rp.reporting_month,
    e.legal_entity,
    e.department_id,
    e.department_name,
    e.cost_center,
    e.department_owner,
    e.authorized_email,
    dsh.status,
    dsh.changed_at,
    dsh.previous_status,
    ds.submission_id,
    ds.version,
    ds.submitted_at,
    ds.processing_status,
    cr.correction_id,
    cr.status,
    cr.sent_at,
    cr.prepared_at,
    me.file_name,
    me.created_at;

COMMENT ON VIEW vw_pbi_department_submission_overview IS
    'Page 1 - one row per department and reporting month with expected, '
    'submitted and accepted PO line counts and the current department state.';


-- ---------------------------------------------------------------------------
-- Page 2 - Validation exceptions
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_pbi_validation_exceptions AS
SELECT
    vi.issue_id,
    vi.reporting_month,
    vi.legal_entity,
    vi.department_id,
    e.department_name,
    e.cost_center,

    vi.po_number,
    vi.po_line,
    vi.exception_code,
    vi.severity,
    vi.status,
    vi.detail,

    sl.source_sheet,
    sl.source_row,
    sl.raw_value,
    sl.raw_number_format,
    sl.normalized_pct,
    sl.suggested_pct,
    sl.format_detection,
    sl.format_confirmation_status,
    sl.previous_pct,
    sl.change_percentage_points,
    sl.remarks,
    sl.reason_status,
    sl.validation_status,
    sl.accepted_current_pct,
    sl.confidence,

    ds.gmail_message_id,
    ds.gmail_thread_id,
    ds.submitted_by,
    ds.submitted_at,
    ds.version AS submission_version,

    vi.created_at AS issue_created_at,
    vi.resolved_at

FROM validation_issues vi
LEFT JOIN submission_lines sl
  ON sl.submission_line_id = vi.submission_line_id
LEFT JOIN department_submissions ds
  ON ds.submission_id = vi.submission_id
LEFT JOIN expected_po_lines e
  ON e.reporting_month = vi.reporting_month
 AND e.legal_entity   = vi.legal_entity
 AND e.po_number      = vi.po_number
 AND e.po_line        = vi.po_line

WHERE vi.status <> 'SUPERSEDED';

COMMENT ON VIEW vw_pbi_validation_exceptions IS
    'Page 2 - line level validation exceptions. Suggested percentages are '
    'exposed separately from accepted values so a guess is never presented '
    'as an accepted number.';


-- ---------------------------------------------------------------------------
-- Page 3 - Submission history and audit trail
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_pbi_submission_audit_history AS
SELECT
    ds.submission_id,
    ds.reporting_month,
    ds.legal_entity,
    ds.department_id,
    ds.department_name,
    ds.cost_center,

    ds.version,
    ds.processing_status,
    ds.error_message,

    ds.gmail_message_id,
    ds.gmail_thread_id,
    ds.submitted_by,
    ds.submitted_at,
    ds.created_at,

    ea.subject,
    ea.sender_address,
    ea.recipient_address,
    ea.message_date,
    ea.eml_path,
    ea.eml_sha256,
    ea.eml_size_bytes,
    ea.archived_at,

    ds.source_attachment_path,
    ds.source_attachment_sha256,
    ds.source_sheet,

    (
        SELECT COUNT(*)
        FROM submission_lines sl
        WHERE sl.submission_id = ds.submission_id
    )::INTEGER AS line_count,

    (
        SELECT COUNT(*)
        FROM submission_lines sl
        WHERE sl.submission_id = ds.submission_id
          AND sl.accepted_current_pct IS NOT NULL
    )::INTEGER AS accepted_line_count,

    (
        SELECT COUNT(*)
        FROM validation_issues vi
        WHERE vi.submission_id = ds.submission_id
          AND vi.status = 'OPEN'
    )::INTEGER AS open_issue_count,

    cr.correction_id,
    cr.status AS correction_status,
    cr.attempt AS correction_attempt,
    cr.sent_at AS correction_sent_at,

    dsh.status AS department_status,
    dsh.changed_at AS department_status_changed_at

FROM department_submissions ds
LEFT JOIN email_archives ea
  ON ea.archive_id = ds.archive_id
LEFT JOIN LATERAL (
    SELECT c.correction_id, c.status, c.attempt, c.sent_at
    FROM correction_requests c
    WHERE c.reporting_month = ds.reporting_month
      AND c.legal_entity   = ds.legal_entity
      AND c.department_id   = ds.department_id
    ORDER BY c.attempt DESC
    LIMIT 1
) cr ON TRUE
LEFT JOIN LATERAL (
    SELECT h.status, h.changed_at
    FROM department_status_history h
    WHERE h.reporting_month = ds.reporting_month
      AND h.legal_entity   = ds.legal_entity
      AND h.department_id   = ds.department_id
    ORDER BY h.changed_at DESC, h.status_id DESC
    LIMIT 1
) dsh ON TRUE;

COMMENT ON VIEW vw_pbi_submission_audit_history IS
    'Page 3 - one row per submission version with the full email evidence '
    'chain and the correction request state.';


-- ---------------------------------------------------------------------------
-- Page 2 / Page 4 - Completion trends
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_pbi_completion_trends AS
SELECT
    e.reporting_month,
    e.legal_entity,
    e.department_id,
    e.department_name,
    e.cost_center,

    e.po_number,
    e.po_line,
    e.po_description,
    e.po_line_amount,

    e.previous_completion_pct,
    e.previous_baseline_period,
    e.baseline_status,

    sl.normalized_pct,
    sl.suggested_pct,
    sl.accepted_current_pct,
    sl.change_percentage_points,
    sl.validation_status,
    sl.reason_status,
    sl.remarks,

    ds.gmail_message_id,
    ds.version AS submission_version,

    CASE
        WHEN sl.accepted_current_pct IS NOT NULL THEN sl.accepted_current_pct
        ELSE NULL
    END AS trend_pct

FROM expected_po_lines e
LEFT JOIN submission_lines sl
  ON sl.reporting_month = e.reporting_month
 AND sl.legal_entity   = e.legal_entity
 AND sl.po_number      = e.po_number
 AND sl.po_line        = e.po_line
 AND sl.is_current_version
LEFT JOIN department_submissions ds
  ON ds.submission_id = sl.submission_id;

COMMENT ON VIEW vw_pbi_completion_trends IS
    'Completion trend per PO line per month. trend_pct is NULL when no '
    'accepted value exists, so an unresolved line is never plotted as a '
    'completed value.';


-- ---------------------------------------------------------------------------
-- Reporting freshness helper
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_pbi_reporting_freshness AS
SELECT
    rp.reporting_month,
    MAX(me.created_at) AS last_export_at,
    MAX(me.file_name)   AS last_export_file,
    COUNT(me.export_id)::INTEGER AS export_run_count
FROM reporting_periods rp
LEFT JOIN master_export_runs me
  ON me.reporting_month = rp.reporting_month
 AND me.status = 'SUCCESS'
GROUP BY rp.reporting_month;

COMMENT ON VIEW vw_pbi_reporting_freshness IS
    'Reporting freshness indicator. Import mode Power BI only reflects this '
    'timestamp after a manual or scheduled refresh.';