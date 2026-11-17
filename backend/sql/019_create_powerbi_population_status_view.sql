-- ===========================================================================
-- AccrualFlow - Power BI view for the PO status scope.
--
-- The owner decided that the monthly report covers the OPEN PO lines only.
-- Every PO line that was filtered out has to be visible, on the same
-- ledger as the excluded departments.
--
-- A line that closes DURING a reporting month is not filtered out.  It
-- stays in that month's population, marked as closed, so the month in
-- which the work finished still shows the line and its final accepted
-- value.  From the next month on, the line is on the ledger with its
-- last active month.
--
-- The view is READ ONLY, like the rest of the Power BI reporting layer.
--
--     GRANT SELECT ON vw_pbi_population_status_exclusions
--         TO accrualflow_pbi;
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- Page 6 - The PO lines that were filtered out by their status
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_pbi_population_status_exclusions AS
SELECT
    pse.reporting_month,
    pse.legal_entity,

    pse.cost_center,
    pse.department_name,
    pse.manager,

    pse.po_number,
    pse.po_line,
    pse.po_status,
    pse.po_description,
    pse.po_line_amount,
    pse.currency,

    -- The last month in which the line was part of the reporting scope.
    -- NULL when the ERP has not been imported twice, so that no status
    -- change could be observed yet.
    pse.last_active_month,

    -- Whether the line closed during the reporting month itself.  Such a
    -- line is retained in that month's population.
    pse.closed_in_reporting_month,

    -- Whether finance has been told about the exclusion.
    pse.reported_to_finance,
    pse.reported_at,

    pse.created_at,

    -- The status scope that was applied to the population of that month.
    pss.included_status,
    pss.excluded_statuses,

    -- The size of the population the exclusion applies to.
    (
        SELECT COUNT(*)
        FROM expected_po_lines e
        WHERE e.reporting_month = pse.reporting_month
          AND e.legal_entity   = pse.legal_entity
    )::INTEGER AS population_line_count,

    -- How many lines of this cost center are in the population.
    (
        SELECT COUNT(*)
        FROM expected_po_lines e
        WHERE e.reporting_month = pse.reporting_month
          AND e.legal_entity   = pse.legal_entity
          AND e.cost_center    = pse.cost_center
    )::INTEGER AS included_line_count_for_cost_center

FROM population_status_exclusions pse

LEFT JOIN population_status_scope pss
       ON pss.reporting_month = pse.reporting_month
      AND pss.legal_entity   = pse.legal_entity;

COMMENT ON VIEW vw_pbi_population_status_exclusions IS
    'Page 6 - the PO lines that were filtered out of a frozen population '
    'by their ERP status, with the reason and the last month in which they '
    'were active. Together with the population scope view this states the '
    'full scope of the collection: every PO line is either in the '
    'population or on this ledger.';


-- ---------------------------------------------------------------------------
-- Page 6 - The PO lines that closed during the reporting month
--
-- These lines are IN the population of their last active month.  They are
-- marked as closed so that finance can see the work finished, and so that
-- the next month can exclude them with a traceable reason.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_pbi_population_closed_lines AS
SELECT
    pcl.reporting_month,
    pcl.legal_entity,

    pcl.cost_center,
    pcl.department_name,

    pcl.po_number,
    pcl.po_line,
    pcl.po_status,
    pcl.po_description,
    pcl.po_line_amount,
    pcl.currency,

    -- The month the line is retained in.  It is the month in which the
    -- work finished.
    pcl.last_active_month,
    pcl.closed_at,

    -- The completion the department reported for the line in that month.
    e.previous_completion_pct AS last_reported_completion_pct,

    pcl.created_at

FROM population_closed_lines pcl

LEFT JOIN expected_po_lines e
       ON e.reporting_month = pcl.reporting_month
      AND e.legal_entity   = pcl.legal_entity
      AND e.po_number      = pcl.po_number
      AND e.po_line        = pcl.po_line;

COMMENT ON VIEW vw_pbi_population_closed_lines IS
    'Page 6 - the PO lines that closed during a reporting month. They stay '
    'in the population of that month, marked as closed, so the month in '
    'which the work finished still shows the line and its final accepted '
    'value.';
