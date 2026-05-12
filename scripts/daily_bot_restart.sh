#!/bin/bash
# v37h Tab-Audit-Day-2 (12.05.2026) — Daily Bot-Restart fuer Stale-Connection-Schutz.
#
# Hintergrund: Daily ib-gateway Container-Restart um 03:00 UTC (siehe crontab
# /var/log/ibgw-restart.log) hinterlaesst Bot's ib_insync-Connection-Pool in
# einem Zombie-Zustand: isConnected() = True, aber accountValues() liefert
# silent gecachte Werte. Resultat 12.05. 06:43 UTC: CASH_DRIFT-Alert weil
# Bot 2+h identische Werte schrieb.
#
# Defense-in-Depth Layer C: 15 Min nach ib-gateway-Restart (also 03:15 UTC)
# wird auch der Bot-Container restartet. Belt-and-Suspenders neben Layer B
# (active reqCurrentTime() healthcheck in ibkr_client._get_ib).
#
# Sicherheits-Gates:
#   - Markt ist um 03:15 UTC zu (US-Pre-Market start = 04:00 UTC = nach uns)
#   - flock verhindert parallele Ausfuehrung
#   - Restart pruft healthy-State nach 60s

set -euo pipefail

COMPOSE_FILE="/opt/investpilot/docker-compose.vps.yml"
LOG="/var/log/investpilot-daily-restart.log"
LOCK="/var/lock/investpilot-daily-restart.lock"
TS=$(date -u +"%Y-%m-%d %H:%M:%S UTC")

# Idempotenz-Lock — falls zwei Cron-Trigger ueberlappen, abort
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[$TS] Skip — andere Instanz laeuft bereits (flock)" >> "$LOG"
    exit 0
fi

echo "[$TS] Daily Bot-Restart triggered (Stale-Connection-Schutz)" >> "$LOG"

# Markt-Zeit-Gate: NICHT zwischen 13:30 UTC und 21:00 UTC laufen
# (US-Regulaere-Handelszeiten 9:30-16:00 ET = 13:30-21:00 UTC im Sommer).
# Im Normalfall wird der Cron eh nur um 03:15 UTC getriggert — dieser Gate
# ist nur fuer manuelles Aufrufen waehrend Markt zu absichern.
HOUR_UTC=$(date -u +%H)
MIN_UTC=$(date -u +%M)
if [ "$HOUR_UTC" -ge 13 ] && [ "$HOUR_UTC" -lt 21 ]; then
    if [ "$HOUR_UTC" -gt 13 ] || [ "$MIN_UTC" -ge 30 ]; then
        echo "[$TS] ABORT — US-Markt geoeffnet (UTC ${HOUR_UTC}:${MIN_UTC}), kein Restart" >> "$LOG"
        exit 1
    fi
fi

# Restart
if /usr/bin/docker compose -f "$COMPOSE_FILE" restart investpilot >> "$LOG" 2>&1; then
    echo "[$TS] docker compose restart OK" >> "$LOG"
else
    echo "[$TS] docker compose restart FAILED" >> "$LOG"
    # Pushover-Alert (falls Bot's alert-Modul erreichbar ist — best-effort)
    /usr/bin/docker exec investpilot python -c \
        "from app.alerts import push_alert; push_alert('Bot-Restart FAIL', 'Daily Restart 03:15 UTC schlug fehl. Manuelle Pruefung notwendig.')" \
        2>/dev/null || true
    exit 1
fi

# Health-Check: nach 60s sollte Container "healthy" sein
sleep 60
STATUS=$(/usr/bin/docker inspect --format='{{.State.Health.Status}}' investpilot 2>/dev/null || echo "unknown")
echo "[$TS] Post-Restart Health: $STATUS" >> "$LOG"

if [ "$STATUS" != "healthy" ] && [ "$STATUS" != "starting" ]; then
    echo "[$TS] WARNING: Health-Status nach 60s = $STATUS — investigate" >> "$LOG"
    /usr/bin/docker exec investpilot python -c \
        "from app.alerts import push_alert; push_alert('Bot Health Warn', 'Nach Daily-Restart Health=$STATUS statt healthy. Logs pruefen.')" \
        2>/dev/null || true
fi

echo "[$TS] Daily Restart fertig" >> "$LOG"
