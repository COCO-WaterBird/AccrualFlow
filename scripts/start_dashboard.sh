#!/bin/bash
# Start the AccrualFlow web dashboard.
#
# The dashboard reads the same reporting views that Power BI reads, so it
# always agrees with the official report.
#
#   http://127.0.0.1:8000/dashboard/dashboard.html
#
# The PostgreSQL container has to be running.  Start it with:
#   docker compose up -d postgres
set -uo pipefail

cd /Users/zhangke/Documents/GitHub/AccrualFlow

HOST="${1:-127.0.0.1}"
PORT="${2:-8000}"

echo "=== Dashboard: http://${HOST}:${PORT}/dashboard/dashboard.html"
exec .venv/bin/python -m uvicorn backend.app.main:app \
  --host "${HOST}" \
  --port "${PORT}" \
  --reload
