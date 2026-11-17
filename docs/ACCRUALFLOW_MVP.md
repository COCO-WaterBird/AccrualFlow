# AccrualFlow AI — CapEx Completion Submission Automation

Deterministic, evidence-first automation of the monthly CapEx completion
collection cycle.

> **No guessing.** Every percentage is interpreted from the submitted cell
> value, its Excel number format and its column-wide context. Nothing is
> inferred, repaired or silently accepted.

---

## 1. What the system does

1. Freezes the **expected PO line population** per reporting month.
2. Discovers the inbound replies of the original request threads.
3. **Archives the original raw `.eml` and the attached workbook** — before
   any validation, so even a failed submission keeps its evidence.
4. Parses the workbook losslessly (raw value, number format, data type,
   sheet, row are all preserved).
5. Validates every line against the frozen population and the previous
   finalized baseline.
6. Persists the submission as an immutable **version**.
7. Issues a **correction request** that contains only the unresolved lines.
8. Publishes the consolidated **Master Excel** and exposes the reporting
   views for **Power BI**.

---

## 2. The percentage contract

A completion percentage is stored as a `DECIMAL` **fraction in `[0, 1]`**.
`0.45` means 45 %. The canonical column and the Excel display format never
disagree, because the export re-applies the `0.00%` display format.

### Interpretation table

| Submitted cell | Number format | Result | Status |
|---|---|---|---|
| `0.45` | `0%` | `0.45` | `ACCEPTED` |
| `45%` (text) | any | `0.45` | `ACCEPTED` |
| `45` | `General` | suggested `0.45` | `FORMAT_CONFIRMATION_REQUIRED` |
| `0.45` | `General` | suggested `0.45` | `FORMAT_CONFIRMATION_REQUIRED` |
| `0` or `1` | `General` | suggested `0` / `1` | `FORMAT_CONFIRMATION_REQUIRED` |
| `120` | any | out of range | `INVALID` |
| `-5` | any | negative | `INVALID` |
| `forty` | any | not numeric | `INVALID` |
| `=A1/B1` | any | formula not executed | `INVALID` |
| `#VALUE!` | any | Excel error | `INVALID` |
| blank | any | missing | `MISSING` |

**Column-wide diagnostics** raise the confidence of an ambiguous value from
`LOW` to `MEDIUM`, but they never turn an ambiguous value into an accepted
one. Historical similarity is evidence, not proof.

### Why the exception codes are separated

`validation_status`, `reason_status`, `format_confirmation_status` and
`review_status` are four different columns. A line that is perfectly valid
can still be waiting for a business review, and a line that is invalid can
have a perfectly good explanation attached. Conflating them makes the
department status meaningless.

---

## 3. The month-over-month comparison

The baseline is the **previous finalized** value of the same PO line:

```
reporting period + entity + PO number + PO line
```

The change is expressed in **percentage points**:

```
delta_pp = (current_fraction - previous_fraction) * 100
```

| Situation | `reason_status` | Correction requested? |
|---|---|---|
| Completion increased or unchanged | `NOT_APPLICABLE` | No |
| Completion decreased, remark supplied | `DECREASE_REASON_PROVIDED` | No — review only |
| Completion decreased, remark blank | `DECREASE_REASON_MISSING` | **Yes** |
| No previous baseline exists | `NOT_APPLICABLE` + `BASELINE_MISSING` | No |

A missing baseline is **never** treated as 0 %. It is reported explicitly
and it never blocks an otherwise valid submitted value.

---

## 4. Department status

A department reaches `COMPLETED` only when **all** of the following hold:

- every expected PO line has an accepted value,
- no line is missing, invalid or format-ambiguous,
- no business review is pending,
- the evidence archive (raw EML + source workbook) exists,
- the Master Excel for the month was published successfully.

| Status | Meaning |
|---|---|
| `NOT_SUBMITTED` | No reply received |
| `RECEIVED` / `VALIDATING` | In flight |
| `VALIDATION_FAILED` | Missing or invalid lines |
| `FORMAT_CONFIRMATION_REQUIRED` | Ambiguous percentage representation |
| `CORRECTION_PREPARED` | Correction draft written (DRY_RUN) |
| `CORRECTION_REQUESTED` | Correction actually sent |
| `RESUBMITTED` | A new version arrived |
| `DATA_COMPLETE_REVIEW_PENDING` | All lines accepted, review still open |
| `COMPLETED` | Fully accepted and evidenced |

---

## 5. The frozen expected population

The frozen population is the authority the whole pipeline validates against.
It is built once per reporting month from the ERP master data and never
changes afterwards, so that a report can always be reconciled against the
exact set of lines that was asked for.

```bash
python3 scripts/freeze_population.py --month 2026-11            # freeze
python3 scripts/freeze_population.py --month 2026-11 --dry-run  # preview
```

### The rules

| Rule | Behaviour |
|---|---|
| **Source** | `capex_po` joined with `costcenter_mapping` for one legal entity |
| **Recipient** | The `manager` of the cost centre is the authorised recipient, stored as both `department_owner` and `authorized_email` |
| **Empty departments** | A cost centre with **no PO line** is excluded from the population — it has nothing to report on |
| **Unmapped cost centres** | PO lines whose cost centre is not in the mapping are excluded and reported, never silently dropped |
| **Ambiguous managers** | A cost centre with two different managers aborts the freeze — the recipient would be ambiguous |
| **Previous baseline** | Taken only from the **finalized** baselines of the previous month; a missing baseline stays `MISSING` and is never defaulted to 0 |
| **Idempotency** | Re-running with the same content is a no-op; re-running with different content is refused |

### How finance learns what was filtered out

The exclusion of a department is a financial fact, so it is never silent.
Every excluded department is recorded in three places:

1. **`population_exclusions`** — the queryable ledger, with the cost centre,
   the department, the manager, the reason and the PO line count.
2. **`data/runtime/population/<YYYY-MM>/excluded_departments.csv`** — the
   file that can be sent to finance without database access.
3. **`Department_Summary` in the Master Excel** — the excluded department
   appears with `Population Status = EXCLUDED` and the reason, so the
   report states its own scope.

`department_contacts` is rebuilt from the included departments only. A
department that was excluded is never an authorised contact, because it is
never asked to report.

---

## 6. Layout

```
backend/
  app/
    core/config.py                        # paths, DRY_RUN switch, DB settings
    main.py                               # read-only FastAPI endpoints
    services/
      completion_parser_service.py        # lossless workbook parsing
      percentage_validation_service.py   # deterministic interpretation
      month_comparison_service.py         # MoM + decrease reasons
      email_archive_service.py            # raw .eml + attachment archiving
      submission_persistence_service.py   # transactional, idempotent storage
      completion_pipeline_service.py      # the orchestrator
      correction_workbook_service.py      # correction-only workbook
      correction_email_service.py         # draft + DRY_RUN gate
      department_status_service.py        # status computation
      master_excel_service.py             # 4-sheet atomic export
      collection_request_service.py       # the outbound request cycle
      gmail_reply_service.py              # thread-correlated discovery
  sql/
    012_create_expected_population.sql   # frozen population + baselines
    013_create_submission_pipeline.sql    # the submission pipeline schema
    014_create_powerbi_reporting_views.sql
    015_create_population_exclusions.sql # the exclusion ledger for finance
  data/runtime/                           # writable runtime root
    email_archive/<YYYY-MM>/<msg_id>/*.eml
    attachments/<YYYY-MM>/<msg_id>/*.xlsx
    corrections/<YYYY-MM>/*.xlsx + *.json
    population/<YYYY-MM>/expected_population.csv
    population/<YYYY-MM>/excluded_departments.csv
    reports/<YYYY-MM>/CapEx_Completion_Master_<YYYY-MM>.xlsx
airflow/dags/accrualflow_daily_monitoring.py
scripts/dry_run_e2e.py                   # offline end-to-end dry run
scripts/freeze_population.py             # freeze the expected population
tests/                                    # 156 tests
```

---

## 7. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ACCRUALFLOW_DB_HOST` | `127.0.0.1` | PostgreSQL host |
| `ACCRUALFLOW_DB_PORT` | `5433` | PostgreSQL port |
| `ACCRUALFLOW_DB_USER` | `accrualflow` | PostgreSQL user |
| `ACCRUALFLOW_DB_PASSWORD` | — | **required** |
| `ACCRUALFLOW_DB_NAME` | `mock_erp_db` | Mock ERP database |
| `ACCRUALFLOW_MAIN_DB_NAME` | `accrualflow_db` | AccrualFlow database |
| `ACCRUALFLOW_DATA_DIR` | `backend/data/runtime` | Writable runtime root |
| `ACCRUALFLOW_DRY_RUN` | `true` | **Master outbound-email switch** |
| `ACCRUALFLOW_MAX_CORRECTION_ATTEMPTS` | `3` | Correction retry cap |
| `ACCRUALFLOW_REPORTING_TIMEZONE` | `America/Los_Angeles` | Business timezone |

### The DRY_RUN switch

While `ACCRUALFLOW_DRY_RUN=true` (the default):

- correction workbooks and email drafts **are** written to disk,
- the correction request is recorded as `PREPARED`,
- **nothing is ever sent** — the Gmail API is not even called.

Only `ACCRUALFLOW_DRY_RUN=false` allows a real send, and the state becomes
`SENT` only after the Gmail API confirms the send.

---

## 8. Setup

```bash
# 1. Database
docker compose up -d postgres

# 2. Schema
for f in backend/sql/*.sql; do
  PGPASSWORD=local_dev_password psql -h 127.0.0.1 -p 5433 \
    -U accrualflow -d accrualflow_db -v ON_ERROR_STOP=1 -f "$f"
done

# 3. Environment
cp .env.example .env    # then set ACCRUALFLOW_DB_PASSWORD

# 4. Tests
python3 -m pytest -q

# 5. Freeze the expected population for the month
python3 scripts/freeze_population.py --month 2026-11

# 6. Offline end-to-end dry run
python3 scripts/dry_run_e2e.py
```

---

## 9. The Power BI reporting layer

Power BI reads the **database views**, not the Master Excel. Import mode
with a scheduled refresh keeps the numbers and the audit trail in sync.

| View | Page |
|---|---|
| `vw_pbi_department_submission_overview` | 1 — Department submission overview |
| `vw_pbi_validation_exceptions` | 2 — Validation exceptions |
| `vw_pbi_submission_audit_history` | 3 — Submission history / audit trail |
| `vw_pbi_completion_trends` | 4 — Completion trends |
| `vw_pbi_reporting_freshness` | Freshness indicator |

The views expose `suggested_pct` separately from `accepted_current_pct`, so
a suggestion is never presented to a reviewer as an accepted number.

Grant the reporting role read-only access:

```sql
CREATE ROLE accrualflow_pbi LOGIN PASSWORD '...';
GRANT USAGE ON SCHEMA public TO accrualflow_pbi;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO accrualflow_pbi;
```

> The Power BI Service refresh is **not** triggered from Airflow. It needs
> its own credentials and permissions; the DAG only marks the data as ready.

---

## 10. The Airflow DAG

`accrualflow_daily_monitoring` runs daily at 09:00 Pacific.

```
resolve_reporting_period
  → dispatch_collection_requests
  → discover_replies
  → register_and_deduplicate
  → archive_raw_emails_and_attachments
  → parse_validate_and_persist
  → generate_correction_workbooks_and_drafts
  → update_department_status
  → export_master_excel
  → mark_data_ready
```

`dispatch_collection_requests` is idempotent: it updates the existing
`email_requests` row for a department instead of creating a second request.
The outbound thread ids it stores are the correlation key for the replies.

Every task is retry safe. The Gmail message id is the idempotency key, so a
repeated run never creates a second submission. A single corrupt workbook
never blocks the other messages: each message is processed inside its own
`try/except` and the failures are collected.

Only identifiers and paths travel through XCom — never binary payloads.

### The collection request

`backend/app/services/collection_request_service.py` builds the workbook
that each department receives. It contains **exactly** the PO lines of that
department, taken from the frozen population, and the completion cell is
pre-formatted as `0.00%`. That removes the ambiguity at the source: the
department types `45` and Excel stores `0.45` with a percent format.

Run it manually with:

```bash
python3 -m backend.app.services.collection_request_service --month 2026-11
# Add --send to actually send. Without it, only the drafts are prepared.
```

---

## 11. The Master Excel

Four sheets, published atomically (written to a temporary file, re-opened,
validated, then moved into place).

| Sheet | Content |
|---|---|
| `Master_Data` | Every expected PO line, including the unresolved ones |
| `Validation_Exceptions` | Every exception, with the raw evidence |
| `Department_Summary` | Per department counts and completion rate |
| `Email_Audit_Log` | Every archived message with the full evidence chain |

`Master_Data` always contains one row per **expected** PO line. An
unresolved line is present with a blank accepted value — it is never dropped
and never receives a fabricated value.

---

## 12. The mandatory test matrix

| # | Scenario | Test file |
|---|---|---|
| 1 | Percent-formatted `0.45` → 45 % | `test_percentage_validation.py` |
| 2 | Text `"45%"` → 45 % | `test_percentage_validation.py` |
| 3 | Unformatted integer `45` → suggested | `test_percentage_validation.py` |
| 4 | Unformatted decimal `0.45` → suggested | `test_percentage_validation.py` |
| 5 | Column-wide diagnostics | `test_percentage_validation.py` |
| 6 | 0/1, negative, >100 %, text, blank, formula, error | `test_percentage_validation.py` |
| 7 | Range validation | `test_percentage_validation.py` |
| 8 | Decrease with a remark → review pending | `test_month_comparison.py` |
| 9 | Decrease without a remark → correction | `test_month_comparison.py` |
| 10 | Missing baseline → explicit flag | `test_month_comparison.py` |
| 11 | 87 expected / 10 issues → 10 correction rows | `test_correction_workflow.py` |
| 12 | (merged into 11) | |
| 13 | Corrupted / empty / wrong-type attachment | `test_correction_workflow.py` |
| 14 | Same message twice → one submission | `test_pipeline_integration.py` |
| 15 | Original request excluded, unauthorized sender | `test_email_archive.py` |
| 16 | EML filename sanitization, no collisions | `test_email_archive.py` |
| 17 | Evidence archived even when validation fails | `test_pipeline_integration.py` |
| 18 | DRY_RUN prepares but never sends | `test_correction_workflow.py` |
| 19 | Live send only after the API confirms | `test_correction_workflow.py` |
| 20 | Resubmission updates only the intended lines | `test_pipeline_integration.py` |
| 21 | Department cannot be COMPLETED prematurely | `test_pipeline_integration.py` |
| 22 | Master Excel has 4 sheets and every line | `test_pipeline_integration.py` |
| 23 | Repeated export does not duplicate data | `test_pipeline_integration.py` |
| 24 | One department's error does not block others | `test_pipeline_integration.py` |

Run the suite:

```bash
python3 -m pytest -q
```

---

## 13. Operational notes

- The Gmail OAuth token lives in `backend/config/gmail_token.json`. It is
  mounted read-write into the Airflow container because it is refreshed in
  place. **A revoked refresh token cannot be repaired by the application** —
  the account has to grant access again, otherwise the `discover_replies`
  task fails with `invalid_grant`.
- The source tree is mounted **read-only** into Airflow. Every runtime
  artefact goes to `ACCRUALFLOW_DATA_DIR`.
- **The workbook checksum is reproducible.** An `.xlsx` is a ZIP archive and
  carries a timestamp in every entry header; openpyxl also stamps the save
  time into the document properties. Both are normalised by
  `backend/app/core/deterministic_xlsx.py`, so the `sha256` of the Master
  Excel and of a correction workbook is a pure function of the exported
  content. A reviewer can verify it with a plain `sha256sum`.
