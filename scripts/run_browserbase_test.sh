#!/usr/bin/env bash
# Run Browserbase form fill with a test payload (login + click Supplier General Information link + fill sample fields).
# From project root:
#   ./scripts/run_browserbase_test.sh
# Or with venv:
#   .venv/bin/python scripts/run_browserbase_fill.py < scripts/test_browserbase_payload.json

set -e
cd "$(dirname "$0")/.."
.venv/bin/python scripts/run_browserbase_fill.py < scripts/test_browserbase_payload.json
