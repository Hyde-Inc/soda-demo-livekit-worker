#!/usr/bin/env python3
"""
Run the Browserbase Ariba form-fill script (same as the subprocess).
Loads .env from project root, then runs browser_automation.ariba_form_fill.
Usage:
  cd /path/to/soda-demo-livekit-worker
  python scripts/run_browserbase_fill.py              # no form answers (login only)
  echo '[{"externalSystemCorrelationId":"x","answer":"y"}]' | python scripts/run_browserbase_fill.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# Project root = parent of scripts/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env so BROWSERBASE_* and ARIBA_WEB_* are set
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv(PROJECT_ROOT / ".env.local")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)

if __name__ == "__main__":
    raw = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
    form_answers_json = None
    existing_session_id = None
    existing_connect_url = None
    if raw:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                faj = payload.get("form_answers_json")
                form_answers_json = json.dumps(faj) if isinstance(faj, list) else faj
                existing_session_id = payload.get("session_id")
                existing_connect_url = payload.get("connect_url")
            else:
                form_answers_json = raw
        except json.JSONDecodeError:
            form_answers_json = raw
    from browser_automation import ariba_form_fill
    result = ariba_form_fill.run_ariba_form_fill(
        form_answers_json=form_answers_json,
        existing_session_id=existing_session_id or None,
        existing_connect_url=existing_connect_url or None,
    )
    print(json.dumps(result), flush=True)
