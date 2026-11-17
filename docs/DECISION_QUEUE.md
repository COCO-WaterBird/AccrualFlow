# AccrualFlow — Decision Queue

**For:** the Finance Product Owner
**Maintained by:** the Technical Lead
**Last updated:** 2026-11-17

Only **business** decisions appear here. Technical decisions, test runs and
permission prompts are not decisions and are tracked in
[`AGENT_STATUS.md`](./AGENT_STATUS.md).

---

## D-1 — Which PO statuses belong in the monthly report?

**Priority:** High — affects every month from now on.
**Status:** **RESOLVED — 2026-11-17, by the owner.**

### Decision

**Report the `Open` PO lines only.** Every PO line that is filtered out
is recorded on the **same ledger** as the excluded departments, so it
also shows up on **Power BI Page 6**.

A PO line that **closes partway through a reporting month** is marked as
**"已关闭" (Closed)** and **retained in its last active month** — the
month in which the work finished. It leaves the population from the
following month on, and appears on the ledger with its last active month.

### The three rules in plain terms

| # | Rule | Effect on `2026-11` |
|---|---|---|
| 1 | The Python filter keeps only `po_status = 'Open'`. | 500 → **476** lines in the population |
| 2 | Every filtered line goes on the exclusion ledger, same as the excluded departments. | 24 lines on the ledger: 21 `On Hold`, 3 `Closed` |
| 3 | A line that closes mid-month is marked "Closed" and kept in its last active month. | 0 lines today — see *Why no line closed this month* below |

### What was delivered

| Piece | Where |
|---|---|
| The status filter | `scripts/freeze_population.py` → `apply_status_scope()` |
| The scope logic | `backend/app/services/population_status_scope_service.py` |
| The ledger table | `backend/sql/017_create_population_status_scope.sql` → `population_status_exclusions` |
| The closed-line table | same file → `population_closed_lines` |
| The scope record | same file → `population_status_scope` |
| The status on the frozen line | `backend/sql/018_add_expected_po_line_status.sql` → `expected_po_lines.po_status`, `.lifecycle_status` |
| Power BI Page 6 | `backend/sql/019_create_powerbi_population_status_view.sql` → `vw_pbi_population_status_exclusions`, `vw_pbi_population_closed_lines` |
| The Master Excel | `Master_Data` gained `PO Status` and `Line Lifecycle` columns |
| The CSV | `backend/data/runtime/population/<month>/excluded_po_lines.csv` |

### Verified output for `2026-11`

```
Population (Open only) : 476 lines across 6 departments
Filtered onto the ledger:  24 lines  (21 On Hold, 3 Closed)
Excluded departments   :   1         (2000-900 Procurement, NO_PO_LINES)
```

```
cost_center  po_number  po_line  po_status  included_status  excluded_statuses
2000-150     45700045         5  Closed     Open             On Hold, Closed
2000-110     45700162         1  Closed     Open             On Hold, Closed
2000-130     45700214         1  Closed     Open             On Hold, Closed
2000-150     45700003         1  On Hold    Open             On Hold, Closed
```

### Why no line closed this month

The rule "keep a mid-month closure in its last active month" needs the
**ERP status history** to work: it can only recognise a closing when the
ERP has been **imported more than once**.

The ERP has been imported **once** (`2026-11-09`, batch 1, 500 lines).
There is therefore **no status history at all**, and the 3 `Closed` lines
were already closed at the first import — they were never active in this
reporting cycle.

So today:

- `last_active_month` is `NULL` on all 24 ledger rows. It is left
  **unknown** rather than guessed.
- `population_closed_lines` is empty, and `closed_in_month_line_count` is
  `0` for every department.

**From the second ERP import onwards the rule activates by itself**, with
no further decision from you. The machinery is in place and covered by
tests (`tests/test_population_status_scope.py`, 14 tests).

### Consequences

- The monthly report now tracks the **live** portfolio (476 lines).
- The report can no longer silently understate the commitment book: the
  24 filtered lines are on the ledger, in the Master Excel, in the CSV
  and on Power BI Page 6.
- The `last_active_month` column stays empty until the ERP has a second
  import. This is expected, not a fault.

---

## D-2 — How should finance receive the excluded-department list?

**Priority:** Medium.
**Status:** **RESOLVED — 2026-11-17, by the owner.**

### Decision

**Option C — the exclusions are shown in Power BI.**

### What was delivered

Two read-only views were added in
`backend/sql/016_create_powerbi_population_exclusions_view.sql`:

| View | What it answers |
|---|---|
| `vw_pbi_population_scope` | One row per department and month, flagged `INCLUDED` or `EXCLUDED`. The expected line count is `0` for an excluded department. |
| `vw_pbi_population_exclusions` | The excluded departments with the reason, the PO line count in the ERP, the size of the population, and whether finance has been told. |

They belong on **Page 6 — Population scope** of the Power BI report.

Verified output for `2026-11`:

```
cost_center  department                status    reason        expected_lines  authorised_contact
2000-100     Manufacturing Engineering  INCLUDED  -                            159  true
2000-110     Production Automation      INCLUDED  -                             95  true
2000-120     Tooling Engineering        INCLUDED  -                             67  true
2000-130     Facilities Engineering     INCLUDED  -                             52  true
2000-140     Quality Engineering        INCLUDED  -                             58  true
2000-150     IT Infrastructure          INCLUDED  -                             69  true
2000-900     Procurement                EXCLUDED  NO_PO_LINES                   0  false
```

### Why this closes the requirement

Together with the Master Excel sheet and the CSV, the exclusions are now
reachable in three ways, and the Power BI view is the one that answers the
standing question *"who was asked to report, and who was not?"* in a
single table.

The invariant is now enforced by a test: **every** department is either in
the population or on the exclusion ledger. A department can never silently
vanish from the reporting.

---

## Not a decision — for your information only

- **Gmail access needs re-authorising.** The OAuth refresh token was
  revoked at the Google account level; the application cannot repair it.
  Running `python3 -m backend.app.services.gmail_auth_service` once in a
  browser re-authorises it. This is an account action, not a business
  decision, and no email has been sent — `DRY_RUN` is in force.
