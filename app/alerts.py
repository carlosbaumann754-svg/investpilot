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
        log.warning(f"Telegram Fehler: {e}")
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
