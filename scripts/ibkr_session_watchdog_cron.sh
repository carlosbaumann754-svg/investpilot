#!/usr/bin/env bash
# v37cy IBKR-Session-Watchdog Cron-Wrapper.
#
# Lauft alle 3 Min via root-crontab. Ruft die Python-Logik im Container,
# bei Exit-Code 42 macht der Host docker restart ib-gateway + Pushover.
#
# State liegt in /opt/investpilot/data/ibkr_session_watchdog.json
# Logs in /var/log/ibkr-session-watchdog.log
set -uo pipefail
LOG=/var/log/ibkr-session-watchdog.log
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Run watchdog inside container
RESULT=$(docker exec investpilot python -m app.ibkr_session_watchdog 2>&1)
EXIT=$?

# Log only non-ok decisions verbose, ok decisions kurz
if [ "$EXIT" = "0" ]; then
  echo "[$TS] OK" >> "$LOG"
elif [ "$EXIT" = "42" ]; then
  echo "[$TS] RECOVERY NEEDED — full result:" >> "$LOG"
  echo "$RESULT" >> "$LOG"

  # Restart ib-gateway
  echo "[$TS] docker restart ib-gateway..." >> "$LOG"
  RESTART_OUT=$(docker restart ib-gateway 2>&1)
  RESTART_RC=$?
  echo "[$TS] restart rc=$RESTART_RC out=$RESTART_OUT" >> "$LOG"

  # Wait for IBC re-login (gnzsnz/ib-gateway needs ~60-90s)
  sleep 90

  # Verify
  VERIFY=$(docker exec investpilot python -m app.ibkr_session_watchdog 2>&1)
  VERIFY_RC=$?
  echo "[$TS] verify rc=$VERIFY_RC out=$VERIFY" >> "$LOG"

  # Pushover via app.alerts
  if [ "$VERIFY_RC" = "0" ]; then
    PRIO=0; TITLE="IBKR-Session Auto-Recovery OK"
    MSG="Bot war disconnected (vermutlich externer Login), Gateway neu gestartet, Verbindung wieder ok."
  else
    PRIO=2; TITLE="IBKR-Session Auto-Recovery FAIL"
    MSG="Restart durchgefuehrt aber Verify failed (rc=$VERIFY_RC). Manuell pruefen!"
  fi
  docker exec investpilot python -c "
from app.alerts import send_alert
send_alert(title='$TITLE', message='$MSG', level='WARNING' if $PRIO == 0 else 'ERROR')
" 2>&1 | tee -a "$LOG" >/dev/null
else
  echo "[$TS] OTHER (rc=$EXIT) — result:" >> "$LOG"
  echo "$RESULT" >> "$LOG"
  # Bei rate_limited einmal Pushover (max 1x pro Stunde via marker)
  RATE_MARKER=/tmp/ibkr-watchdog-rate-warned
  if echo "$RESULT" | grep -q "rate_limited" && [ ! -f "$RATE_MARKER" -o $(($(date +%s) - $(stat -c %Y "$RATE_MARKER" 2>/dev/null || echo 0))) -gt 3600 ]; then
    docker exec investpilot python -c "
from app.alerts import send_alert
send_alert(title='IBKR-Session Watchdog Rate-Limit',
           message='6 Restart-Versuche in letzter Stunde — manuell pruefen ob Gateway oder Konto Problem hat.',
           level='ERROR')
" 2>&1 | tee -a "$LOG" >/dev/null
    touch "$RATE_MARKER"
  fi
fi

exit 0
