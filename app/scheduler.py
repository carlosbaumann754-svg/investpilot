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
from datetime import datetime, timezone

from app.config_manager import get_data_path

log = logging.getLogger("Scheduler")

TRADING_FLAG = get_data_path("trading_enabled.flag")
INTERVAL_SECONDS = 300  # 5 Minuten


def _dispatch_discovery_workflow(triggered_by: str = "scheduler-cron") -> bool:
    """Triggert den Asset-Discovery-Workflow auf GitHub Actions.

    Mirror zu web/app.py::_trigger_github_action_discovery — hier inline,
    damit der Scheduler nicht auf FastAPI-Code zugreifen muss. Wird vom
    Friday-17:00-Slot genutzt statt des frueheren in-process
    run_weekly_discovery() (OOM-Risiko auf Render Free Tier 512 MB).
    """
    from app.config_manager import save_json
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log.error("Discovery-Dispatch: GITHUB_TOKEN fehlt — kann Workflow nicht triggern")
        return False

    repo = os.environ.get("GITHUB_REPO", "carlosbaumann754-svg/investpilot")
    workflow_file = os.environ.get("DISCOVERY_WORKFLOW_FILE", "asset_discovery.yml")
    ref = os.environ.get("DISCOVERY_WORKFLOW_REF", "master")
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches"

    # Status initialisieren, damit der Watchdog den Lauf sehen kann
    status = {
        "state": "running",
        "phase": "dispatching",
        "message": "Scheduler-Cron: GitHub Action wird gestartet...",
        "started_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "finished_at": None,
        "triggered_by": triggered_by,
        "error": None,
        "mode": "github-action-dispatching",
    }
    try:
        save_json("discovery_status.json", status)
    except Exception as e:
        log.warning(f"Discovery-Status initial save fehlgeschlagen: {e}", exc_info=True)

    try:
        import requests
        resp = requests.post(
            url,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={"ref": ref, "inputs": {"triggered_by": triggered_by}},
            timeout=15,
        )
        if resp.status_code in (201, 204):
            log.info(f"Discovery-Workflow getriggert (repo={repo}, ref={ref})")
            status["mode"] = "github-action-running"
            status["message"] = "GitHub Action gestartet, warte auf Runner..."
            status["updated_at"] = datetime.now().isoformat()
            try:
                save_json("discovery_status.json", status)
            except Exception as e:
                log.warning(f"Discovery-Status post-dispatch save fehlgeschlagen: {e}", exc_info=True)
            return True
        log.error(f"Discovery-Dispatch HTTP {resp.status_code}: {resp.text[:200]}")
        status["state"] = "error"
        status["error"] = f"workflow_dispatch HTTP {resp.status_code}: {resp.text[:160]}"
    except Exception as e:
        log.exception("Discovery Workflow-Dispatch fehlgeschlagen")
        status["state"] = "error"
        status["error"] = f"dispatch: {type(e).__name__}: {e}"

    status["finished_at"] = datetime.now().isoformat()
    status["updated_at"] = datetime.now().isoformat()
    try:
        save_json("discovery_status.json", status)
    except Exception as e:
        log.warning(f"Discovery-Status error-save fehlgeschlagen: {e}", exc_info=True)
    return False


def is_trading_enabled():
    """Pruefe ob Trading vom Dashboard aktiviert ist.

    v37cw: Fail-CLOSED. Wenn Flag-Datei FEHLT, ist Trading DEAKTIVIERT.
    Frueher: Default=True bei fehlender Datei. Das fuehrte am 05.05.2026
    nach einem Container-Rebuild dazu, dass der Bot ueber Nacht autonom
    AAPL+TSLA-Orders submittete obwohl der User pausiert hatte.
    """
    if not TRADING_FLAG.exists():
        log.warning("Trading-Flag-Datei fehlt — fail-closed: Trading PAUSIERT "
                    "bis Flag-Datei mit 'true' angelegt wird.")
        return False
    try:
        content = TRADING_FLAG.read_text().strip().lower()
        return content == "true" or content == "1"
    except Exception as e:
        log.error(f"Trading-Flag nicht lesbar ({e}) — safe default: Trading PAUSIERT.",
                  exc_info=True)
        return False


def _ensure_trading_flag_initialized():
    """Boot-Init: legt trading_enabled.flag mit 'false' an, falls nicht existiert.

    v37cw. Verhindert Race wo der Watchdog/Web-UI eine fehlende Flag als
    'true' interpretiert (alte data_access.py-Logik), waehrend der Scheduler
    sie korrekt als 'false' liest. Mit Init sind beide konsistent.
    """
    try:
        if not TRADING_FLAG.exists():
            TRADING_FLAG.parent.mkdir(parents=True, exist_ok=True)
            TRADING_FLAG.write_text("false")
            log.warning(f"Trading-Flag initialisiert -> false (Default fail-closed): {TRADING_FLAG}")
    except Exception as e:
        log.error(f"Trading-Flag-Init fehlgeschlagen: {e}", exc_info=True)


def cancel_all_pending_orders(reason: str = "trading_disabled") -> int:
    """Cancel ALLE pending IBKR-Orders. Wird beim Trading-Off-Transition gerufen.

    v37cw. Verhindert Episode 04.05.2026 wo gestern abend gesetzte
    Limit-Orders ueber Nacht durch Pre-Market-Open trotzdem fillten obwohl
    Trading-Flag inzwischen aus war.

    Returns: Anzahl gecancelter Orders.
    """
    cancelled = 0
    try:
        from app.ibkr_client import IbkrBroker  # lazy import
        broker = IbkrBroker()
        ib = broker._get_ib()
        open_trades = list(ib.openTrades())
        if not open_trades:
            log.info("cancel_all_pending: keine offenen IBKR-Trades")
            return 0
        log.warning(f"cancel_all_pending ({reason}): {len(open_trades)} offene Trades — cancelling…")
        for t in open_trades:
            try:
                ib.cancelOrder(t.order)
                cancelled += 1
                log.warning(f"  cancelled: {t.contract.symbol} {t.order.action} "
                            f"qty={t.order.totalQuantity} status={t.orderStatus.status}")
            except Exception as e:
                log.error(f"  cancel FAILED for {t.contract.symbol}: {e}")
        ib.sleep(1.0)
    except Exception as e:
        log.error(f"cancel_all_pending unexpected error: {e}", exc_info=True)
    return cancelled


def is_us_stock_hours():
    """Kompat-Wrapper. Nutzt jetzt asset_classes.Registry (DST-aware via zoneinfo)."""
    from app.asset_classes import is_asset_class_tradeable as _is_tradeable
    return _is_tradeable("stocks")


def is_forex_hours():
    """Kompat-Wrapper. Nutzt jetzt asset_classes.Registry."""
    from app.asset_classes import is_asset_class_tradeable as _is_tradeable
    return _is_tradeable("forex")


def is_asset_class_tradeable(asset_class: str) -> bool:
    """Pruefe ob eine spezifische Asset-Klasse JETZT tradeable ist.

    Delegiert vollstaendig an app.asset_classes (Single Source of Truth).
    Alle bekannten Klassen siehe asset_classes.REGISTRY:
      crypto, stocks, etf, stocks_extended, eu_stocks, uk_stocks, ch_stocks,
      jp_stocks, hk_stocks, au_stocks, forex, futures, indices, commodities, bonds.

    Unbekannte Klassen -> True (permissiv, damit neue Asset-Typen den Bot
    nicht versehentlich stilllegen).
    """
    from app.asset_classes import is_asset_class_tradeable as _is_tradeable
    return _is_tradeable(asset_class)


def is_market_hours():
    """Pruefe ob IRGENDEINE Asset-Klasse im Universum JETZT tradeable ist.

    v30 (asset_classes-Registry): Liest ASSET_UNIVERSE, sammelt alle vorkommenden
    'class'-Werte und fragt die Registry. So bekommt der Klon-Bot, der z.B.
    EU-Stocks oder Futures handelt, automatisch korrekte Trading-Hours ohne
    Code-Change im Scheduler.

    Modi:
    - PAPER_FORCE_24x7=1 ENV: Bypass aktiv (nur fuer Tests, NICHT Default)
    - sonst: True wenn min. 1 Klasse im Universum tradeable ist

    HISTORICAL FIX (2026-04-27): Vorher gab es einen broker==etoro+demo
    Bypass, der das Marktzeiten-Gate IMMER auf True setzte. Folge: 67
    SCANNER_BUY-Versuche fuer ROKU am 27.04. zwischen 01:45-06:22 UTC,
    weit ausserhalb US-Marktzeiten. Bypass entfernt — nun gelten echte
    Marktzeiten fuer ALLE Broker (eToro Demo/Real, IBKR Paper/Live).
    Wer 24/7 will (z.B. fuer Crypto-only-Bots), aktiviert das via
    PAPER_FORCE_24x7=1 oder hat Crypto im Universum (Registry).
    """
    if os.environ.get("PAPER_FORCE_24x7", "0") == "1":
        return True

    try:
        from app.market_scanner import ASSET_UNIVERSE
        from app.asset_classes import any_class_tradeable
        classes = {(m.get("class") or "stocks") for m in ASSET_UNIVERSE.values()}
        if not classes:
            return is_us_stock_hours()  # leeres Universum -> safe default
        return any_class_tradeable(classes)
    except Exception as e:
        log.warning(f"is_market_hours fallback wegen Fehler: {e}")
        return is_us_stock_hours()


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

    # v37r: WFO-Drift-Check beim Bot-Start
    # Pruefe ob Live-Config mit WFO-Empfehlungen aus wfo_status.json matcht.
    # Bei Drift: Pushover-WARNING + Auto-Restore via save_config-Hook.
    try:
        from app.wfo_lock import boot_drift_check
        result = boot_drift_check(send_alert=True, auto_restore=True)
        if result.get("drifts_detected", 0) > 0:
            log.warning(
                f"WFO-Drift-Check: {result['drifts_detected']} Drift(s) erkannt, "
                f"{len(result['restored'])} korrigiert, alert_sent={result['alert_sent']}"
            )
        else:
            log.info("WFO-Drift-Check: Config matcht WFO-Locks (alles gruen)")
    except Exception as e:
        log.warning(f"WFO-Drift-Check fehlgeschlagen: {e}")

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
            # v37co (03.05.2026): Heartbeat IMMER schreiben — auch bei Skip-Cycles
            # (Markt zu / Trading deaktiviert). Bot ist clearly alive wenn er den
            # Cycle-Check macht. Vorher hat Watchdog am Wochenende falsch alarmiert
            # ('Bot inaktiv seit 408 Min!') weil Heartbeat-Update nur in trader.py
            # nach vollem Cycle passierte. Carlos bekam Sa+So Pushovers obwohl
            # Bot gesund war.
            try:
                from app.alerts import update_heartbeat
                update_heartbeat()
            except Exception as e:
                log.debug(f"Heartbeat-Update fehlgeschlagen (non-fatal): {e}")

            if not is_trading_enabled():
                # v37cw: bei Trading-Off zusaetzlich offene IBKR-Orders cancellen.
                # Verhindert dass gestern gesetzte Limit-Orders heute fillen.
                # Guard: nur cancellen wenn vorher tatsaechlich an war (Transition),
                # nicht in jedem 5min-Tick wenn Flag dauerhaft aus.
                if globals().get('_TRADING_WAS_ENABLED', False):
                    try:
                        n = cancel_all_pending_orders(reason='trading_flag_off_transition')
                        log.warning(f"Trading-Off-Transition: {n} pending Orders gecancelt.")
                    except Exception as e:
                        log.error(f"Cancel-on-Off-Transition fehlgeschlagen: {e}", exc_info=True)
                    globals()['_TRADING_WAS_ENABLED'] = False
                log.info(f"[{datetime.now():%H:%M}] Trading deaktiviert (Flag=false)")
                time.sleep(INTERVAL_SECONDS)
                continue
            else:
                globals()['_TRADING_WAS_ENABLED'] = True

            if not is_market_hours():
                log.info(f"[{datetime.now():%H:%M}] Ausserhalb Markt-Oeffnungszeiten")
                time.sleep(INTERVAL_SECONDS)
                continue

            # --- Freitag 17:00: Asset Discovery (offloaded an GitHub Actions) ---
            # Fruehere Version rief run_weekly_discovery() in-process auf -> OOM
            # Risiko auf Render Free Tier (512 MB). Jetzt: Dispatch an GH Action,
            # Ergebnisse kommen via Gist + Watchdog zurueck. Guard via
            # discovery_last_dispatched.flag verhindert Mehrfach-Dispatch
            # innerhalb des 1-Stunden-Slots (Scheduler tickt alle 5 Min).
            from app.asset_discovery import is_friday_discovery_time
            if is_friday_discovery_time():
                guard = get_data_path("discovery_last_dispatched.flag")
                # UTC, damit der Day-Key zum UTC-Slot der Stundenpruefung passt
                # (sonst kann nahe Mitternacht der Local-Day-Key abweichen).
                today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                already = False
                try:
                    if guard.exists() and guard.read_text().strip() == today_key:
                        already = True
                except Exception:
                    pass
                if not already:
                    log.info(
                        f"[{datetime.now():%H:%M}] Freitag - Dispatche Asset Discovery "
                        f"an GitHub Actions..."
                    )
                    # Flag SOFORT schreiben (vor Dispatch), damit der naechste
                    # 5-Min-Tick nicht nochmal dispatcht falls der Dispatch-Call
                    # selbst laenger dauert als 5 Minuten (Race-Condition).
                    # Falls Dispatch schlaegt fehl -> wird Flag wieder geloescht.
                    try:
                        guard.write_text(today_key)
                    except Exception as e:
                        log.warning(f"Discovery-Guard-Flag pre-write fehlgeschlagen: {e}")
                    try:
                        ok = _dispatch_discovery_workflow(triggered_by="scheduler-friday-17")
                        if not ok:
                            # Dispatch hat fehlgeschlagen -> Flag zuruecksetzen,
                            # damit naechster Slot (oder manueller Retry) noch geht.
                            try:
                                guard.unlink(missing_ok=True)
                            except Exception:
                                pass
                    except Exception as e:
                        log.error(f"Asset Discovery Dispatch Fehler: {e}", exc_info=True)
                        try:
                            guard.unlink(missing_ok=True)
                        except Exception:
                            pass

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
                log.warning(f"Optimizer-Watchdog Fehler (non-fatal): {e}", exc_info=True)

            # --- Backtest-Watchdog (GH Action v12) ---
            try:
                from app.persistence import check_and_reload_backtest_output
                check_and_reload_backtest_output()
            except Exception as e:
                log.warning(f"Backtest-Watchdog Fehler (non-fatal): {e}", exc_info=True)

            # --- ML-Training-Watchdog (GH Action v12) ---
            try:
                from app.persistence import check_and_reload_ml_training_output
                check_and_reload_ml_training_output()
            except Exception as e:
                log.warning(f"ML-Training-Watchdog Fehler (non-fatal): {e}", exc_info=True)

            # --- Discovery-Watchdog (GH Action v12) ---
            try:
                from app.persistence import check_and_reload_discovery_output
                check_and_reload_discovery_output()
            except Exception as e:
                log.warning(f"Discovery-Watchdog Fehler (non-fatal): {e}", exc_info=True)

            # --- WFO-Watchdog (GH Action v37c, monatlich)
            # Prueft ob neuer WFO-Lauf im Gist liegt -> reloaded + Hard-Gate-Alert
            try:
                from app.persistence import check_and_reload_wfo_output
                check_and_reload_wfo_output()
            except Exception as e:
                log.warning(f"WFO-Watchdog Fehler (non-fatal): {e}", exc_info=True)

            # --- Daily Equity Snapshot (>= 22:30 CET, einmal pro Werktag) ---
            # Schreibt portfolio_total_value + SPY/QQQ/AGG Close in
            # equity_history.json. Daraus baut das Frontend die Monatstabelle
            # (Bot vs Markt). Idempotent via Daily-Guard.
            try:
                from app.equity_snapshot import maybe_take_snapshot
                maybe_take_snapshot(triggered_by="scheduler-daily-2230")
            except Exception as e:
                log.warning(f"Equity-Snapshot Fehler (non-fatal): {e}")

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
    _ensure_trading_flag_initialized()  # v37cw boot-init
    scheduler_loop()
