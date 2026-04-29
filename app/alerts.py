"""
InvestPilot - Alerting & Notifications
Telegram/Discord Push-Notifications fuer Trades, Fehler, Drawdowns.
Watchdog-Funktion zur Bot-Ueberwachung.
"""

import logging
import os
import threading
import time
from datetime import datetime

try:
    import requests
except ImportError:
    requests = None

from app.config_manager import load_config, load_json, save_json

log = logging.getLogger("Alerts")

ALERT_STATE_FILE = "alert_state.json"


def _load_alert_state():
    return load_json(ALERT_STATE_FILE) or {
        "last_heartbeat": None,
        "alerts_sent_today": 0,
        "last_daily_summary": None,
    }


def _save_alert_state(state):
    save_json(ALERT_STATE_FILE, state)


def _tg_config(config=None):
    """Lade Telegram-Konfiguration (Helper)."""
    if config is None:
        config = load_config()
    return config.get("alerts", {}).get("telegram", {})


def _tg_notify_enabled(event_type, config=None):
    """Pruefe ob ein bestimmter Telegram-Benachrichtigungstyp aktiviert ist.

    event_type: 'trades', 'stop_loss', 'regime_change', 'daily_summary',
                'weekly_report', 'optimizer'
    """
    tg_cfg = _tg_config(config)
    if not tg_cfg.get("enabled", False):
        return False
    return tg_cfg.get(f"notify_{event_type}", True)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message, config=None):
    """Sende Nachricht via Telegram Bot."""
    if not requests:
        return False
    if config is None:
        config = load_config()

    tg_cfg = config.get("alerts", {}).get("telegram", {})
    bot_token = tg_cfg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = tg_cfg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        return False

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        log.warning(f"Telegram Fehler: {e}", exc_info=True)
        return False


# ============================================================
# DISCORD
# ============================================================

def send_discord(message, config=None):
    """Sende Nachricht via Discord Webhook."""
    if not requests:
        return False
    if config is None:
        config = load_config()

    dc_cfg = config.get("alerts", {}).get("discord", {})
    webhook_url = dc_cfg.get("webhook_url") or os.environ.get("DISCORD_WEBHOOK_URL", "")

    if not webhook_url:
        return False

    try:
        resp = requests.post(webhook_url, json={
            "content": message,
        }, timeout=10)
        return resp.status_code in (200, 204)
    except Exception as e:
        log.warning(f"Discord Fehler: {e}")
        return False


# ============================================================
# UNIFIED ALERT
# ============================================================

def send_alert(message, level="INFO", config=None):
    """Sende Alert ueber alle konfigurierten Kanaele."""
    if config is None:
        config = load_config()
    alert_cfg = config.get("alerts", {})

    prefix = {
        "INFO": "\u2139\ufe0f",
        "WARNING": "\u26a0\ufe0f",
        "ERROR": "\u274c",
        "CRITICAL": "\U0001f6a8",
        "TRADE": "\U0001f4b0",
        "PROFIT": "\U0001f4b5",
        "LOSS": "\U0001f4c9",
    }.get(level, "\u2139\ufe0f")

    formatted = f"{prefix} <b>InvestPilot</b>\n{message}\n<i>{datetime.now():%d.%m.%Y %H:%M}</i>"

    sent = False
    if alert_cfg.get("telegram", {}).get("enabled", False):
        sent = send_telegram(formatted, config) or sent
    if alert_cfg.get("discord", {}).get("enabled", False):
        sent = send_discord(f"{prefix} **InvestPilot**\n{message}\n*{datetime.now():%d.%m.%Y %H:%M}*", config) or sent

    if sent:
        state = _load_alert_state()
        state["alerts_sent_today"] = state.get("alerts_sent_today", 0) + 1
        _save_alert_state(state)

    return sent


# ============================================================
# TRADE-NOTIFICATIONS
# ============================================================

def alert_trade_executed(trade_entry, config=None):
    """Sende Notification fuer ausgefuehrten Trade."""
    if config is None:
        config = load_config()

    action = trade_entry.get("action", "?")
    symbol = trade_entry.get("symbol", "?")
    amount = trade_entry.get("amount_usd", 0)
    leverage = trade_entry.get("leverage", 1)
    score = trade_entry.get("scanner_score", "")
    pnl = trade_entry.get("pnl_pct", "")

    # Bestimme ob Stop-Loss oder normaler Trade
    is_stop_loss = action in ("STOP_LOSS_CLOSE", "TRAILING_SL_CLOSE")

    # Pruefe granulare Telegram-Einstellung
    if is_stop_loss and not _tg_notify_enabled("stop_loss", config):
        return
    if not is_stop_loss and not _tg_notify_enabled("trades", config):
        return

    if "CLOSE" in action or "SELL" in action:
        pnl_str = f"\nP/L: {pnl:+.1f}%" if pnl else ""
        msg = f"<b>{action}</b>: {symbol}{pnl_str}"

        # Detailliertere Stop-Loss Nachrichten
        if action == "STOP_LOSS_CLOSE":
            msg = f"\U0001f6d1 <b>STOP-LOSS</b>: {symbol}{pnl_str}"
            if trade_entry.get("pnl_usd"):
                msg += f"\nVerlust: ${trade_entry['pnl_usd']:+,.2f}"
        elif action == "TRAILING_SL_CLOSE":
            sl_level = trade_entry.get("trailing_sl_level", "?")
            msg = f"\U0001f4c9 <b>TRAILING SL</b>: {symbol}{pnl_str}"
            msg += f"\nSL-Level: {sl_level}"
            if trade_entry.get("pnl_usd"):
                msg += f"\nP/L: ${trade_entry['pnl_usd']:+,.2f}"

        level = "PROFIT" if isinstance(pnl, (int, float)) and pnl > 0 else "LOSS"
    else:
        score_str = f" (Score: {score:+.1f})" if score else ""
        lev_str = f" {leverage}x" if leverage > 1 else ""
        asset_class = trade_entry.get("asset_class", "")
        class_str = f" [{asset_class}]" if asset_class else ""
        msg = f"<b>{action}</b>: {symbol}{class_str} ${amount:,.2f}{lev_str}{score_str}"
        level = "TRADE"

    send_alert(msg, level, config)


def alert_drawdown(daily_pnl_pct, weekly_pnl_pct, reason, config=None):
    """Sende Drawdown-Warnung."""
    msg = (f"<b>DRAWDOWN WARNING</b>\n"
           f"Tages-P/L: {daily_pnl_pct:+.1f}%\n"
           f"Wochen-P/L: {weekly_pnl_pct:+.1f}%\n"
           f"Aktion: {reason}")
    send_alert(msg, "WARNING", config)


def alert_error(error_msg, context="", config=None):
    """Sende Fehler-Notification."""
    msg = f"<b>FEHLER</b>"
    if context:
        msg += f" ({context})"
    msg += f"\n{error_msg}"
    send_alert(msg, "ERROR", config)


def alert_emergency(reason, closed_count=0, config=None):
    """Sende Emergency-Alert (Kill Switch)."""
    msg = (f"<b>EMERGENCY STOP</b>\n"
           f"Grund: {reason}\n"
           f"Positionen geschlossen: {closed_count}\n"
           f"Trading DEAKTIVIERT")
    send_alert(msg, "CRITICAL", config)


# ============================================================
# REGIME CHANGE NOTIFICATIONS
# ============================================================

def alert_regime_halt(regime_reason, regime_data=None, config=None):
    """Sende Notification wenn Regime-Filter Trading stoppt."""
    if config is None:
        config = load_config()
    if not _tg_notify_enabled("regime_change", config):
        return

    msg = f"\U0001f6ab <b>REGIME HALT AKTIVIERT</b>\n{regime_reason}"
    if regime_data:
        if "vix" in regime_data:
            msg += f"\nVIX: {regime_data['vix']:.1f}"
        if "fear_greed" in regime_data:
            msg += f"\nFear&Greed: {regime_data['fear_greed']}"
        if "regime" in regime_data:
            msg += f"\nRegime: {regime_data['regime']}"
    send_alert(msg, "WARNING", config)


def alert_regime_resumed(config=None):
    """Sende Notification wenn Regime-Filter Trading wieder erlaubt."""
    if config is None:
        config = load_config()
    if not _tg_notify_enabled("regime_change", config):
        return

    msg = "\u2705 <b>REGIME HALT AUFGEHOBEN</b>\nTrading wieder aktiv."
    send_alert(msg, "INFO", config)


# ============================================================
# WEEKLY REPORT NOTIFICATION
# ============================================================

def alert_weekly_report(report, config=None):
    """Sende Zusammenfassung des Weekly Reports via Telegram."""
    if config is None:
        config = load_config()
    if not _tg_notify_enabled("weekly_report", config):
        return

    perf = report.get("performance", {})
    trades = report.get("weekly_trades", {})
    suggestions = report.get("suggestions", [])

    total_return = perf.get("total_return_pct", 0)
    portfolio_value = perf.get("portfolio_value", 0)

    msg = (f"\U0001f4ca <b>WEEKLY REPORT</b> (KW {datetime.now().isocalendar()[1]})\n\n"
           f"Portfolio: ${portfolio_value:,.2f}\n"
           f"Gesamt-Rendite: {total_return:+.2f}%\n"
           f"Trades diese Woche: {trades.get('total_trades', 0)}\n"
           f"  Kaeufe: {trades.get('buys', 0)} | Verkaeufe: {trades.get('sells', 0)}\n"
           f"  SL: {trades.get('sl_closes', 0)} | TP: {trades.get('tp_closes', 0)}\n"
           f"Volumen: ${trades.get('total_volume_usd', 0):,.0f}")

    if suggestions:
        msg += f"\n\n\u26a0\ufe0f {len(suggestions)} Verbesserungsvorschlaege"
        for s in suggestions[:3]:
            msg += f"\n  - [{s.get('prioritaet', '?')}] {s.get('vorschlag', '')[:80]}"

    send_alert(msg, "INFO", config)


# ============================================================
# OPTIMIZER NOTIFICATION
# ============================================================

def alert_optimizer_completed(result, config=None):
    """Sende Notification wenn Optimizer abgeschlossen ist."""
    if config is None:
        config = load_config()
    if not _tg_notify_enabled("optimizer", config):
        return

    action = result.get("action", "unknown")
    changes = result.get("changes", {})

    if action == "rollback":
        msg = (f"\u21a9\ufe0f <b>OPTIMIZER ROLLBACK</b>\n"
               f"Grund: {result.get('reason', '?')}")
    elif action == "optimized":
        msg = f"\u2699\ufe0f <b>OPTIMIZER ABGESCHLOSSEN</b>\nAenderungen:"
        for key, val in changes.items():
            msg += f"\n  {key}: {val['old']} \u2192 {val['new']}"
        grid = result.get("grid_search", {})
        if grid.get("best_oos_sharpe") is not None:
            msg += f"\nBester OOS-Sharpe: {grid['best_oos_sharpe']:.2f}"
        msg += f"\nGetestet: {grid.get('total_tested', 0)} Kombinationen"
    elif action == "no_change":
        msg = ("\u2705 <b>OPTIMIZER ABGESCHLOSSEN</b>\n"
               "Keine Aenderungen - aktuelle Parameter sind optimal.")
    else:
        msg = f"\u2699\ufe0f <b>OPTIMIZER</b>: {action}"
        if result.get("error"):
            msg += f"\nFehler: {result['error']}"

    send_alert(msg, "INFO", config)


# ============================================================
# DAILY SUMMARY
# ============================================================

def send_daily_summary(portfolio_value, daily_pnl_pct, daily_pnl_usd,
                       trades_today, brain_regime, config=None):
    """Sende taegliche Zusammenfassung (abends)."""
    if config is None:
        config = load_config()

    # Pruefe granulare Einstellung
    if not _tg_notify_enabled("daily_summary", config):
        return

    msg = (f"<b>Tages-Zusammenfassung</b>\n"
           f"Portfolio: ${portfolio_value:,.2f}\n"
           f"Tages-P/L: {daily_pnl_pct:+.1f}% (${daily_pnl_usd:+,.2f})\n"
           f"Trades heute: {trades_today}\n"
           f"Regime: {brain_regime}")
    send_alert(msg, "INFO", config)

    state = _load_alert_state()
    state["last_daily_summary"] = datetime.now().isoformat()
    _save_alert_state(state)


def should_send_daily_summary():
    """Pruefe ob taegliche Zusammenfassung gesendet werden soll (21:00-21:05)."""
    now = datetime.now()
    state = _load_alert_state()

    if now.hour != 21 or now.minute >= 5:
        return False

    last = state.get("last_daily_summary")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.date() == now.date():
                return False
        except (ValueError, TypeError):
            pass

    return True


# ============================================================
# WATCHDOG
# ============================================================

def update_heartbeat():
    """Setze Heartbeat-Timestamp (wird bei jedem Trading-Zyklus aufgerufen)."""
    state = _load_alert_state()
    state["last_heartbeat"] = datetime.now().isoformat()
    _save_alert_state(state)


def check_watchdog(max_silence_minutes=15):
    """Pruefe ob Bot noch aktiv ist. Gibt (alive, minutes_since_last) zurueck."""
    state = _load_alert_state()
    last = state.get("last_heartbeat")

    if not last:
        return True, 0  # Erster Start

    try:
        last_dt = datetime.fromisoformat(last)
        elapsed = (datetime.now() - last_dt).total_seconds() / 60
        return elapsed < max_silence_minutes, round(elapsed, 1)
    except (ValueError, TypeError):
        return True, 0


def watchdog_thread(check_interval_seconds=600):
    """Hintergrund-Thread der Bot-Aktivitaet ueberwacht."""
    log.info("Watchdog Thread gestartet")
    while True:
        time.sleep(check_interval_seconds)
        alive, minutes = check_watchdog()
        if not alive:
            log.warning(f"WATCHDOG: Bot inaktiv seit {minutes:.0f} Minuten!")
            alert_error(
                f"Bot scheint inaktiv zu sein (letzter Heartbeat vor {minutes:.0f} Min)",
                context="Watchdog"
            )


def start_watchdog():
    """Starte Watchdog als Daemon-Thread."""
    t = threading.Thread(target=watchdog_thread, daemon=True)
    t.start()
    return t


# ============================================================
# TELEGRAM COMMAND HANDLER
# ============================================================

def check_telegram_commands(config=None):
    """Pruefe ob Telegram-Befehle eingegangen sind (z.B. /killswitch).

    Wird periodisch vom Scheduler aufgerufen.
    """
    if not requests:
        return None
    if config is None:
        config = load_config()

    tg_cfg = config.get("alerts", {}).get("telegram", {})
    bot_token = tg_cfg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return None

    state = _load_alert_state()
    last_update_id = state.get("telegram_last_update_id", 0)

    try:
        url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
        resp = requests.get(url, params={
            "offset": last_update_id + 1,
            "timeout": 1,
        }, timeout=5)

        if resp.status_code != 200:
            return None

        updates = resp.json().get("result", [])
        commands = []

        for update in updates:
            update_id = update.get("update_id", 0)
            msg = update.get("message", {})
            text = msg.get("text", "").strip().lower()

            if text in ("/killswitch", "/kill", "/emergency", "/stop"):
                commands.append({"command": "killswitch", "update_id": update_id})
            elif text in ("/status", "/stats"):
                commands.append({"command": "status", "update_id": update_id})
            elif text in ("/start", "/resume"):
                commands.append({"command": "start", "update_id": update_id})

            state["telegram_last_update_id"] = max(
                state.get("telegram_last_update_id", 0), update_id)

        _save_alert_state(state)
        return commands if commands else None

    except Exception as e:
        log.debug(f"Telegram Command Check Fehler: {e}")
        return None


# ============================================================
# BROKER CONNECTION HEALTH (W6+ — IBKR Paper-Phase)
# ============================================================

def check_broker_health(client, config=None, max_attempts=2, retry_wait_s=5.0):
    """Schneller Health-Check des aktiven Brokers + Telegram-Alert bei Drop.

    Wird von run_trading_cycle() VOR dem ersten get_portfolio() aufgerufen.
    Dedupliziert Alerts via alert_state['broker_last_health'] — sendet nur
    bei State-Wechsel (ok->fail, fail->ok), nicht bei jedem Cycle.

    Args:
        client: BrokerBase-Instanz (EtoroClient oder IbkrBroker)
        config: optional config dict fuer alerts
        max_attempts: Wie oft wir es probieren bevor wir 'fail' melden.
                      Default 2 schuetzt vor Race-Conditions mit z.B. dem
                      Reconciliation-Cron (gleichzeitiger IBKR-Connect).
        retry_wait_s: Pause zwischen den Attempts (default 5s — gibt IBG
                      Zeit Connection-Pool zu reset'en).

    Returns:
        True wenn Broker erreichbar (in mind. einem der Versuche), False sonst.
    """
    import time as _time
    # Lazy disconnect zwischen Attempts (gibt Connection-Pool sauberen Reset)
    state = _load_alert_state()
    last = state.get("broker_last_health", "unknown")  # "ok" | "fail" | "unknown"
    broker_name = getattr(client, "broker_name", "?")

    healthy = False
    error_detail = None
    attempt_errors = []
    for attempt in range(1, max_attempts + 1):
        try:
            eq = client.get_equity()
            if eq is not None and float(eq) > 0:
                healthy = True
                break
            attempt_errors.append(f"attempt {attempt}: get_equity returned {eq!r}")
        except Exception as e:
            attempt_errors.append(f"attempt {attempt}: {type(e).__name__}: {e}")
        # Retry nur wenn noch Versuche uebrig
        if attempt < max_attempts:
            log.warning("Broker-Healthcheck attempt %d/%d failed (%s) — retry in %.1fs",
                        attempt, max_attempts, attempt_errors[-1], retry_wait_s)
            # Force-Disconnect bevor Retry: bei Singleton-Pool muss
            # force_disconnect() gerufen werden (disconnect ist no-op fuer reuse).
            try:
                if hasattr(client, "force_disconnect"):
                    client.force_disconnect()
                elif hasattr(client, "disconnect"):
                    client.disconnect()
            except Exception:
                pass
            _time.sleep(retry_wait_s)

    if not healthy:
        error_detail = " | ".join(attempt_errors)

    new_state = "ok" if healthy else "fail"

    if last != new_state:
        # State-Change → Alert
        if new_state == "fail":
            send_alert(
                f"🔴 Broker '{broker_name.upper()}' Connection LOST\n"
                f"Detail: {error_detail}\n"
                f"Bot pausiert keine Trades — naechster Cycle versucht erneut.",
                level="ERROR", config=config,
            )
        else:
            send_alert(
                f"🟢 Broker '{broker_name.upper()}' Connection RESTORED",
                level="INFO", config=config,
            )
        state["broker_last_health"] = new_state
        _save_alert_state(state)

    return healthy


# ============================================================
# WFO ALERTS (v37c, Option-3 Hybrid Auto-Run + Push-Notification)
# ============================================================

# 3 Hard-Gate-Trigger fuer Telegram-Alerts:
WFO_HARD_MIN_OOS_SHARPE = 2.0    # unter dieser Schwelle -> Edge-Erosion
WFO_HARD_MIN_DECAY_PCT  = 50.0   # IS->OOS Retention < 50% -> Overfitting
# Plus: best_params haben sich aenderte ggu. letztem Run -> Manual Review


def _build_wfo_message(status: dict, history: dict, hard_gate_violations: list[str],
                      param_changes: list[str]) -> str:
    """Telegram-Nachricht zusammenbauen — nur kritische Infos."""
    agg = status.get("aggregate") or {}
    runs = (history or {}).get("runs") or []
    last_run = runs[-1] if runs else {}
    prev_run = runs[-2] if len(runs) >= 2 else {}

    lines = []
    if hard_gate_violations:
        lines.append("🔴 *WFO HARD-GATE VERLETZUNG*")
    elif param_changes:
        lines.append("🟡 *WFO Param-Change* (Manual Review)")
    else:
        lines.append("✅ *WFO OK*")
    lines.append("")
    lines.append(f"Mean OOS-Sharpe: *{agg.get('mean_oos_sharpe', '--')}*")
    lines.append(f"Sharpe-Decay: *{agg.get('sharpe_decay_pct', '--')}%*")
    lines.append(f"OOS-Stability (StdDev): {agg.get('oos_stability_std', '--')}")
    lines.append(f"Mean OOS-Trades: {agg.get('mean_oos_trades', '--')}")

    if hard_gate_violations:
        lines.append("")
        lines.append("*Verletzungen:*")
        for v in hard_gate_violations:
            lines.append(f"• {v}")

    if param_changes:
        lines.append("")
        lines.append("*Param-Changes (vs vorherigem Run):*")
        for c in param_changes:
            lines.append(f"• {c}")

    if prev_run:
        lines.append("")
        lines.append(f"_Letzter Run vorher: Sharpe {prev_run.get('mean_oos_sharpe','--')}, "
                     f"Decay {prev_run.get('sharpe_decay_pct','--')}%_")

    return "\n".join(lines)


def check_wfo_alerts(config=None):
    """Prueft WFO-Status nach jedem reload und sendet ggf. Telegram-Alert.

    Aufgerufen von persistence.check_and_reload_wfo_output() nach erfolgreichem
    Reload. State-deduped via alert_state.json (gleiche Logik wie health-check).
    """
    try:
        status = load_json("wfo_status.json") or {}
        history = load_json("wfo_history.json") or {}
    except Exception as e:
        log.warning(f"check_wfo_alerts: status load failed: {e}")
        return

    if status.get("state") != "done":
        return  # nur done-State alerten

    agg = status.get("aggregate") or {}
    last_run_iso = status.get("last_run", "")

    # Dedupe: gleichen Run nicht zweimal alerten
    state = _load_alert_state()
    last_alerted = state.get("wfo_last_alerted_run")
    if last_alerted == last_run_iso:
        log.debug(f"check_wfo_alerts: Run {last_run_iso} bereits alerted, skip")
        return

    # Hard-Gates pruefen
    violations = []
    sharpe = agg.get("mean_oos_sharpe")
    decay = agg.get("sharpe_decay_pct")
    if sharpe is not None and sharpe < WFO_HARD_MIN_OOS_SHARPE:
        violations.append(f"Mean OOS-Sharpe {sharpe} < {WFO_HARD_MIN_OOS_SHARPE} (Edge-Erosion)")
    if decay is not None and decay < WFO_HARD_MIN_DECAY_PCT:
        violations.append(f"Sharpe-Decay {decay}% < {WFO_HARD_MIN_DECAY_PCT}% (Overfitting-Verdacht)")

    # Param-Changes vs vorigem Run (best_params Trends in history)
    param_changes = []
    runs = (history or {}).get("runs") or []
    if len(runs) >= 2:
        cur = runs[-1].get("param_summary") or {}
        prev = runs[-2].get("param_summary") or {}
        for key in cur:
            cur_dominant = max(cur[key].items(), key=lambda x: x[1])[0] if cur.get(key) else None
            prev_dominant = max(prev[key].items(), key=lambda x: x[1])[0] if prev.get(key) else None
            if cur_dominant and prev_dominant and cur_dominant != prev_dominant:
                param_changes.append(f"{key}: {prev_dominant} -> {cur_dominant}")

    # Wenn weder Verletzung noch Change -> kein Alert (Stille = OK)
    if not violations and not param_changes:
        log.info("check_wfo_alerts: alle Hard-Gates OK, keine Param-Changes")
        # Trotzdem markieren damit naechster Restart nicht erneut prueft
        state["wfo_last_alerted_run"] = last_run_iso
        state["wfo_last_check"] = datetime.now().isoformat()
        _save_alert_state(state)
        return

    # Alert senden
    msg = _build_wfo_message(status, history, violations, param_changes)
    level = "ERROR" if violations else "WARN"
    send_alert(msg, level=level, config=config)
    state["wfo_last_alerted_run"] = last_run_iso
    state["wfo_last_check"] = datetime.now().isoformat()
    state["wfo_last_violations"] = violations
    state["wfo_last_param_changes"] = param_changes
    _save_alert_state(state)
    log.info(f"check_wfo_alerts: Alert gesendet ({len(violations)} Verletzungen, "
             f"{len(param_changes)} Param-Changes)")


# ============================================================
# SURVIVORSHIP-ALERTS (E4 Auto-Run, Wochentlich Sonntag 13 UTC)
# ============================================================

# Hard-Gate-Trigger fuer Telegram:
SURV_ALERT_DEAD_THRESHOLD = 1            # mind. 1 totes Symbol -> Alert
SURV_ALERT_SUSPICIOUS_THRESHOLD = 2      # mind. 2 suspicious -> Alert
SURV_ALERT_BIAS_DRIFT_THRESHOLD = 0.10   # Bias-Estimate-Drift > 0.10 vs voriger Run


def _build_survivorship_message(summary: dict, history_runs: list,
                                violations: list[str], drift_info: dict | None) -> str:
    lines = []
    if violations:
        lines.append("🔴 *Survivorship-Audit: ANOMALIE*")
    elif drift_info:
        lines.append("🟡 *Survivorship-Audit: Bias-Drift*")
    else:
        lines.append("✅ *Survivorship-Audit OK*")
    lines.append("")
    lines.append(f"Universe: *{summary.get('universe_size', '--')}* Symbole")
    lines.append(f"Live-Check: alive={summary.get('live_alive', '--')}, "
                 f"dead={summary.get('live_dead', '--')}, "
                 f"suspicious={summary.get('live_suspicious', '--')}")
    lines.append(f"Sharpe-Reduktion: *{summary.get('estimated_sharpe_reduction_point', '--')}*")
    wfo = summary.get("wfo_correction") or {}
    if wfo:
        lines.append(f"WFO-Korrektur: {wfo.get('wfo_mean_oos_sharpe', '--')} -> "
                     f"*{wfo.get('corrected_point_estimate', '--')}*")
    if violations:
        lines.append("")
        lines.append("*Verletzungen:*")
        for v in violations:
            lines.append(f"• {v}")
    if drift_info:
        lines.append("")
        lines.append("*Drift seit letztem Run:*")
        lines.append(f"• Bias-Estimate {drift_info['prev']} -> {drift_info['cur']} "
                     f"(Δ {drift_info['delta']:+.3f})")
    return "\n".join(lines)


def check_survivorship_alerts(config=None):
    """Prueft Survivorship-Audit-Resultate, sendet Telegram nur bei Anomalien.

    Trigger:
      1. dead-Symbole >= 1 -> Universe-Update noetig (ERROR)
      2. suspicious >= 2 -> mehrere Symbole liefern keine Daten (WARN)
      3. Bias-Estimate-Drift > 0.10 vs voriger Run -> Universe-Quality erodiert (WARN)

    State-Dedupe: gleicher generated_at-Timestamp wird nie zweimal alerted.
    """
    try:
        summary = load_json("survivorship_audit_summary.json") or {}
        history = load_json("survivorship_history.json") or {}
    except Exception as e:
        log.warning("check_survivorship_alerts: load failed: %s", e)
        return

    if not summary.get("generated_at"):
        return

    state = _load_alert_state()
    last_alerted = state.get("survivorship_last_alerted_run")
    if last_alerted == summary["generated_at"]:
        log.debug("check_survivorship_alerts: bereits alerted")
        return

    # Hard-Gates pruefen
    violations = []
    dead = summary.get("live_dead") or 0
    susp = summary.get("live_suspicious") or 0
    if dead >= SURV_ALERT_DEAD_THRESHOLD:
        violations.append(f"dead-Symbole: {dead} (>= {SURV_ALERT_DEAD_THRESHOLD})")
    if susp >= SURV_ALERT_SUSPICIOUS_THRESHOLD:
        violations.append(f"suspicious-Symbole: {susp} (>= {SURV_ALERT_SUSPICIOUS_THRESHOLD})")

    # Bias-Drift vs voriger Run
    drift_info = None
    runs = (history or {}).get("runs") or []
    if len(runs) >= 2:
        cur_bias = runs[-1].get("sharpe_reduction_point")
        prev_bias = runs[-2].get("sharpe_reduction_point")
        if cur_bias is not None and prev_bias is not None:
            delta = cur_bias - prev_bias
            if abs(delta) > SURV_ALERT_BIAS_DRIFT_THRESHOLD:
                drift_info = {"prev": prev_bias, "cur": cur_bias, "delta": delta}

    if not violations and not drift_info:
        log.info("check_survivorship_alerts: alles OK, kein Alert")
        state["survivorship_last_alerted_run"] = summary["generated_at"]
        state["survivorship_last_check"] = datetime.now().isoformat()
        _save_alert_state(state)
        return

    msg = _build_survivorship_message(summary, runs, violations, drift_info)
    level = "ERROR" if violations else "WARN"
    send_alert(msg, level=level, config=config)
    state["survivorship_last_alerted_run"] = summary["generated_at"]
    state["survivorship_last_check"] = datetime.now().isoformat()
    state["survivorship_last_violations"] = violations
    if drift_info:
        state["survivorship_last_drift"] = drift_info
    _save_alert_state(state)
    log.info("check_survivorship_alerts: Alert gesendet (%d Verletzungen, drift=%s)",
             len(violations), drift_info is not None)
