FROM python:3.11-slim

WORKDIR /app

# curl fuer Healthcheck installieren
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Dependencies installieren
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Anwendung kopieren
COPY app/ app/
COPY web/ web/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Data-Verzeichnis vorbereiten
RUN mkdir -p /app/data/logs

# Port fuer Dashboard
EXPOSE 8000

# Health Check
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]
