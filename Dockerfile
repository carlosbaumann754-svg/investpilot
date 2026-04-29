FROM python:3.11-slim

WORKDIR /app

# curl fuer Healthcheck + tzdata fuer IANA-Zeitzonen (US/Eastern etc.,
# wird von ib-insync beim Parsing der IBKR-Order-Felder gebraucht).
RUN apt-get update && apt-get install -y --no-install-recommends curl build-essential tzdata && rm -rf /var/lib/apt/lists/*

# Dependencies installieren
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Anwendung kopieren
COPY app/ app/
COPY web/ web/
COPY scripts/ scripts/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Data-Verzeichnis + Bericht-Ordner vorbereiten + Default-Config kopieren
RUN mkdir -p /app/data/logs /app/Bericht
COPY data/config.json /app/data/config.json

# Port fuer Dashboard
EXPOSE 8000

# Health Check
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]
