#!/usr/bin/env bash
# v37cx Anti-Regression Self-Test — stuendlicher Run.
set -e
LOG=/var/log/self-test.log
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Self-Test starting" >> "$LOG"
docker exec investpilot python -c "
from app.self_test import run_and_alert
import json, sys
suite = run_and_alert()
print(f\"  total={suite.total} passed={suite.passed} failed={suite.failed} status={suite.overall_status}\")
sys.exit(0 if suite.failed == 0 else 1)
" >> "$LOG" 2>&1
RC=$?
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Self-Test done (rc=$RC)" >> "$LOG"
exit $RC
