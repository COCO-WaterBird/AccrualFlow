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
**Status:** OPEN — awaiting your answer.

### What this means, in plain terms

Every month the company sends a list of PO lines to each department and
asks "how far along is this line?" That list is the **expected population**.

The ERP holds a status on every PO line. At the moment:

| `po_status` | PO lines | Cost centres | Amount | Share |
|---|---|---|---|---|
| `Open` | 476 | 6 | 345,241,000 | 95.2 % |
| `On Hold` | 21 | 5 | 17,239,000 | 4.2 % |
| `Closed` | 3 | 3 | 2,204,500 | 0.6 % |
| **Total** | **500** | **6** | **364,684,500** | 100 % |

The question is: **should all 500 lines be on the monthly list, or should
some be left off?**

### Why it matters

- A **Closed** PO is finished. Its completion is settled at 100 % and will
  never change. Asking a department to confirm it every month is work for
  nothing, and it adds rows that can never move — which flattens the
  "did anything change this month" statistics.
- An **On Hold** PO is blocked for reasons often outside the
  department's control. It is still CapEx money that is not being spent,
  so keeping it visible is usually valuable.

Right now **all 500 lines are included**, because the ERP import pulls
every PO line without a status filter and the MVP document defines no
status scope. This is the safe default, but it may not be the right one.

### Options

**A. Report all 500 lines every month** — *status quo; my recommendation.*

- Nothing is filtered, so no line can ever look "missing" just because its
  PO closed.
- The report is a complete picture of the CapEx commitment book.
- Downside: departments keep re-confirming finished work.

**B. Report only `Open` POs — 476 lines.**

- The report tracks only the live portfolio, which is what a manager acts
  on.
- Downside: a PO that closes mid-month disappears from the next month's
  report without a trace. It needs an explicit "closed this period"
  treatment, otherwise the disappearance looks like missing data.

**C. Report `Open` + `On Hold` — 497 lines, exclude `Closed`.**

- A balanced middle: closed work leaves the report, blocked work stays
  visible so the reason for the stall can be chased.
- Downside: same "disappears from the report" issue as B, for the 3
  closed lines.

### Recommendation

**Option A for now.** It is already running, it is the conservative
choice, and it loses nothing. I would rather see how the departments
actually respond to a closed PO being asked about before I narrow the
scope for every future month.

If you prefer B or C, say which and I will add the filter, re-freeze
`2026-11`, and record the excluded lines on the same ledger that already
reports the excluded departments — so they show up in Power BI Page 6 too.

### Consequences

| If you choose | Then |
|---|---|
| A | Nothing changes. `2026-11` stays at 500 lines. |
| B | The population drops to 476 lines. The 24 closed and on-hold lines move to the exclusion ledger, appear in `excluded_departments.csv`, and appear in `vw_pbi_population_scope` as excluded. I need **one more business input**: should a line that closes mid-month show up in its last active month marked "closed", or just disappear? |
| C | The population drops to 497 lines. Same ledger and Power BI treatment. Same follow-up question about a line closing mid-month. |

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
