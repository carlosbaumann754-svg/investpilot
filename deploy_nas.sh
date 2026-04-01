#!/bin/bash
# ============================================================
# InvestPilot - Synology NAS Deployment Script
# Kopiert alle Dateien aufs NAS und startet den Docker Container
# ============================================================
#
# VORAUSSETZUNGEN:
#   1. SSH auf dem NAS aktiviert (DSM > Systemsteuerung > Terminal & SNMP)
#   2. Docker / Container Manager auf dem NAS installiert
#   3. docker-compose auf dem NAS verfuegbar (kommt mit Container Manager)
#
# VERWENDUNG:
#   ./deploy_nas.sh <NAS_IP> <NAS_USER>
#   Beispiel: ./deploy_nas.sh 192.168.1.100 carlos
#
# ============================================================

set -e

# Parameter
NAS_HOST="${1:?Bitte NAS IP angeben, z.B.: ./deploy_nas.sh 192.168.1.100 carlos}"
NAS_USER="${2:-carlos}"
NAS_PATH="/volume1/docker/investpilot"

echo "============================================"
echo "  InvestPilot -> Synology NAS Deployment"
echo "============================================"
echo "NAS: ${NAS_USER}@${NAS_HOST}"
echo "Pfad: ${NAS_PATH}"
echo ""

# Pruefen ob alle Dateien vorhanden
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

for f in Dockerfile docker-compose.yml entrypoint.sh requirements.txt .env; do
    if [ ! -f "$f" ]; then
        echo "FEHLER: $f nicht gefunden!"
        exit 1
    fi
done

echo "[1/5] Erstelle Verzeichnisse auf dem NAS..."
ssh "${NAS_USER}@${NAS_HOST}" "
    sudo mkdir -p ${NAS_PATH}/{app,web/static,data/logs}
    sudo chown -R ${NAS_USER}:users ${NAS_PATH}
"

echo "[2/5] Kopiere Projekt-Dateien..."
# Hauptdateien
scp Dockerfile docker-compose.yml entrypoint.sh requirements.txt .env \
    "${NAS_USER}@${NAS_HOST}:${NAS_PATH}/"

# App-Modul
scp -r app/ "${NAS_USER}@${NAS_HOST}:${NAS_PATH}/app/"

# Web-Modul
scp -r web/ "${NAS_USER}@${NAS_HOST}:${NAS_PATH}/web/"

# Data-Dateien (config, brain_state, trade_history)
for f in data/config.json data/brain_state.json data/trade_history.json; do
    if [ -f "$f" ]; then
        scp "$f" "${NAS_USER}@${NAS_HOST}:${NAS_PATH}/$f"
    fi
done

echo "[3/5] Setze Berechtigungen..."
ssh "${NAS_USER}@${NAS_HOST}" "
    chmod +x ${NAS_PATH}/entrypoint.sh
    chmod 600 ${NAS_PATH}/.env
"

echo "[4/5] Baue und starte Docker Container..."
ssh "${NAS_USER}@${NAS_HOST}" "
    cd ${NAS_PATH}
    sudo docker-compose down 2>/dev/null || true
    sudo docker-compose build --no-cache
    sudo docker-compose up -d
"

echo "[5/5] Warte auf Health Check..."
sleep 10
HEALTH=$(ssh "${NAS_USER}@${NAS_HOST}" "curl -s http://localhost:8443/health 2>/dev/null || echo 'FAIL'")

echo ""
echo "============================================"
if echo "$HEALTH" | grep -q '"ok"'; then
    echo "  DEPLOYMENT ERFOLGREICH!"
    echo ""
    echo "  Dashboard: https://${NAS_HOST}:8443"
    echo "  Login: Username und Passwort aus .env"
    echo ""
    echo "  Container Status:"
    ssh "${NAS_USER}@${NAS_HOST}" "sudo docker ps --filter name=investpilot --format 'table {{.Status}}\t{{.Ports}}'"
else
    echo "  WARNUNG: Health Check fehlgeschlagen"
    echo "  Pruefe Container Logs:"
    echo "  ssh ${NAS_USER}@${NAS_HOST} 'sudo docker logs investpilot'"
fi
echo "============================================"
