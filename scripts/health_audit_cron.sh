#!/bin/bash
# v37h+2 (17.05.2026) — Wöchentlicher Health-Audit Cron-Wrapper.
# Cron-Eintrag: 0 13 * * 6 /opt/investpilot/scripts/health_audit_cron.sh
#                          (Samstag 13:00 UTC = 15:00 CEST)
set -e

LOG=/var/log/health-audit.log
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "[$TS] Health-Audit cron triggered" >> "$LOG"

# Run audit im Container
docker exec investpilot python -m app.health_audit >> "$LOG" 2>&1
RC=$?

echo "[$TS] Health-Audit rc=$RC (0=clean, 1=failures, 2=new-findings)" >> "$LOG"
exit $RC
