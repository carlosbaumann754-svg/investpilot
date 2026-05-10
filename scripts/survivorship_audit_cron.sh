#!/bin/bash
# v37cg (2026-05-01): Vereinfachter Wrapper fuer wochentliche Survivorship-
# Audit-Cron (Sonntag 13:00 UTC, 1h nach WFO).
#
# Vorher inline-Subshell mit escaped quotes — gleiche Anfaelligkeit wie
# v37d ib-gateway-Restart-Cron der 30.04.+01.05. nicht feuerte (Subshell-
# Parsing-Quirk). Jetzt sauberes Skript.

set -e

LOG=/var/log/survivorship-audit.log
TS=$(date -u '+%Y-%m-%d %H:%M:%S UTC')

echo "[$TS] Weekly survivorship audit triggered" >> "$LOG"

if /usr/bin/docker exec investpilot python -m app.survivorship_audit --cron >> "$LOG" 2>&1; then
    TS=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
    echo "[$TS] Audit OK" >> "$LOG"
    exit 0
else
    RC=$?
    TS=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
    echo "[$TS] Audit FAILED rc=$RC" >> "$LOG"
    exit $RC
fi
