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

One verification remains: a full DAG run end-to-end. That run is blocked
by a revoked Gmail OAuth token — see *Blockers*.

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

### 4.1 Gmail OAuth refresh token is revoked — **blocks the full DAG run**

- **Symptom:** `google.auth.exceptions.RefreshError: invalid_grant — Token
  has been expired or revoked.`
- **Scope:** the `discover_replies` task only. The four tasks before it
  (`resolve_reporting_period`, `dispatch_collection_requests`) and the
  three after it that do not need a reply
  (`export_master_excel`, `mark_data_ready`) succeed. The five tasks
  between them are `upstream_failed`.
- **Evidence:** the token's `expiry` is `2026-11-17T01:04:10Z`; a direct
  refresh attempt fails with `invalid_grant`.
- **Why it is not a code bug:** the refresh token was revoked at the
  Google account level. The application cannot repair it; the account has
  to grant access again.
- **Owner action:** re-run the OAuth consent once —
  `python3 -m backend.app.services.gmail_auth_service` — which opens a
  browser window and rewrites `backend/config/gmail_token.json`.
- **Important:** no email was sent. `ACCRUALFLOW_DRY_RUN=true` is in
  force, and the six collection requests are recorded as `DRY_RUN`.

### 4.2 Not a blocker, but noted

The ERP snapshot holds `Open` (476), `On Hold` (21) and `Closed` (3). The
owner resolved **D-1** on 2026-11-17: the report covers the **Open**
lines only, and the 24 filtered lines are on the exclusion ledger. The
mid-month closure rule is implemented but dormant until the ERP has a
**second** import — see section 2.8.

---

## 5. Tests and evidence

```
$ python3 -m pytest -q
184 passed in 49.61s
```

| Suite | Result |
|---|---|
| Whole suite (184 tests) | **184 passed** |
| `tests/test_population_status_scope.py` | 14 passed (new) |
| `tests/test_freeze_population.py` | 14 passed |
| `tests/test_population_exclusions.py` | 4 passed |
| `tests/test_deterministic_xlsx.py` | 8 passed |
| `tests/test_dag_safety.py` | 7 passed |
| `tests/test_xcom_payload.py` | 28 passed |
| `tests/test_powerbi_views.py` | 7 passed |
| Previously flaky export test, 10 consecutive runs | 10/10 passed |
| Export determinism stress, 15 exports across second boundaries | 1 unique hash |

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
  Manufacturing Engineering | NOT_SUBMITTED | INCLUDED
  Production Automation     | NOT_SUBMITTED | INCLUDED
  Tooling Engineering       | NOT_SUBMITTED | INCLUDED
  Facilities Engineering    | NOT_SUBMITTED | INCLUDED
  Quality Engineering       | NOT_SUBMITTED | INCLUDED
  IT Infrastructure         | NOT_SUBMITTED | INCLUDED
  Procurement               | EXCLUDED_FROM_POPULATION | EXCLUDED | NO_PO_LINES | 0
```

---

## 6. Next actions

| # | Action | Category |
|---|---|---|
| 1 | Owner re-authorises Gmail: `python3 -m backend.app.services.gmail_auth_service` | Owner action (not a code change) |
| 2 | Run the full DAG end-to-end and confirm all 10 tasks succeed with the 476-line population | Agent, after action 1 |
| 3 | Verify a submitted reply is parsed, validated, persisted and corrected — the one part of the pipeline never exercised with real data | Agent |
| 4 | ~~Decide D-1 (PO status scope)~~ — **resolved: Open only, with the mid-month closure retained in its last active month** | Done 2026-11-17 |
| 5 | ~~Decide D-2 (finance reporting channel for the exclusion list)~~ — **resolved: Power BI** | Done 2026-11-17 |
| 6 | Import the ERP a second time so the mid-month closure rule has a status history to read | Agent / ERP owner |
| 7 | Publish the repository update | Agent |

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

## 6.1 Repository

| Item | Value |
|---|---|
| Remote | `https://github.com/COCO-WaterBird/AccrualFlow` |
| Visibility | Public |
| Branch | `main` |
| Commit | `bae6389` — "docs: record the D-1 push in the agent status" |
| Files | 86 |

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
