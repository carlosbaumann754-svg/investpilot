#!/bin/bash
# v37ch (2026-05-01): One-shot Pushover-Reminder fuer ROKU-SHORT-Close.
# Cron: 25 13 1 5 *  (= 13:25 UTC = 15:25 CEST am 01.05.2026, 5 Min vor RTH-Open)
# Loescht sich selbst nach Ausfuehrung damit es nicht naechstes Jahr wieder feuert.

set -e

# Pushover-Credentials aus Bot-Config (Live-Disk)
USER_KEY=$(docker exec investpilot python -c "from app.config_manager import load_config; c=load_config(); print(c.get('alerts',{}).get('pushover',{}).get('user_key',''))" 2>/dev/null)
API_TOKEN=$(docker exec investpilot python -c "from app.config_manager import load_config; c=load_config(); print(c.get('alerts',{}).get('pushover',{}).get('api_token',''))" 2>/dev/null)

if [ -z "$USER_KEY" ] || [ -z "$API_TOKEN" ]; then
    echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') ROKU-Reminder: Pushover credentials fehlen" >> /var/log/roku-reminder.log
    exit 1
fi

curl -s --form-string "token=$API_TOKEN" \
        --form-string "user=$USER_KEY" \
        --form-string "title=ROKU Schliessen — RTH-Open in 5 Min" \
        --form-string "message=ROKU SHORT (-1383 Shares, ~-19k Verlust) jetzt schliessen. Dashboard -> Verkaufen-Button bei ROKU. RTH oeffnet 15:30 CEST." \
        --form-string "priority=1" \
        --form-string "html=1" \
        https://api.pushover.net/1/messages.json >> /var/log/roku-reminder.log 2>&1

echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') ROKU-Reminder gesendet" >> /var/log/roku-reminder.log

# Self-cleanup: Cron-Eintrag entfernen damit nicht naechstes Jahr wieder feuert
crontab -l 2>/dev/null | grep -v 'roku_close_reminder.sh' | crontab -
echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') Cron-Eintrag entfernt (self-cleanup)" >> /var/log/roku-reminder.log
