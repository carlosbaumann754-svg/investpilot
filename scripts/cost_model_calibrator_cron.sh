#!/bin/bash
# v37cg (2026-05-01): Vereinfachter Wrapper fuer wochentliche Cost-Model-
# Calibrator-Cron (Sonntag 15:00 UTC, 1h nach Semgrep).
#
# Vorher inline-Subshell mit escaped quotes — gleiche Anfaelligkeit wie
# v37d ib-gateway-Restart-Cron. Jetzt sauberes Skript.

set -e

LOG=/var/log/cost-model-calibrator.log
TS=$(date -u '+%Y-%m-%d %H:%M:%S UTC')

echo "[$TS] Weekly cost-model calibrator triggered" >> "$LOG"

if /usr/bin/docker exec investpilot python -m app.cost_model_calibrator >> "$LOG" 2>&1; then
    TS=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
    echo "[$TS] Calibrator OK" >> "$LOG"
    exit 0
else
    RC=$?
    TS=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
    echo "[$TS] Calibrator FAILED rc=$RC" >> "$LOG"
    exit $RC
fi
