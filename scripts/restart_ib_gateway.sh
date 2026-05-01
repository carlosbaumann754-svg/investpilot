#!/bin/bash
# v37cg (2026-05-01): Vereinfachter IB-Gateway-Restart-Wrapper.
#
# Vorher inline-Subshell in crontab — feuerte 2 Tage nicht (30.04. + 01.05.)
# vermutlich wegen Cron-Subshell-Parsing-Quirk mit den escaped quotes im
# date-Kommando. Jetzt sauberes Skript ohne Quote-Hell.
#
# Loest die taegliche IB-Gateway Re-Login-Race-Condition (Unrecognized
# Username/Password 23:59 UTC) durch sauberen Container-Restart 7h vor
# US Pre-Market.
#
# Cron: 0 3 * * * /opt/investpilot/scripts/restart_ib_gateway.sh

set -e

LOG=/var/log/ibgw-restart.log
TS=$(date -u '+%Y-%m-%d %H:%M:%S UTC')

echo "[$TS] Daily ib-gateway restart triggered" >> "$LOG"

if /usr/bin/docker restart ib-gateway >> "$LOG" 2>&1; then
    TS=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
    echo "[$TS] Restart OK" >> "$LOG"
    exit 0
else
    RC=$?
    TS=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
    echo "[$TS] Restart FAILED rc=$RC" >> "$LOG"
    exit $RC
fi
