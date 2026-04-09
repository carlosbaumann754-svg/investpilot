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
INTERVAL_SECONDS = 300  # 5 Minuten


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

    # Cloud-Restore: Brain-Daten aus letztem Backup wiederherstellen (GitHub Gist + GDrive)
    log.info("Cloud-Restore: Pruefe ob Backup vorhanden...")
    try:
        from app.persistence import restore_from_cloud_with_gdrive
        restored = restore_from_cloud_with_gdrive()
        if restored:
            log.info("Cloud-Restore: Learnings erfolgreich wiederhergestellt!")
        else:
            log.info("Cloud-Restore: Kein Restore noetig (lokal vorhanden oder kein Backup)")
    except Exception as e:
        log.warning(f"Cloud-Restore fehlgeschlagen: {e}")

    # Keep-Alive Thread starten (verhindert dass Render Free Tier einschlaeft)
    ka = threading.Thread(target=_keep_alive, daemon=True)
    ka.start()
    log.info("Keep-Alive Thread gestartet")

    # Watchdog Thread starten
    try:
        from app.alerts import start_watchdog
        start_watchdog()
        log.info("Watchdog Thread gestartet")
    except ImportError:
        log.info("Watchdog nicht verfuegbar (alerts Modul fehlt)")

    # Market Context initial laden
    try:
        from app.market_context import update_full_context
        update_full_context()
        log.info("Market Context initialisiert")
    except ImportError:
        log.info("Market Context nicht verfuegbar")
    except Exception as e:
        log.warning(f"Market Context Init Fehler: {e}")

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

            # --- Freitag 17:00: Asset Discovery ---
            from app.asset_discovery import is_friday_discovery_time
            if is_friday_discovery_time():
                log.info(f"[{datetime.now():%H:%M}] Freitag - Starte Asset Discovery...")
                try:
                    from app.asset_discovery import run_weekly_discovery
                    run_weekly_discovery()
                except Exception as e:
                    log.error(f"Asset Discovery Fehler: {e}", exc_info=True)

            # --- Freitag 18:00: Weekly Report ---
            from app.weekly_report import is_friday_evening
            if is_friday_evening():
                log.info(f"[{datetime.now():%H:%M}] Freitag - Sende Weekly Report...")
                try:
                    from app.weekly_report import send_weekly_report
                    send_weekly_report()
                except Exception as e:
                    log.error(f"Weekly Report Fehler: {e}", exc_info=True)

            # --- Sonntag 02:00: Weekly Optimization ---
            # TEMPORAER DEAKTIVIERT: Optimizer crasht den Render-Container per
            # OOM (512 MB Limit). Bis Subprocess-Isolation oder Plan-Upgrade
            # umgesetzt sind, MUSS der Optimizer manuell via
            # /api/optimizer/run gestartet werden. Sonst riskieren wir bei
            # jedem Sonntag-Lauf einen Brain-Reset.
            if os.environ.get("ENABLE_SUNDAY_AUTO_OPTIMIZER", "0") == "1":
                try:
                    from app.optimizer import is_sunday_optimization_time
                    if is_sunday_optimization_time():
                        log.info(f"[{datetime.now():%H:%M}] Sonntag - Starte Weekly Optimization...")
                        try:
                            from app.optimizer import run_weekly_optimization
                            result = run_weekly_optimization()
                            log.info(f"Optimization Ergebnis: {result.get('action', 'unknown')}")
                        except Exception as e:
                            log.error(f"Weekly Optimization Fehler: {e}", exc_info=True)
                except ImportError:
                    pass

            # --- Optimizer-Watchdog: pruefe ob GitHub-Action neue Werte pushed hat ---
            # Verhindert die Race-Condition wo Renders naechster backup_to_cloud()
            # die Optimizer-Tunings wieder ueberschreibt.
            try:
                from app.persistence import check_and_reload_optimizer_output
                check_and_reload_optimizer_output()
            except Exception as e:
                log.debug(f"Optimizer-Watchdog Fehler (non-fatal): {e}")

            # --- Backtest-Watchdog (GH Action v12) ---
            try:
                from app.persistence import check_and_reload_backtest_output
                check_and_reload_backtest_output()
            except Exception as e:
                log.debug(f"Backtest-Watchdog Fehler (non-fatal): {e}")

            # --- ML-Training-Watchdog (GH Action v12) ---
            try:
                from app.persistence import check_and_reload_ml_training_output
                check_and_reload_ml_training_output()
            except Exception as e:
                log.debug(f"ML-Training-Watchdog Fehler (non-fatal): {e}")

            # --- Discovery-Watchdog (GH Action v12) ---
            try:
                from app.persistence import check_and_reload_discovery_output
                check_and_reload_discovery_output()
            except Exception as e:
                log.debug(f"Discovery-Watchdog Fehler (non-fatal): {e}")

            # --- Trading Zyklus ---
            log.info(f"[{datetime.now():%H:%M}] Starte Trading-Zyklus...")
            from app.trader import run_trading_cycle
            run_trading_cycle()
            log.info(f"[{datetime.now():%H:%M}] Trading-Zyklus abgeschlossen")

            # --- v12: Meta-Labeler Retrain & Activation Check (taeglich ~03:15) ---
            now_hm = datetime.now().strftime("%H:%M")
            if now_hm.startswith("03:1"):  # 03:10..03:19
                try:
                    from app.config_manager import load_config
                    from app import meta_labeler
                    cfg = load_config()
                    if (cfg.get("meta_labeling", {}) or {}).get("enabled", False):
                        meta_labeler.train_meta_labeler()
                        changed = meta_labeler.check_and_maybe_activate(cfg)
                        if changed:
                            log.info("  Meta-Labeler Shadow -> Live aktiviert")
                except Exception as e:
                    log.warning(f"Meta-Labeler Daily-Task Fehler: {e}")

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
