"""
InvestPilot - Trading Scheduler
Ersetzt Windows Task Scheduler. Laeuft als Daemon im Docker Container.
Fuehrt stuendlich einen Trading-Zyklus aus (nur waehrend Markt-Oeffnungszeiten).
"""

import time
import logging
import os
import threading
import urllib.request
from datetime import datetime

from app.config_manager import get_data_path

log = logging.getLogger("Scheduler")

TRADING_FLAG = get_data_path("trading_enabled.flag")
INTERVAL_SECONDS = 3600  # 1 Stunde


def is_trading_enabled():
    """Pruefe ob Trading vom Dashboard aktiviert ist."""
    # Wenn Flag-Datei nicht existiert, ist Trading standardmaessig AN
    if not TRADING_FLAG.exists():
        return True
    try:
        content = TRADING_FLAG.read_text().strip().lower()
        return content == "true" or content == "1"
    except Exception:
        return True


def is_market_hours():
    """Pruefe ob US-Markt offen ist (Mo-Fr, 15:30-22:00 CET).
    Im Demo-Modus immer True, da eToro Demo 24/7 tradet."""
    env = os.environ.get("ETORO_ENVIRONMENT", "demo")
    if env == "demo":
        return True

    now = datetime.now()
    # Wochenende
    if now.weekday() >= 5:
        return False
    # US Market Hours (CET): 15:30 - 22:00
    hour = now.hour
    minute = now.minute
    if hour < 15 or (hour == 15 and minute < 30) or hour >= 22:
        return False
    return True


def _keep_alive():
    """Pingt den eigenen Health-Endpoint alle 10 Min um Render Free Tier wach zu halten."""
    port = os.environ.get("PORT", "8000")
    url = f"http://localhost:{port}/health"
    while True:
        try:
            urllib.request.urlopen(url, timeout=5)
        except Exception:
            pass
        time.sleep(600)  # alle 10 Minuten


def scheduler_loop():
    """Endlos-Loop: prueft stuendlich ob getradet werden soll."""
    log.info("=" * 55)
    log.info("InvestPilot Scheduler gestartet")
    log.info(f"Interval: {INTERVAL_SECONDS}s")
    log.info(f"Trading Flag: {TRADING_FLAG}")
    log.info("=" * 55)

    # Keep-Alive Thread starten (verhindert dass Render Free Tier einschlaeft)
    ka = threading.Thread(target=_keep_alive, daemon=True)
    ka.start()
    log.info("Keep-Alive Thread gestartet")

    while True:
        try:
            if not is_trading_enabled():
                log.info(f"[{datetime.now():%H:%M}] Trading deaktiviert (Flag=false)")
                time.sleep(INTERVAL_SECONDS)
                continue

            if not is_market_hours():
                log.info(f"[{datetime.now():%H:%M}] Ausserhalb Markt-Oeffnungszeiten")
                time.sleep(INTERVAL_SECONDS)
                continue

            log.info(f"[{datetime.now():%H:%M}] Starte Trading-Zyklus...")
            from app.trader import run_trading_cycle
            run_trading_cycle()
            log.info(f"[{datetime.now():%H:%M}] Trading-Zyklus abgeschlossen")

        except Exception as e:
            log.error(f"Fehler im Trading-Zyklus: {e}", exc_info=True)

        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(str(get_data_path("logs/scheduler.log")), encoding="utf-8"),
            logging.StreamHandler(),
        ]
    )
    scheduler_loop()
