#!/bin/bash
set -e

echo "=== InvestPilot Container startet ==="
echo "Zeit: $(date)"
echo "Data Dir: ${INVESTPILOT_DATA_DIR:-/app/data}"

# Sicherstellen dass Data-Verzeichnisse existieren
mkdir -p /app/data/logs

# Trading Scheduler im Hintergrund starten
echo "Starte Trading Scheduler..."
python -m app.scheduler &
SCHEDULER_PID=$!
echo "Scheduler PID: $SCHEDULER_PID"

# Graceful Shutdown
trap "echo 'Stopping...'; kill $SCHEDULER_PID; exit 0" SIGTERM SIGINT

# Web Dashboard im Vordergrund starten
# Render setzt PORT automatisch, Fallback auf 8000
WEB_PORT="${PORT:-8000}"
echo "Starte Web Dashboard auf Port ${WEB_PORT}..."
exec uvicorn web.app:app --host 0.0.0.0 --port ${WEB_PORT} --log-level info
