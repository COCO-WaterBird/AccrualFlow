# AccrualFlow — Agent Status

**Last updated:** 2026-11-17
**Owner:** Finance Product Owner
**Agent role:** Technical Lead / Project Manager

---

## 1. Current task

**Apply the owner's PO status scope (D-1) and keep the excluded lines
visible to finance.**

Status: **complete.** The population for `2026-11` was re-frozen under the
`Open`-only scope (500 → **476** lines), the 24 filtered lines were
written onto the exclusion ledger, and they are visible in the Master
Excel, in a CSV, in the database and on **Power BI Page 6**. The
mid-month closure rule is implemented and covered by tests, and it
activates by itself from the second ERP import onwards.

The Gmail OAuth blocker is **cleared** — the account was re-authorised
and a fresh token is in place. The full DAG was then run end to end and
all ten tasks passed.

A **web dashboard** was added on 2026-11-17, because Power BI is
Microsoft's own software and is not part of this repository. It reads
the same nine reporting views that Power BI reads, so the two can never
disagree. See section 2.9.

---

## 2. Completed work

### 2.1 The frozen population (this session)

| Item | Result |
|---|---|
| `reporting_periods` | 1 row — `2026-11`, `population_frozen = TRUE`, `population_row_count = 476` |
| `expected_po_lines` | 476 rows (entity `2000`), all `po_status = 'Open'` |
| `department_contacts` | 6 rows — the authorised recipients |
| `population_exclusions` | 1 row — `2000-900 Procurement` |
| `population_status_exclusions` | 24 rows — the PO lines filtered out by status |

**Recipient rule applied:** the `manager` of the cost centre is the
authorised recipient. It is stored as both `department_owner` and
`authorized_email`.

**Departments in the population (6):**

| Cost centre | Department | Recipient (manager) |
|---|---|---|
| 2000-100 | Manufacturing Engineering | zhang.ke8@northeastern.edu |
| 2000-110 | Production Automation | k.zhang2@northeastern.edu |
| 2000-120 | Tooling Engineering | kzhang@u.circlecat.org |
| 2000-130 | Facilities Engineering | zhangkk5332@gmail.com |
| 2000-140 | Quality Engineering | zhangkk5333@gmail.com |
| 2000-150 | IT Infrastructure | zhangkk5334@gmail.com |

**Department excluded (1):**

| Cost centre | Department | Reason | PO lines |
|---|---|---|---|
| 2000-900 | Procurement | `NO_PO_LINES` | 0 |

`2000-900` exists in `costcenter_mapping` with 12 requester rows but has
**zero** PO lines in the ERP snapshot, so it has nothing to report on. It
was excluded — visibly, not silently.

### 2.2 How finance learns what was filtered out

The exclusion is recorded in **three** places, so it cannot be missed:

1. **`population_exclusions`** (new table, `015_create_population_exclusions.sql`) —
   the queryable ledger: cost centre, department, manager, reason, PO line
   count, plus a `reported_to_finance` flag for follow-up.
2. **`data/runtime/population/2026-11/excluded_departments.csv`** — the file
   that can be sent to finance without database access.
   `expected_population.csv` sits next to it as the frozen population itself.
3. **`Department_Summary` in the Master Excel** — three new columns
   (`Population Status`, `Exclusion Reason`, `PO Lines in ERP`). The
   excluded department appears as a row with
   `Population Status = EXCLUDED` and `Exclusion Reason = NO_PO_LINES`, so
   the report states its own scope instead of silently understating it.

`department_contacts` is rebuilt from the **included** departments only, so
an excluded department is never an authorised contact — it is never asked
to report.

### 2.3 Files created

| File | Purpose |
|---|---|
| `scripts/freeze_population.py` | Freezes the population. `--dry-run` previews, `--month` / `--entity` select the scope. |
| `backend/sql/015_create_population_exclusions.sql` | The exclusion ledger. |
| `backend/app/core/deterministic_xlsx.py` | Byte-stable XLSX packaging (see 2.5). |
| `tests/test_freeze_population.py` | 14 tests — freeze rules. |
| `tests/test_population_exclusions.py` | 4 tests — exclusion visibility. |
| `tests/test_deterministic_xlsx.py` | 8 tests — reproducible checksums. |
| `AGENTS.md` | The operating rules for the agent. |
| `docs/AGENT_STATUS.md`, `docs/DECISION_QUEUE.md` | This file and the owner decision queue. |

### 2.4 Files modified

| File | Change |
|---|---|
| `backend/app/services/master_excel_service.py` | Loads the exclusion ledger; adds the excluded departments to `Department_Summary`; publishes the canonical (reproducible) workbook. |
| `backend/app/services/correction_workbook_service.py` | Publishes the canonical (reproducible) workbook. |
| `docs/ACCRUALFLOW_MVP.md` | New section *5 — The frozen expected population*; renumbered sections; setup now includes the freeze step. |

### 2.5 Bug fixed — the workbook checksum was not reproducible

**Root cause.** An `.xlsx` is a ZIP archive, and a ZIP archive carries a
modification timestamp in the header of every entry. On top of that,
openpyxl writes the current time into `docProps/core.xml`. Two exports of
*identical* data therefore produced *different* files whenever they landed
in a different second — so the `sha256` recorded in `master_export_runs`
proved nothing.

**Evidence.** The pre-existing test
`test_repeated_export_does_not_duplicate_data` failed roughly **1 in 4**
runs. A direct diff showed identical content CRC for every part but
different ZIP entry timestamps and a differing `dcterms:modified`.

**Fix.** `backend/app/core/deterministic_xlsx.py` repackages the workbook
with a fixed ZIP timestamp and a normalised `docProps/core.xml`. The
published file *is* the canonical repackaging, so a reviewer can verify
the checksum with a plain `sha256sum`.

**Verification.**
- 10/10 consecutive runs of the previously flaky test passed.
- A stress run of 15 exports, deliberately crossing second boundaries,
  produced **1 unique hash**.
- 4 back-to-back exports produced 4 identical hashes.

### 2.6 Earlier session work (unchanged, verified)

The Airflow 3.0 XCom dual-writer defect is fixed and covered by
`tests/test_xcom_payload.py` (22 tests): payloads arriving as JSON text
instead of native objects are normalised by `as_dict` / `as_list` /
`as_str` before use.

---

## 3. Running tasks

None. All background shells have finished.

---

## 4. Blockers

**None.** The Gmail OAuth token was re-authorised on 2026-11-17 and the
token file now records the consent time, so the 7 day testing-mode window
is predictable.

### 4.1 Gmail OAuth — the bug that blocked re-authorisation, now fixed

- **Symptom:** running `python3 -m backend.app.services.gmail_auth_service`
  printed `Gmail OAuth: FAILED - ... Token has been expired or revoked` and
  then told the owner to run **the same command again**. The command could
  never succeed: the revoked token raised out of the refresh step, so the
  interactive consent flow was never reached.
- **Root cause:** `get_gmail_service()` refreshed the token *before* it
  considered the consent flow, and the refresh error propagated. A revoked
  token can only be repaired by a new consent, but the code had no path to
  one.
- **Fix:** `get_gmail_service(allow_reauthorise=False)` now takes an
  explicit flag. When the refresh fails with a revoked token **and** the
  flag is set, the stale token is discarded and the flow falls through to
  `InstalledAppFlow.run_local_server(open_browser=True)`, which opens the
  browser and completes the consent. The CLI entry point passes
  `allow_reauthorise=True`; every pipeline caller keeps the default
  `False`, so a DAG run still fails fast and never opens a browser.
- **Verified:** the re-authorisation completed — account
  `finance.engineering.lab@gmail.com`, token written to
  `backend/config/gmail_token.json`, consent time `2026-11-18T00:44:33Z`,
  so the testing-mode window runs to `2026-11-25T00:44:33Z`.
- **Test coverage:** `tests/test_gmail_auth.py` — the revoked-token case
  still raises when the fallback is off, and a new test asserts that the
  consent flow **is** entered when it is on. Test suite: 205 → **206
  passed**.
- **Note:** no email was sent at any point. `ACCRUALFLOW_DRY_RUN=true` is
  in force.

### 4.2 Not a blocker, but noted

The ERP snapshot holds `Open` (476), `On Hold` (21) and `Closed` (3). The
owner resolved **D-1** on 2026-11-17: the report covers the **Open**
lines only, and the 24 filtered lines are on the exclusion ledger. The
mid-month closure rule is implemented but dormant until the ERP has a
**second** import — see section 2.8.

---

## 5. Tests and evidence

```
$ .venv/bin/python -m pytest -q
232 passed in 4.84s
```

| Suite | Result |
|---|---|
| Whole suite (232 tests) | **232 passed** |
| `tests/test_gmail_auth.py` | 22 passed (new) |
| `tests/test_dashboard.py` | 26 passed (new) |
| `tests/test_population_status_scope.py` | 14 passed |
| `tests/test_freeze_population.py` | 14 passed |
| `tests/test_population_exclusions.py` | 4 passed |
| `tests/test_deterministic_xlsx.py` | 8 passed |
| `tests/test_dag_safety.py` | 7 passed |
| `tests/test_xcom_payload.py` | 28 passed |
| `tests/test_powerbi_views.py` | 7 passed |
| Previously flaky export test, 10 consecutive runs | 10/10 passed |
| Export determinism stress, 15 exports across second boundaries | 1 unique hash |

### 5.1 The full DAG run, end to end — **passed**

The blocker was the revoked Gmail token. With the token restored, the
whole pipeline was executed against the throwaway period `2098-11` (87
lines), so the live period `2026-11` was never written to.

```
$ docker exec -e ACCRUALFLOW_REPORTING_MONTH=2098-11 \
    accrualflow-airflow-airflow-scheduler-1 \
    airflow dags test accrualflow_daily_monitoring 2098-11-01

MASTER EXPORT: CapEx_Completion_Master_2098-11.xlsx | expected=87 |
  accepted=76 | exceptions=17 | excluded_departments=0
RECONCILE: expected=87 | accepted=76 | export=87 | reconciled=True |
  collection_complete=True | data_ready=True
DagRun Finished: state=success
```

All **10 tasks** succeeded, including `discover_replies` — the task the
revoked token used to block.

**Nothing was sent.** The collection request for the test period is
recorded as `status = DRY_RUN` with `gmail_message_id = NULL` and
`sent_at = NULL`. `ACCRUALFLOW_DRY_RUN=true` was in force for the whole
run.

**The live period was not touched.** After the run:

```
reporting_periods   2026-11  frozen  476 lines   (unchanged)
expected_po_lines   2026-11  476 rows           (unchanged)
email_requests      2026-11  6 rows, all DRY_RUN, sent_at NULL
```

**The Power BI views read the 476-line population correctly:**

```
vw_pbi_population_scope              8 rows
vw_pbi_population_exclusions          1 row
vw_pbi_population_status_exclusions 24 rows
vw_pbi_population_closed_lines       0 rows   (dormant: one ERP import only)
```

`scripts/run_dag_test.sh [YYYY-MM]` runs the same test. It needs a
terminal with Docker socket access.

> **Run the tests with `.venv/bin/python`, not the system `python3`.** The
> two environments were disjoint: the venv had `google-auth` but no
> `pytest`, the system interpreter had `pytest` but no `google-auth`. A
> run with the system interpreter therefore skipped every database test
> and failed every Gmail test. `pytest` is now installed in the venv, and
> it holds every dependency.

**Database state, verified after the re-freeze:**

```
reporting_periods               1 row   (2026-11, frozen, 476 lines)
expected_po_lines             476 rows
department_contacts              6 rows
population_exclusions            1 row   (2000-900, NO_PO_LINES)
population_status_scope          1 row   (Open / On Hold, Closed)
population_status_exclusions    24 rows  (21 On Hold, 3 Closed)
population_closed_lines          0 rows  (no second ERP import yet)
```

**Probe residue removed.** Two throwaway months (`2096-03`, `2097-05`)
created during diagnosis were deleted. Only `2026-11` data remains.

**Master Excel, verified:**

```
CapEx_Completion_Master_2026-11.xlsx | expected=476 | departments=6 | SUCCESS

Master_Data: 476 rows, PO Status = Open (476), Line Lifecycle = ACTIVE (476)

Department_Summary:
  2000-100 Manufacturing Engineering | INCLUDED | 148
  2000-110 Production Automation     | INCLUDED |  91
  2000-120 Tooling Engineering       | INCLUDED |  63
  2000-130 Facilities Engineering    | INCLUDED |  51
  2000-140 Quality Engineering       | INCLUDED |  57
  2000-150 IT Infrastructure         | INCLUDED |  66
  2000-900 Procurement               | EXCLUDED |    0 | NO_PO_LINES
```

---

## 6. Next actions

| # | Action | Category |
|---|---|---|
| 1 | ~~Owner re-authorises Gmail~~ — **done 2026-11-17, account re-authorised** | Done |
| 2 | ~~Run the full DAG end-to-end and confirm all 10 tasks succeed with the 476-line population~~ — **done: all 10 tasks passed, `data_ready=True`** | Done |
| 3 | Verify a submitted reply is parsed, validated, persisted and corrected — the one part of the pipeline never exercised with real data | Agent |
| 4 | ~~Decide D-1 (PO status scope)~~ — **resolved: Open only, with the mid-month closure retained in its last active month** | Done 2026-11-17 |
| 5 | ~~Decide D-2 (finance reporting channel for the exclusion list)~~ — **resolved: Power BI** | Done 2026-11-17 |
| 6 | Import the ERP a second time so the mid-month closure rule has a status history to read | Agent / ERP owner |
| 7 | Publish the repository update | Agent |
| 8 | ~~Owner wants to see the dashboard~~ — **done: the web dashboard is live at `/dashboard/dashboard.html`** | Done 2026-11-17 |

### 2.7 Power BI Page 6 — the population scope (D-2, resolved)

The owner decided that the excluded departments are shown in **Power BI**.
Two read-only views were added in
`backend/sql/016_create_powerbi_population_exclusions_view.sql`:

| View | What it answers |
|---|---|
| `vw_pbi_population_scope` | One row per department and month, flagged `INCLUDED` / `EXCLUDED` |
| `vw_pbi_population_exclusions` | The excluded departments with the reason and the ERP line count |

Verified for `2026-11`: 6 departments `INCLUDED` (159 / 95 / 67 / 52 / 58 /
69 lines) and 1 `EXCLUDED` (`2000-900 Procurement`, `NO_PO_LINES`, 0 lines,
not an authorised contact).

**Invariant, now enforced by a test:** every department is either in the
population or on the exclusion ledger. A department can never silently
vanish from the reporting.

### 2.8 The PO status scope (D-1, resolved)

The owner decided: **only the `Open` PO lines are reported**, every
filtered line goes onto the **same ledger** as the excluded departments
(so it shows on Power BI Page 6 too), and a line that **closes partway
through a month** is marked "已关闭" and **retained in its last active
month**.

**Population re-frozen under the new scope:**

| Item | Before | After |
|---|---|---|
| `expected_po_lines` | 500 | **476** (all `po_status = 'Open'`) |
| `population_exclusions` | 1 (department) | 1 (department) |
| `population_status_exclusions` | — | **24** (21 `On Hold`, 3 `Closed`) |
| `population_status_scope` | — | 1 (`Open` / `On Hold, Closed`) |
| `population_closed_lines` | — | 0 (see below) |

**Files created:**

| File | Purpose |
|---|---|
| `backend/app/services/population_status_scope_service.py` | The scope logic: filter, last active month, closed-in-month detection, ledger persistence. |
| `backend/sql/017_create_population_status_scope.sql` | `population_status_scope`, `population_status_exclusions`, `population_closed_lines`. |
| `backend/sql/018_add_expected_po_line_status.sql` | `expected_po_lines.po_status` and `.lifecycle_status`. |
| `backend/sql/019_create_powerbi_population_status_view.sql` | `vw_pbi_population_status_exclusions`, `vw_pbi_population_closed_lines`. |
| `tests/test_population_status_scope.py` | 14 tests — the filter, the last active month, the mid-month closure. |

**Files modified:**

| File | Change |
|---|---|
| `scripts/freeze_population.py` | Applies the status scope; writes `excluded_po_lines.csv`. |
| `backend/app/services/submission_persistence_service.py` | `ExpectedPopulationRow` carries `po_status` and `lifecycle_status`; both are persisted. |
| `backend/app/services/completion_pipeline_service.py` | Carries `po_status` into the built population. |
| `backend/app/services/master_excel_service.py` | `Master_Data` gained the `PO Status` and `Line Lifecycle` columns. |
| `backend/sql/016_create_powerbi_population_exclusions_view.sql` | `vw_pbi_population_scope` gained `closed_in_month_line_count`. |

**A semantic bug was found and fixed during the work.** The first
implementation defined "last active month" as the month of the newest
capture in which the line was still *Open*. That is wrong for a line
that closes mid-month: such a line was still active *during* the month it
closed, and that month is the one it must be retained in. The logic was
rewritten around a single classifier, `classify_line_status()`, which
returns the last active month, whether it is known, and whether the line
closed in the reporting month.

**Why the mid-month rule is dormant today.** The rule can only recognise
a closing when the ERP has been **imported more than once**. The ERP has
been imported **once** (`2026-11-09`, batch 1). There is no status
history, so the 3 `Closed` lines were already closed at the first import
and were never active in this cycle. Consequently `last_active_month` is
`NULL` on all 24 ledger rows — it is left unknown rather than guessed —
and `population_closed_lines` is empty. From the second import onwards
the rule activates with no further decision.

**Bug fixed — a view cannot change its column order in place.** Adding
`closed_in_month_line_count` to `vw_pbi_population_scope` failed with
`cannot change name of view column`. The migration now drops and
recreates the view. No report reads from it during the swap.

---

### 2.9 The web dashboard

The owner asked where to see the dashboard. The answer was: **Power BI
is Microsoft's own software and is not in this repository** — the repo
holds the nine reporting *views*, but no `.pbix` report file existed.

Connecting the browser Power BI to a local PostgreSQL is not
straightforward: the Power BI Service needs the Microsoft on-premises
data gateway, and a Pro licence. The Power BI PostgreSQL connector also
requires SSL, while the local database runs with `ssl = off`.

So the project got its own dashboard. It reads **the same nine views**
that Power BI reads, so the dashboard and the official report can never
disagree — and a test asserts exactly that.

```bash
scripts/start_dashboard.sh
#   -> http://127.0.0.1:8000/dashboard/dashboard.html
```

| Tab | View |
|---|---|
| 1 部门总览 | `vw_pbi_department_submission_overview` |
| 2 校验异常 | `vw_pbi_validation_exceptions` |
| 3 提交历史 | `vw_pbi_submission_audit_history` |
| 4 完成率趋势 | `vw_pbi_completion_trends` |
| 5 人口口径 | `vw_pbi_population_scope` + `vw_pbi_population_exclusions` |
| 6 状态口径 | `vw_pbi_population_status_exclusions` + `vw_pbi_population_closed_lines` |

**Files created:**

| File | Purpose |
|---|---|
| `backend/app/services/dashboard_service.py` | The read only data layer over the nine views. |
| `backend/app/static/dashboard.html` | The page. One self contained file, no build step, no CDN. |
| `tests/test_dashboard.py` | 26 tests. |
| `scripts/start_dashboard.sh` | The launcher. |

**Files modified:**

| File | Change |
|---|---|
| `backend/app/main.py` | The 11 read only dashboard routes, and the `/dashboard` static mount. |

**A bug was found and fixed during the work.** The page showed
"正在加载…" forever. The `/months` endpoint returns `{"months": [...]}`
but the JavaScript treated the response as an array, so `months.forEach`
threw and `init()` stopped before it ever reached the render. The
response shape is now handled explicitly.

**Verified in the browser**, all six tabs, both periods:

```
2026-11   476 lines, 6 departments, 24 status exclusions, 0 closed lines
2098-11    87 lines, 1 department,  0 status exclusions, 0 closed lines
```

The dashboard shows the 24 filtered lines of `2026-11` with their reason,
the department that was excluded, and the `last_active_month` marked
"未知" rather than guessed — the same honest state as the ledger.

### 2.10 The dashboard redesign — enterprise North American standard

The dashboard was rebuilt from the original Chinese tab layout into a
modern North American B2B SaaS interface, to the standard of Ramp,
Stripe Dashboard and Linear, using the shadcn/ui token model.

**The stack did not change.** It is still one self contained
`dashboard.html` with vanilla JS and CSS custom properties, served by
FastAPI, reading the same nine PostgreSQL views. No React, no Node, no
bundler, no CDN. The owner ruled that no useless code and no useless
extensions may be added, so a design system was built *inside* the
existing file rather than by adding a toolchain.

| Before | After |
|---|---|
| Horizontal Chinese tab bar | Left sidebar, grouped into **Reporting** and **Scope** |
| Plain number tables | Compact KPI cards with dot indicators and context lines |
| Chinese status text | American English status badges, colour **and** text |
| Proportional figures | `tabular-nums` on every number, percentage and currency |
| Ungrouped rows | Review queues beside the primary tables |
| No live counters | Sidebar badge counts (open exceptions, excluded departments, status ledger rows) |

**The tabs**, all in American English:

| Tab | View |
|---|---|
| Overview | `vw_pbi_department_submission_overview` |
| Exceptions | `vw_pbi_validation_exceptions` |
| Audit Trail | `vw_pbi_submission_audit_history` |
| Trends | `vw_pbi_completion_trends` |
| Population Scope | `vw_pbi_population_scope` + `vw_pbi_population_exclusions` |
| PO Status Scope | `vw_pbi_population_status_exclusions` + `vw_pbi_population_closed_lines` |

**Design rules are now permanent.** `.cursor/rules/project-rules.md`
records the stack constraints, the design system rules, the financial
safety rules and the test rules, so the agent keeps applying them.

**Verified in the browser.** No JavaScript errors on any of the six
pages; the period selector reloads every KPI card, queue and table; the
sidebar collapses into a drawer under 900px. Both periods were checked:
`2026-11` (476 lines, 6 departments, 24 status exclusions) and `2098-11`
(87 lines, 1 department). Full test suite: **232 passed**.

### 2.11 The monthly cycle and the default reporting period

**The dashboard opened on the wrong period.** The owner asked what
`2098-11 Period` meant. It is a **test period** — the `Dry Run` source
in the database says so:

| reporting_month | population_source | rows | departments |
|---|---|---|---|
| `2026-11-01` | Mock ERP + costcenter_mapping | **476** | 6 |
| `2098-11-01` | **Dry Run** | 87 | 1 |

**The bug.** The month list was sorted newest first, and `2098` is
newer than `2026`, so the test period sorted first — and the page
opened on `months[0]`. The owner saw synthetic numbers where a real
reporting result should have been.

**The fix.**

- `default_month()` picks the live period: not a dry run, and not in a
  future year. The API returns it as `default`.
- `is_test_period()` identifies a dry run period. The page shows a
  **warning banner** and the selector labels it `(test)`, so a
  synthetic period can never be mistaken for a real one.

**The monthly collection cycle.** The owner starts one run per reporting
month from the dashboard. The trigger only asks Airflow to start a run;
the pipeline still lives in the DAG, where every task is retryable.
`cycle_progress_service.py` records every stage the moment it finishes,
with the error message when it fails, so the dashboard shows where the
month stands instead of only that something happened.

**Four bugs found and fixed while wiring this up.**

| Bug | Symptom | Fix |
|---|---|---|
| The legal entity was dropped between the tasks | Every task after the ERP import died with a bare `KeyError: 'legal_entity'` | It now travels with the period through the whole chain |
| The reporting month was read from the wrong place | A manual run for `2098-11` collected `2026-11` | Airflow merges the run configuration into the params, so the month sits at the **top level** of them, not under a `conf` key. It is now read from both |
| A test wrote into the live reporting month | The dashboard reported *"The cycle for 2026-11 has a failed stage"* for a month that never ran one | The test now uses a throwaway month (`2099-07`) with a cleanup fixture |
| Two tests asserted on a renamed variable | They failed on a refactor that changed a name, not a behaviour | They now assert the behaviour with a regex |

**Verified against the throwaway period `2098-11`:**

```
resolve_reporting_period   success   -> resolved 2098-11, not 2026-11
sync_erp_snapshot          success   -> 500 PO rows imported
freeze_expected_population up_for_retry -> the population guard fired
```

The `FREEZE` failure was the **idempotency guard working as designed**:
`2098-11` had been frozen with 87 synthetic rows by the earlier dry run,
and the ERP returns 500, so the freeze correctly refused to replace a
frozen population. The throwaway period was then reseeded from the ERP
(476 Open lines across the same 6 departments as the live period), and
the full pipeline ran clean:

```
RECONCILE: expected=476 | accepted=76 | export=476 | reconciled=True
           collection_complete=True | data_ready=True
```

All 8 recorded stages succeeded. The live period `2026-11` was never
written to (476 rows, unchanged).

**A test period is synthetic on two signals**, not one. It is synthetic
when the population source is `Dry Run` **or** when it sits in a future
year. The second signal is the stronger one: a throwaway period may
have been frozen from the ERP by hand, so the source no longer says it
is synthetic, but the year still does. A month the business has not
reached yet cannot be a real reporting month.

Test suite: 232 → **264 passed**.

---

## 6.1 Repository

| Item | Value |
|---|---|
| Remote | `https://github.com/COCO-WaterBird/AccrualFlow` |
| Visibility | Public |
| Branch | `main` |
| Commit | `c9ec5a6` — "the monthly cycle, the progress ledger and the default reporting period" |
| Files | 94 |

**Secrets are excluded.** The OAuth client secret, the OAuth token, the
`.env` file and the whole `backend/config/` folder are in `.gitignore` and
are **not** in the repository. The `.env.example` file carries placeholders
only. The published tree was verified against the remote: 80 files, byte
identical to the local commit, and the credential backup folder is absent.

---

## 7. Change log

| Date | Change |
|---|---|
| 2026-11-17 | Population frozen for `2026-11`: 500 lines, 6 departments, 1 excluded. |
| 2026-11-17 | Exclusion ledger (`population_exclusions`) + CSV + `Department_Summary` columns. |
| 2026-11-17 | Reproducible workbook checksum fixed (`deterministic_xlsx.py`). |
| 2026-11-17 | `AGENTS.md`, `docs/AGENT_STATUS.md`, `docs/DECISION_QUEUE.md` created. |
| 2026-11-17 | Readiness gate fixed: a failed reply discovery no longer reports the period as ready. |
| 2026-11-17 | DAG reporting-month override added, so a test run can target a throwaway period. |
| 2026-11-17 | **Published to GitHub** — `https://github.com/COCO-WaterBird/AccrualFlow` (public, branch `main`, 80 files, commit `7055eba`). |
| 2026-11-17 | **D-2 resolved by the owner** — the excluded departments are shown in Power BI (page 6). Two views added: `vw_pbi_population_scope` and `vw_pbi_population_exclusions`. |
| 2026-11-17 | **D-1 resolved by the owner** — the report covers the `Open` PO lines only. The 24 filtered lines (21 `On Hold`, 3 `Closed`) went onto the exclusion ledger and appear on Power BI Page 6 via `vw_pbi_population_status_exclusions`. The population was re-frozen: 500 → **476** lines. |
| 2026-11-17 | The mid-month closure rule implemented: a line that closes during a reporting month is marked "已关闭" and retained in its last active month, then moves to the ledger the following month. Dormant until the ERP has a second import. |
| 2026-11-17 | `Master_Data` gained the `PO Status` and `Line Lifecycle` columns. Test suite: 170 → **184 passed**. |
| 2026-11-17 | **Pushed to GitHub** — commit `1f3d3b3` on `main`. |
| 2026-11-17 | The revoked Gmail token is now reported as `GmailAuthorizationError` with the remedy, and the 7 day testing-mode expiry is predicted from the recorded consent time. |
| 2026-11-17 | **Dead weight removed.** The lazy-import machinery in the Gmail service, the extra consent environment variable and exception class, the duplicate classification pass in the status scope, the two test-only wrappers, and the two always-constant ledger columns. Test suite: 184 → **205 passed**. |
| 2026-11-17 | `pytest` installed in the venv. The two Python environments were disjoint, so runs with the system interpreter skipped every database test and failed every Gmail test. |
| 2026-11-17 | **Pushed to GitHub** — commit `972f7fb` on `main`. |
| 2026-11-17 | **The Gmail re-authorisation bug is fixed.** The CLI told the owner to re-run a command that could never succeed, because the revoked token raised out of the refresh step before the consent flow could be reached. `get_gmail_service()` now takes `allow_reauthorise`; the CLI sets it, the pipeline does not. The account was re-authorised and a fresh token is in place (window to `2026-11-25`). Test suite: 205 → **206 passed**. |
| 2026-11-17 | **The full DAG ran end to end, all 10 tasks green.** `RECONCILE: expected=87 | accepted=76 | export=87 | reconciled=True | collection_complete=True | data_ready=True`. Run against the throwaway period `2098-11`, so the live `2026-11` period was never written to. Nothing was sent: `ACCRUALFLOW_DRY_RUN=true`, the collection request is `DRY_RUN` with `sent_at = NULL`. `scripts/run_dag_test.sh` runs the same test. |
| 2026-11-17 | **Pushed to GitHub** — commit `2b2f3dd` on `main`. |
| 2026-11-17 | **The web dashboard was built.** The owner asked where to see the dashboard; Power BI is Microsoft's software and is not in the repository (no `.pbix` file existed, and the browser Power BI needs the on-premises data gateway plus a Pro licence to reach a local database). The project now ships its own dashboard on the same nine reporting views, so it can never disagree with the official report. A bug was found and fixed: the page hung on "正在加载…" because the month list response shape was not handled. Verified in the browser, all six tabs, both periods. Test suite: 206 → **232 passed**. |
| 2026-11-17 | **Pushed to GitHub** — commit `fe0beea` on `main`. |
| 2026-11-17 | **The dashboard was redesigned to the enterprise North American standard** (Ramp / Stripe / Linear, shadcn/ui tokens): left sidebar with badge counters, compact KPI cards, review queues, semantic status badges in American English, tabular figures, responsive drawer. The stack is unchanged — one self contained HTML file, no React, no Node, no CDN. The standing project rules were written to `.cursor/rules/project-rules.md`. Verified in the browser: no JavaScript errors on any of the six pages, period switching reloads every panel. Test suite: **232 passed**. |
| 2026-11-17 | **Pushed to GitHub** — commit `fe4118d` on `main`. |
| 2026-11-18 | **The dashboard opened on the wrong period.** The owner asked what `2098-11 Period` meant — it is a `Dry Run` test period, and because it sorts first (2098 > 2026) the page opened on it. Fixed: `default_month()` picks the live period, and a dry run period is now marked with a warning banner and a `(test)` label so it can never be mistaken for a real result. |
| 2026-11-18 | **The monthly collection cycle and the progress ledger** were added: the owner starts one run per reporting month from the dashboard, and every stage is recorded the moment it finishes, with its error message when it fails. |
| 2026-11-18 | **Four bugs found and fixed**: the legal entity was dropped between DAG tasks (`KeyError`); the reporting month was read from the wrong place in the Airflow context (a run for `2098-11` collected `2026-11`); a test wrote its stage rows into the live month and made the dashboard report a false failure; and two tests asserted on a renamed variable. Verified against the throwaway period — the live period `2026-11` was never written to. Test suite: 232 → **261 passed**. |
| 2026-11-18 | **Pushed to GitHub** — commit `c9ec5a6` on `main`. |
| 2026-11-18 | **The throwaway period was reseeded from the ERP.** It had been frozen with 87 synthetic rows by the earlier dry run, and the freeze correctly refused to replace them with the 476 real ones. It now holds the same 476 Open PO lines across the same 6 departments as the live period, and the full pipeline runs clean: `RECONCILE: expected=476 | accepted=76 | export=476 | reconciled=True`, all 8 stages successful, live period untouched. |
| 2026-11-18 | **A test period is synthetic on two signals** — the `Dry Run` source, or a future year. The future year is the stronger one, because a throwaway period may have been frozen from the ERP by hand, in which case the source no longer says it is synthetic but the year still does. The banner now says what a test period *is* rather than how it was filled. Test suite: 261 → **264 passed**. |
| 2026-11-20 | **The monthly cycle now runs.** The DAG was built from the Task SDK class (`airflow.sdk.definitions.dag.DAG`), which has no `next_dagrun_info` — the attribute the core serializer needs to save a parsed DAG. So the processor parsed the file again and again and silently failed on the write, marking the DAG stale each time. From the outside the processor looked healthy. The DAG is now built from the core class (`__DAG_class=CoreDAG`), the Airflow API stopped answering "DAG not found", and a cycle triggered from the dashboard ran all 12 tasks successfully with all 8 stages recorded SUCCESS. The live period was not written to. Test suite: 264 passed. |
| 2026-11-18 | **Pushed to GitHub** — commits `e735b49` and `bbb1ad5` on `main`. |
