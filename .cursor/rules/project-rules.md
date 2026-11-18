# AccrualFlow — Project Rules

These rules are the standing specification for this repository.  Every
agent working on AccrualFlow reads them before touching code.

They complement the repository-level `AGENTS.md`, which governs the
relationship with the Finance Product Owner.  This file governs how the
**code** is written.

---

## 1. The stack, and why it does not change

| Layer | Technology | Note |
|---|---|---|
| Database | PostgreSQL 17 | Source of truth |
| Reporting | 9 read-only SQL views | `vw_pbi_*` |
| API | FastAPI | Read only endpoints |
| Frontend | **One self-contained HTML file** | `backend/app/static/dashboard.html` |
| Orchestration | Airflow 3 (Docker) | `accrualflow_daily_monitoring` |
| Tests | pytest, in `.venv` | `.venv/bin/python -m pytest` |

**There is no React, no node_modules, no build step.**  The dashboard is
one HTML file with inline CSS and vanilla JavaScript.  That is a
deliberate decision, not an accident.

### Why no framework

The owner is explicit: *"功能够用就行,不需要无用的代码,不需要无用的
拓展"* — functionality just needs to work; no useless code, no unnecessary
extensions.  A React toolchain would add a `package.json`, a lockfile, a
bundler, a CI node step and a `node_modules` tree to a project whose
entire frontend is served by FastAPI from one file.  That is dead weight.

### The consequence

- **Do not add** `package.json`, `node_modules/`, a bundler, or a CDN
  `<script>` tag.  The dashboard must work with no network.
- **Do not** add a Python dependency for the frontend.  No Jinja
  templates, no React, no chart library.  SVG and CSS only.
- **Reuse** the existing `esc()`, `tag()`, `table()`, `bar()` helpers in
  the page before writing anything new.
- The one place a new file is legitimate is a **new** page or a **new**
  endpoint, with a matching test.

---

## 2. The design system

The visual language follows modern North American B2B SaaS: think Ramp,
Stripe Dashboard, Linear.  Not copies of them — the *principles*.

### 2.1 Layout

- **Persistent left sidebar** for navigation (5–15 sections).  Not a
  horizontal tab row.  A tab row belongs to a lighter product; AccrualFlow
  has six sections and a month selector, which is a sidebar's job.
- **Top band**: 3–5 compact KPI cards.  The reader must know the state of
  the month within two seconds of loading.
- **Middle**: charts and tables that explain.
- **Bottom**: the actionable work queue.  Review queues belong low, where
  someone scrolls deliberately to work.
- The sidebar is **visually quiet** — a few notches dimmer than the
  content area.  The content is where people work; navigation recedes.

### 2.2 Density

AccrualFlow's users are **operators**: finance and engineering staff who
live in the numbers all day.  Density wins over sparseness, but it must
stay readable.

- Base font size **13px**, not 14–16px.  Table text 13px.
- Compact row padding — roughly `8px 12px`.
- KPI card values around **22–26px**, labels 12px, muted.
- The dashboard must show ~15 table rows without scrolling on a
  1080p screen.  That is the density test.

### 2.3 Colour

Colour is **reserved for status**.  If everything is coloured, nothing is.

- Use **semantic status tokens**, never raw palette colours:
  `success`, `warning`, `info`, `destructive`, plus `neutral`/`muted`.
- A status is always **colour + text**.  Colour is never the only signal.
  This is an accessibility requirement, not a style preference.
- Grey for structure.  One accent (indigo/blue) for interactive elements
  and the active nav item.  Green only for accepted/complete.  Amber for
  attention needed.  Red only for a real failure.
- Borders are subtle — low contrast, rounded, so they orient without
  cluttering.

### 2.4 Typography

- System font stack (no webfont download).
- Numbers use `font-variant-numeric: tabular-nums` so columns align.
- Tabular figures on **every** metric, percentage and amount.  This is
  non-negotiable in a financial application.
- Label case for column headers and nav items.  Sentence case for prose.

### 2.5 Language

**Professional American English for every user-facing label.**  No
machine-translated text, no mixed languages in one screen.

| Do not write | Write |
|---|---|
| 部门总览 | Department Overview |
| 被排除的部门 | Excluded Departments |
| 最后活跃月（未知） | Last Active Month (Unknown) |
| 校验异常 | Validation Exceptions |

Status values come from the database and stay as they are — `Open`,
`On Hold`, `Closed` are ERP values, not UI copy.  UI chrome around them
is English.

---

## 3. Rules that protect the money

These are **not** design choices.  A redesign that breaks one of these is
a regression, not an improvement.

1. **Never change a calculation.**  Percentages stay fractions in `[0,1]`
   in the database and are formatted for display only.
2. **Never change a status transition or threshold.**  Baselines,
   population scope and approval rules come from the MVP document or the
   owner.
3. **Never change the population scope.**  `Open` lines only, with the
   filtered lines on the exclusion ledger, and a mid-month closure
   retained in its last active month.
4. **Never let a proposed value be shown as an accepted value.**  The
   suggested percentage and the accepted percentage are separate columns,
   always.
5. **Never hide a department or a PO line.**  Every department is either
   in the population or on the exclusion ledger.  Silence is not an
   acceptable state.
6. **Never show a guess as a fact.**  An unknown last active month is
   rendered as "Unknown", not as a number and not as blank.
7. **The dashboard is read only.**  It cannot change a submission, a
   status, a population or a financial record.

---

## 4. Backend conventions

- The API is **read only**.  A new endpoint that writes needs the
  owner's approval.
- Business logic lives in `backend/app/services/`, one module per
  concern.  `main.py` only wires routes.
- **Views are the reporting contract.**  The dashboard and Power BI read
  the same nine views.  Never compute a reporting number in JavaScript
  that a view already provides.
- Every new endpoint gets a test.  Every new dashboard tab gets a test
  that asserts it agrees with the underlying view.
- SQL migrations are numbered and ordered, and a view whose column list
  changes is **dropped and recreated** — PostgreSQL cannot change a
  view's column order in place.
- `psycopg` rows come back as dicts.  Convert `Decimal` and `date` at the
  service boundary so the API can serialise them.

---

## 5. Testing and verification

```bash
.venv/bin/python -m pytest -q          # the whole suite
.venv/bin/python -m pytest tests/test_dashboard.py -q
```

- **Only `.venv`.**  The system Python lacks the Google libraries; the
  venv lacks nothing.
- A DAG test is **not** side-effect free.  It writes to the database,
  prepares email and publishes files.  Check `DRY_RUN` and the target
  month before every run.
- After any frontend change, verify in a **real browser** and capture a
  screenshot.  A page that renders "loading…" forever is not a page.

---

## 6. What the frontend may not do

- No external network calls.  No CDN, no webfont, no analytics, no
  telemetry.
- No `innerHTML` with unescaped data.  Every value goes through `esc()`.
- No client-side re-computation of a financial figure.
- No dependency added for the frontend.  If a chart is needed, it is
  inline SVG.

---

## 7. When to ask the owner

Ask **only** when the answer changes a financial rule, a security
boundary, data integrity, or the architecture.  A permission prompt is
not a question.  A layout, a colour, a label, a density choice — decide
it, and say what you decided.

Owner decisions live in `docs/DECISION_QUEUE.md`.
Technical handover lives in `docs/AGENT_STATUS.md`.
