
CREATE OR REPLACE VIEW vw_po_reminder_list AS
SELECT
    legal_entity,
    po_number,
    po_line_number,
    requester,
    cost_center,
    supplier_name,
    description,
    currency,
    po_line_amount,
    completion_progress,
    cumulative_gr_amount,
    monthly_completion_estimate_pct,

    -- New columns must be appended
    po_status,
    supplier_code

FROM capex_po

WHERE LOWER(TRIM(po_status)) = 'open'
  AND completion_progress < 0.99
  AND monthly_completion_estimate_pct IS NULL;
