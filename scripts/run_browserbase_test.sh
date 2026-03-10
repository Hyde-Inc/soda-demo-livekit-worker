#!/usr/bin/env bash
# Run Browserbase form fill with test payload; redirect all output to logs/browserbase_test_<timestamp>.log.
# From project root:
#   ./scripts/run_browserbase_test.sh
# Then inspect the log for "Filled X by ..." and "Form fill: N of M field(s) filled".

set -e
cd "$(dirname "$0")/.."
mkdir -p logs
LOG="logs/browserbase_test_$(date +%Y%m%d_%H%M%S).log"
echo "Running form fill; output -> $LOG"
.venv/bin/python scripts/run_browserbase_fill.py < scripts/test_browserbase_payload.json 2>&1 | tee "$LOG"
