#!/bin/bash
# v37h+2 (17.05.2026) — Monatlicher LLM-Deep-Audit-Reminder.
# Cron-Eintrag: 0 14 1-7 * 0 /opt/investpilot/scripts/monthly_llm_audit_reminder.sh
#                            (1. Sonntag 14:00 UTC = 16:00 CEST)
#
# Carlos hat Option C (Hybrid) gewaehlt: wöchentlicher Code-Audit autonom +
# monatlicher LLM-Deep-Audit via Spawn-Task-Chip. Dieser Cron sendet einen
# Pushover-Reminder am 1. Sonntag jedes Monats damit Carlos den Spawn-Task-
# Chip im Dashboard klickt.
set -e

LOG=/var/log/health-audit.log
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
DOM=$(date -u +%-d)

# Gating: nur an 1. Sonntag im Monat (DOM 1-7 + dow=0 ist via cron geprüft,
# DOM zusaetzlich hier weil GH-Cron-OR-Falle moeglich)
if [ "$DOM" -gt 7 ]; then
    echo "[$TS] Monthly-LLM-Reminder skip: DOM=$DOM > 7 (nicht 1. Sonntag)" >> "$LOG"
    exit 0
fi

echo "[$TS] Monthly-LLM-Audit-Reminder triggered (DOM=$DOM)" >> "$LOG"

# Pushover via Bot's alerts-Modul
docker exec investpilot python3 -c "
from app.alerts import send_alert
send_alert(
    'Monatlicher LLM-Deep-Audit faellig (1. Sonntag im Monat). '
    'Bitte heute den Phantom-Audit-Spawn-Task-Chip im Dashboard klicken — '
    'wöchentlicher Code-Audit deckt nur bekannte Patterns ab, der LLM-Audit '
    'findet neue Bug-Klassen (Heute morgen-Style Deep-Review). '
    'Aufwand: ~10 Min deine Aufmerksamkeit + 5-10 Min Agent-Run.',
    level='INFO',
)
" >> "$LOG" 2>&1

RC=$?
echo "[$TS] Monthly-LLM-Reminder rc=$RC" >> "$LOG"
exit $RC
