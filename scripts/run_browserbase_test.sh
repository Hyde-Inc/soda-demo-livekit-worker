#!/usr/bin/env bash
# Run Browserbase form fill with test payload; output to console only.
# From project root:
#   ./scripts/run_browserbase_test.sh
#
# To capture to a file again:
#   mkdir -p logs && .venv/bin/python scripts/run_browserbase_fill.py < scripts/test_browserbase_payload.json 2>&1 | tee "logs/browserbase_test_$(date +%Y%m%d_%H%M%S).log"

set -e
cd "$(dirname "$0")/.."
.venv/bin/python scripts/run_browserbase_fill.py < scripts/test_browserbase_payload.json
