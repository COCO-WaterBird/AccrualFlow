# AccrualFlow — Agent Status

**Last updated:** 2026-11-17
**Owner:** Finance Product Owner
**Agent role:** Technical Lead / Project Manager

---

## 1. Current task

**Freeze the expected PO line population for `2026-11` and make the
exclusions visible to finance.**

Status: **substantially complete.** The population is frozen (500 lines,
6 departments), the exclusions are recorded and visible in three places,
and the Master Excel now reports the excluded department explicitly.
One verification remains: a full DAG run end-to-end. That run is blocked
by a revoked Gmail OAuth token — see *Blockers*.

---

## 2. Completed work

### 2.1 The frozen population (this session)

| Item | Result |
|---|---|
| `reporting_periods` | 1 row — `2026-11`, `population_frozen = TRUE`, `population_row_count = 500` |
| `expected_po_lines` | 500 rows (entity `2000`) |
| `department_contacts` | 6 rows — the authorised recipients |
| `population_exclusions` | 1 row — `2000-900 Procurement` |

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

`po_status` in the ERP snapshot is `Open` (476), `On Hold` (21) and
`Closed` (3). **All 500 lines are currently in the population**, because
`po_sync_service` imports every PO line without a status filter and the
MVP document defines no status scope. Whether closed and on-hold POs
should be reported every month is a business rule — it is queued as
**D-1**.

---

## 5. Tests and evidence

```
$ /usr/local/bin/python3.12 -m pytest -q
154 passed in 15.39s
```

| Suite | Result |
|---|---|
| Whole suite (154 tests) | **154 passed** |
| `tests/test_freeze_population.py` | 14 passed |
| `tests/test_population_exclusions.py` | 4 passed |
| `tests/test_deterministic_xlsx.py` | 8 passed |
| Previously flaky export test, 10 consecutive runs | 10/10 passed |
| Export determinism stress, 15 exports across second boundaries | 1 unique hash |

**Database state, verified after cleanup:**

```
reporting_periods      1 row   (2026-11, frozen, 500 lines)
expected_po_lines    500 rows
department_contacts     6 rows
population_exclusions  1 row   (2000-900, NO_PO_LINES)
email_requests          6 rows  (all DRY_RUN, one per manager)
```

**Probe residue removed.** Two throwaway months (`2096-03`, `2097-05`)
created during diagnosis were deleted. Only `2026-11` data remains.

**Master Excel, verified:**

```
CapEx_Completion_Master_2026-11.xlsx | expected=500 | departments=6 | SUCCESS

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
| 2 | Run the full DAG end-to-end and confirm all 10 tasks succeed with the 500-line population | Agent, after action 1 |
| 3 | Verify a submitted reply is parsed, validated, persisted and corrected — the one part of the pipeline never exercised with real data | Agent |
| 4 | Decide D-1 (PO status scope) and, if needed, add a status filter to the freeze | Owner decision |
| 5 | Decide D-2 (finance reporting channel for the exclusion list) | Owner decision |

---

## 7. Change log

| Date | Change |
|---|---|
| 2026-11-17 | Population frozen for `2026-11`: 500 lines, 6 departments, 1 excluded. |
| 2026-11-17 | Exclusion ledger (`population_exclusions`) + CSV + `Department_Summary` columns. |
| 2026-11-17 | Reproducible workbook checksum fixed (`deterministic_xlsx.py`). |
| 2026-11-17 | `AGENTS.md`, `docs/AGENT_STATUS.md`, `docs/DECISION_QUEUE.md` created. |
