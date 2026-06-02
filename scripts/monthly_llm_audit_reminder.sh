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
DOW=$(date -u +%u)   # 1=Mo … 7=So

# Gating: NUR am 1. Sonntag im Monat.
# BUGFIX 02.06.2026: Cron-Eintrag '0 14 1-7 * 0' loest wegen der Cron-OR-Falle
# (DOM UND DOW beide gesetzt = ODER-Verknuepfung) an JEDEM Tag 1.-7. PLUS jedem
# Sonntag aus. Der alte Guard fing nur DOM>7 ab -> Reminder feuerte taeglich
# 1.-7. (z.B. Mo 01.06. + Di 02.06.) statt nur am 1. Sonntag. Fix: zusaetzlich
# DOW==7 (Sonntag) verlangen. 1. Sonntag = DOM<=7 UND Sonntag.
# (date -u: Cron laeuft 14:00 UTC = 16:00 CEST, gleicher Kalendertag -> kein
#  Zeitzonen-Grenzfall.)
if [ "$DOM" -gt 7 ] || [ "$DOW" -ne 7 ]; then
    echo "[$TS] Monthly-LLM-Reminder skip: nicht 1. Sonntag (DOM=$DOM DOW=$DOW)" >> "$LOG"
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
