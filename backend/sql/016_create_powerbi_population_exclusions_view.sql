-- ===========================================================================
-- AccrualFlow - Power BI view for the population exclusions.
--
-- A department that was filtered out of the frozen expected population was
-- never asked to report.  Finance still has to be able to see that the
-- department exists, that it was deliberately left out, and why.
--
-- Without this view the excluded department would simply be absent from
-- the department overview, and the report would silently understate the
-- scope of the collection.
--
-- The view is READ ONLY, like the rest of the Power BI reporting layer.
--
--     GRANT SELECT ON vw_pbi_population_exclusions TO accrualflow_pbi;
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- Page 6 - Population scope and the excluded departments
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_pbi_population_exclusions AS
SELECT
    pe.reporting_month,
    pe.legal_entity,

    pe.cost_center,
    pe.department_name,
    pe.manager,

    pe.exclusion_reason,
    pe.po_line_count,

    -- Whether finance has been told about this exclusion.
    pe.reported_to_finance,
    pe.reported_at,

    pe.created_at,

    -- The size of the population the exclusion applies to.  It makes the
    -- proportion visible without needing a second query.
    (
        SELECT COUNT(*)
        FROM expected_po_lines e
        WHERE e.reporting_month = pe.reporting_month
          AND e.legal_entity   = pe.legal_entity
    )::INTEGER AS population_line_count,

    (
        SELECT COUNT(*)
        FROM expected_po_lines e
        WHERE e.reporting_month = pe.reporting_month
          AND e.legal_entity   = pe.legal_entity
          AND e.cost_center    = pe.cost_center
    )::INTEGER AS included_line_count_for_cost_center,

    -- A department that is on the exclusion ledger must never be an
    -- authorised contact: it is never asked to report.
    EXISTS (
        SELECT 1
        FROM department_contacts dc
        WHERE dc.legal_entity = pe.legal_entity
          AND dc.cost_center  = pe.cost_center
          AND dc.active
    ) AS is_authorised_contact

FROM population_exclusions pe;

COMMENT ON VIEW vw_pbi_population_exclusions IS
    'Page 6 - the departments that were excluded from a frozen expected '
    'population, with the reason. Together with the department overview '
    'this view states the full scope of the collection: every department is '
    'either in the population or on this ledger.';


-- ---------------------------------------------------------------------------
-- Page 6 - The full population scope
--
-- One row per department and reporting month, whether the department is in
-- the population or was excluded from it.  This is the single view that
-- answers the question finance actually asks: "who was asked to report, and
-- who was not?"
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_pbi_population_scope AS
WITH included AS (
    SELECT
        e.reporting_month,
        e.legal_entity,
        e.cost_center,
        e.department_name,
        e.department_owner,
        COUNT(*)::INTEGER AS expected_line_count
    FROM expected_po_lines e
    GROUP BY
        e.reporting_month,
        e.legal_entity,
        e.cost_center,
        e.department_name,
        e.department_owner
)
SELECT
    i.reporting_month,
    i.legal_entity,
    i.cost_center,
    i.department_name,
    i.department_owner,
    'INCLUDED'                          AS population_status,
    NULL::VARCHAR                       AS exclusion_reason,
    i.expected_line_count,
    TRUE                                AS is_authorised_contact
FROM included i

UNION ALL

SELECT
    pe.reporting_month,
    pe.legal_entity,
    pe.cost_center,
    pe.department_name,
    pe.manager,
    'EXCLUDED'                          AS population_status,
    pe.exclusion_reason,
    0                                   AS expected_line_count,
    FALSE                               AS is_authorised_contact
FROM population_exclusions pe;

COMMENT ON VIEW vw_pbi_population_scope IS
    'Page 6 - the full scope of the collection. One row per department and '
    'reporting month, flagged as INCLUDED or EXCLUDED. The expected line '
    'count is zero for an excluded department, because it was never asked '
    'to report.';
