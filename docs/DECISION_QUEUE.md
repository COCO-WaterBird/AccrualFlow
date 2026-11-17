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

### The question

The ERP snapshot for entity `2000` contains 500 PO lines:

| `po_status` | PO lines | Currently in the population? |
|---|---|---|
| `Open` | 476 | Yes |
| `On Hold` | 21 | Yes |
| `Closed` | 3 | Yes |

Should all 500 be reported every month, or should some statuses be
excluded?

### Why it matters

A **Closed** PO is finished — its completion is settled and will never
change. Asking a department to confirm it every month adds work and adds
rows that can never move. An **On Hold** PO may be blocked for reasons
outside the department's control.

There is also a comparability cost: a line that can never change makes
the month-over-month "no change" statistics look flatter than the real
movement in the open portfolio.

Right now **all 500 lines are included**, because the ERP import pulls
every PO line without a status filter and the MVP document defines no
status scope. This is the safe default, but it is not necessarily the
right one.

### Options

**A. Report all 500 lines, every month** — *status quo, recommended as
the interim answer.*

- Nothing is filtered, so no line can ever be "missing" purely because
  its PO closed.
- The report is a complete picture of the CapEx commitment book.
- Downside: departments keep re-confirming finished work, and the
  portfolio statistics are diluted.

**B. Report only `Open` POs (476 lines).**

- The report tracks only the live portfolio, which is what a manager
  actually acts on.
- Downside: a PO that closes mid-month disappears from the next month's
  report without a trace. It needs an explicit "closed this period"
  treatment, otherwise the disappearance looks like missing data.

**C. Report `Open` + `On Hold` (497 lines), exclude `Closed`.**

- A balanced middle: closed work leaves the report, blocked work stays
  visible so the reason for the stall can be chased.
- Downside: the same "disappears from the report" issue as B, for the
  three closed lines.

### Recommendation

**Option A for now** — it is already running, it is the conservative
choice, and it loses nothing. Raise this again once the first real
submission cycle has been observed, because the right answer depends on
how the departments actually respond to a closed PO being asked about.

If you prefer B or C, say so and I will add the filter, apply it to
`2026-11`, and record the exclusion in the same ledger that already
reports the excluded departments.

### Consequences

| If you choose | Then |
|---|---|
| A | Nothing changes. `2026-11` stays at 500 lines. |
| B | The population drops to 476 lines. The 24 closed and on-hold lines move into the exclusion ledger, appear in `excluded_departments.csv` with reason `PO_STATUS_NOT_IN_SCOPE`, and appear in `Department_Summary` as excluded. I need **one more piece of business input** from you: should a line that closes mid-month show up in its last active month with a "closed" marker, or just vanish? |
| C | The population drops to 497 lines. The same ledger and reporting treatment applies. |

---

## D-2 — How should finance receive the excluded-department list?

**Priority:** Medium — the data exists and is correct; only the delivery
channel is undecided.

### The question

The excluded departments are currently recorded in three places: the
`population_exclusions` table, a CSV per month, and the `Department_Summary`
sheet of the Master Excel. Should anything more be sent, and to whom?

### Why it matters

You asked that finance know which departments were filtered out. That
requirement is met — the information is recorded and is visible in the
report finance already receives.

The open question is whether finance needs it *proactively*, for example
as a separate note or email to a finance mailbox, rather than having to
open the Master Excel and look at the summary sheet.

### Options

**A. Keep it inside the Master Excel only** — *recommended.*

- Zero extra machinery, no new recipients, no new notification path.
- Finance already receives the Master Excel, and `Department_Summary` now
  carries three new columns that state the scope of the report.
- Downside: finance has to notice the row.

**B. Also publish a monthly finance note** — a short email or a one-page
PDF generated at freeze time listing the excluded departments.

- The exclusions arrive with a push rather than a pull.
- Downside: needs a recipient address, a send path, and — because
  `ACCRUALFLOW_DRY_RUN` currently governs all outbound email — an
  explicit decision about whether this note is sent by the pipeline or
  prepared only.

**C. Expose the exclusions in the Power BI layer** — add a page or a table
built on `population_exclusions`.

- Consistent with how the rest of the reporting works: Power BI reads the
  database views, not the Master Excel.
- Downside: a new view and a new report page, which is more work than the
  value justifies for a list that is normally empty or nearly so.

### Recommendation

**Option A now, C if Power BI reporting becomes a priority.**

For the current dataset the excluded list is a single line
(`2000-900 Procurement`, no PO lines). Building a delivery mechanism for
one line is not justified yet, and option A already satisfies the
requirement.

### Consequences

| If you choose | Then |
|---|---|
| A | Nothing changes. The requirement is met. |
| B | I need: the recipient address, and whether the note is **sent** or only **prepared** (`DRY_RUN`). I will not send anything without your explicit approval. |
| C | I will add a read-only view (e.g. `vw_pbi_population_exclusions`) alongside the existing ones in `014_create_powerbi_reporting_views.sql`. |

---

## Not a decision — for your information only

- **Gmail access needs re-authorising.** The OAuth refresh token was
  revoked at the Google account level; the application cannot repair it.
  Running `python3 -m backend.app.services.gmail_auth_service` once in a
  browser re-authorises it. This is an account action, not a business
  decision, and no email has been sent — `DRY_RUN` is in force.
