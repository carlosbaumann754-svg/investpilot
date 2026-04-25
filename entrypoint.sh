#!/bin/bash
set -e

echo "=== InvestPilot Container startet ==="
echo "Zeit: $(date)"
echo "Data Dir: ${INVESTPILOT_DATA_DIR:-/app/data}"

# Sicherstellen dass Data-Verzeichnisse existieren
mkdir -p /app/data/logs

# v12 Bootstrap-Migration (idempotent): injiziert fehlende Feature-Flag-
# Sections + disabled_symbols in data/config.json ohne Optimizer-tunbare
# Werte (sl_pct/tp_pct/min_score) anzufassen.
echo "Starte v12 Bootstrap-Migration..."
python -m app.bootstrap_v12 || echo "WARNUNG: bootstrap_v12 non-zero exit — fahre fort"

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
# --loop asyncio statt default uvloop: nest_asyncio braucht patchbaren Loop
# fuer ib_insync-Calls aus FastAPI-Handlern. uvloop unterstuetzt das nicht.
# Trade-off: minimal langsamere Event-Loop (~5%), aber funktionierende
# IBKR-Endpoints im Dashboard (Cash/Investiert/Positionen Cards).
exec uvicorn web.app:app --host 0.0.0.0 --port ${WEB_PORT} --log-level info --loop asyncio
