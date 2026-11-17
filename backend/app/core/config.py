import os
from pathlib import Path
from dotenv import load_dotenv

# Locate AccrualFlow project root
BASE_DIR = Path(__file__).resolve().parents[3]

# Load .env file automatically
load_dotenv(BASE_DIR / ".env")

DB_HOST = os.getenv("ACCRUALFLOW_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("ACCRUALFLOW_DB_PORT", "5433"))
DB_USER = os.getenv("ACCRUALFLOW_DB_USER", "accrualflow")
DB_NAME = os.getenv("ACCRUALFLOW_DB_NAME", "mock_erp_db")
DB_PASSWORD = os.getenv("ACCRUALFLOW_DB_PASSWORD")

if not DB_PASSWORD:
    raise RuntimeError("Database password is not configured")

# ---------------------------------------------------------------------------
# Main AccrualFlow database name.
# The Mock ERP database keeps its own name so that both can coexist on the
# same PostgreSQL instance.
# ---------------------------------------------------------------------------
ACCRUALFLOW_DB_NAME = os.getenv(
    "ACCRUALFLOW_MAIN_DB_NAME",
    "accrualflow_db",
)

# ---------------------------------------------------------------------------
# Writable data root.
#
# All runtime artefacts (raw EML archives, attachments, corrections, master
# workbooks) live under one configurable root so that the same logical layout
# can be mounted as a dedicated read-write volume inside Airflow without
# making the source code tree writable.
#
# Host default : <project>/backend/data/runtime
# Container     : /opt/airflow/data   (mounted from ../backend/data/runtime)
# ---------------------------------------------------------------------------
DATA_DIR = Path(
    os.getenv("ACCRUALFLOW_DATA_DIR", str(BASE_DIR / "backend" / "data" / "runtime"))
).resolve()

# Raw inbound evidence: original .eml bytes + attachments.
EMAIL_ARCHIVE_DIR = DATA_DIR / "email_archive"
ATTACHMENT_DIR = DATA_DIR / "attachments"

# Outbound correction requests (workbooks + drafts).
CORRECTION_DIR = DATA_DIR / "corrections"

# Published consolidated workbook.
REPORT_DIR = DATA_DIR / "reports"

# Directory tree of the expected PO line population snapshot.
POPULATION_DIR = DATA_DIR / "population"

# Legacy output roots that pre-date the DATA_DIR layout.
LEGACY_OUTPUT_DIR = BASE_DIR / "backend" / "output"
LEGACY_ARCHIVE_DIR = BASE_DIR / "backend" / "archive"

# ---------------------------------------------------------------------------
# Behaviour switches
# ---------------------------------------------------------------------------

# Master switch for outbound email. While False, correction requests are
# only prepared (CORRECTION_PREPARED) and never sent.
DRY_RUN = os.getenv("ACCRUALFLOW_DRY_RUN", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Maximum number of correction requests that may be issued for one
# (reporting month, department) pair before manual intervention is required.
MAX_CORRECTION_ATTEMPTS = int(
    os.getenv("ACCRUALFLOW_MAX_CORRECTION_ATTEMPTS", "3")
)

# Reporting business timezone.
REPORTING_TIMEZONE = os.getenv(
    "ACCRUALFLOW_REPORTING_TIMEZONE",
    "America/Los_Angeles",
)

# Canonical percentage representation.
#
# CONTRACT: a completion percentage is always stored as a DECIMAL fraction
# in the closed interval [0, 1].  0.45 means 45%.  Values in [0, 100] are
# never mixed into the same canonical column.  Excel exports re-apply the
# percent display format so the exported workbook shows 45%.
PERCENT_CANONICAL_MAX = 1
PERCENT_CANONICAL_MIN = 0

# Number of decimal places kept for a canonical fraction.
PERCENT_SCALE = 6


def ensure_runtime_dirs():
    """Create every writable runtime directory. Safe to call repeatedly."""
    for directory in (
        DATA_DIR,
        EMAIL_ARCHIVE_DIR,
        ATTACHMENT_DIR,
        CORRECTION_DIR,
        REPORT_DIR,
        POPULATION_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
