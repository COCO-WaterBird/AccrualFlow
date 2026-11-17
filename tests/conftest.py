"""Pytest configuration for the AccrualFlow test suite."""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# The project .env holds the real local credentials.  It is loaded before
# the fallback default is applied, so that a developer who changed the
# password in .env is not silently overridden by the default below.
os.environ.setdefault(
    "ACCRUALFLOW_DB_PASSWORD",
    "local_dev_password",
)

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=True)
except ImportError:  # pragma: no cover - dotenv is a hard dependency
    pass
