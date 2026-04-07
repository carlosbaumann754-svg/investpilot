# app/watchdog.py — Bot-Gesundheitsprüfung & Diagnostics
"""
Drei Prüf-Ebenen:
1. Trade-Erfolgsrate: Erkennt wenn CLOSE-Calls systematisch fehlschlagen
2. Zyklen-Aktivität: Erkennt wenn der Bot hängt oder abgestürzt ist
3. Error-Pattern: Erkennt wiederholte Fehlermuster in den Logs
"""

import logging
import re
from datetime import datetime, timedelta

log = logging.getLogger("investpilot")


def run_diagnostics(trade_history=None, brain_state=None, risk_state=None,
                    log_lines=None, config=None):
    """Führe alle Diagnose-Checks durch und gib Report zurück."""
    issues = []
    checks = {}

    # --- Check 1: Letzte Zyklen-Aktivität ---
    checks["cycle_activity"] = _check_cycle_activity(brain_state)
    if checks["cycle_activity"]["status"] == "error":
        issues.append(checks["cycle_activity"]["message"])

    # --- Check 2: Trade-Erfolgsrate ---
    checks["trade_success"] = _check_trade_success(trade_history, log_lines)
    if checks["trade_success"]["status"] == "error":
        issues.append(checks["trade_success"]["message"])

    # --- Check 3: Error-Pattern ---
    checks["error_patterns"] = _check_error_patterns(log_lines)
    if checks["error_patterns"]["status"] == "error":
        issues.append(checks["error_patterns"]["message"])

    # --- Check 4: Margin-Gesundheit ---
    checks["margin_health"] = _check_margin_health(risk_state)
    if checks["margin_health"]["status"] == "warning":
        issues.append(checks["margin_health"]["message"])

    # --- Check 5: Drawdown-Status ---
    checks["drawdown"] = _check_drawdown(risk_state)
    if checks["drawdown"]["status"] in ("error", "warning"):
        issues.append(checks["drawdown"]["message"])

    # --- Check 6: Optimizer-Lock Gesundheit ---
    checks["optimizer_lock"] = _check_optimizer_lock()
    if checks["optimizer_lock"]["status"] in ("error", "warning"):
        issues.append(checks["optimizer_lock"]["message"])

    # Gesamtstatus
    statuses = [c["status"] for c in checks.values()]
    if "error" in statuses:
        overall = "error"
    elif "warning" in statuses:
        overall = "warning"
    else:
        overall = "healthy"

    return {
        "status": overall,
        "timestamp": datetime.now().isoformat(),
        "issues": issues,
        "checks": checks,
    }


def _is_market_hours():
    """Prüfe ob aktuell Handelszeiten sind (Mo-Fr, grob 08-22 Uhr MEZ)."""
    now = datetime.now()
    # Wochenende: Sa=5, So=6
    if now.weekday() >= 5:
        return False
    # Ausserhalb Handelszeiten (vor 08:00 oder nach 22:00)
    if now.hour < 8 or now.hour >= 22:
        return False
    return True


def _check_cycle_activity(brain_state):
    """Prüfe ob Trading-Zyklen regelmässig laufen."""
    if not brain_state:
        return {"status": "warning", "message": "Kein Brain-State verfügbar"}

    snapshots = brain_state.get("performance_snapshots", [])
    if not snapshots:
        return {"status": "warning", "message": "Keine Snapshots vorhanden"}

    last = snapshots[-1]
    last_date = last.get("date", "")
    last_time = last.get("time", "")

    try:
        last_dt = datetime.strptime(f"{last_date} {last_time}", "%Y-%m-%d %H:%M")
        minutes_ago = (datetime.now() - last_dt).total_seconds() / 60

        # Ausserhalb Handelszeiten: kein Alarm
        if not _is_market_hours():
            return {
                "status": "ok",
                "message": f"Ausserhalb Handelszeiten — letzter Zyklus vor {int(minutes_ago)} Min",
                "last_cycle": last_dt.isoformat(),
                "minutes_ago": round(minutes_ago),
            }

        if minutes_ago > 30:
            return {
                "status": "error",
                "message": f"Letzter Zyklus vor {int(minutes_ago)} Minuten — Bot möglicherweise inaktiv",
                "last_cycle": last_dt.isoformat(),
                "minutes_ago": round(minutes_ago),
            }
        elif minutes_ago > 15:
            return {
                "status": "warning",
                "message": f"Letzter Zyklus vor {int(minutes_ago)} Minuten",
                "last_cycle": last_dt.isoformat(),
                "minutes_ago": round(minutes_ago),
            }
        else:
            return {
                "status": "ok",
                "message": f"Letzter Zyklus vor {int(minutes_ago)} Minuten",
                "last_cycle": last_dt.isoformat(),
                "minutes_ago": round(minutes_ago),
            }
    except (ValueError, TypeError):
        return {"status": "warning", "message": f"Snapshot-Zeitformat ungültig: {last_date} {last_time}"}


def _check_trade_success(trade_history, log_lines):
    """Prüfe ob Trades erfolgreich ausgeführt werden."""
    # Prüfe CLOSE-Fehler in den Logs
    close_attempts = 0
    close_errors = 0

    if log_lines:
        for line in log_lines:
            if "CLOSE: PositionID=" in line:
                close_attempts += 1
            if "instrument id does not exist" in line.lower() or \
               ("CLOSE" in line and ("400" in line or "error" in line.lower())):
                close_errors += 1

    if close_attempts > 0:
        error_rate = close_errors / close_attempts
        if error_rate > 0.5:
            return {
                "status": "error",
                "message": f"CLOSE-Fehlerrate {error_rate:.0%} ({close_errors}/{close_attempts}) — API-Problem",
                "close_attempts": close_attempts,
                "close_errors": close_errors,
            }
        elif error_rate > 0.2:
            return {
                "status": "warning",
                "message": f"CLOSE-Fehlerrate {error_rate:.0%} ({close_errors}/{close_attempts})",
                "close_attempts": close_attempts,
                "close_errors": close_errors,
            }

    # Prüfe Trade-History auf rejected Trades
    rejected = 0
    recent_trades = []
    if trade_history:
        recent_trades = trade_history[-20:] if len(trade_history) > 20 else trade_history
        rejected = sum(1 for t in recent_trades if t.get("status") == "rejected")

    if len(recent_trades) > 5 and rejected / len(recent_trades) > 0.5:
        return {
            "status": "error",
            "message": f"{rejected}/{len(recent_trades)} letzte Trades abgelehnt",
            "rejected": rejected,
            "total": len(recent_trades),
        }

    return {
        "status": "ok",
        "message": f"Trades OK ({close_attempts} CLOSE-Versuche, {close_errors} Fehler)",
        "close_attempts": close_attempts,
        "close_errors": close_errors,
    }


def _check_error_patterns(log_lines):
    """Erkennt wiederholte Fehlermuster in den Logs."""
    if not log_lines:
        return {"status": "ok", "message": "Keine Logs verfügbar", "patterns": []}

    error_counts = {}
    for line in log_lines:
        if "[ERROR]" in line or "Traceback" in line or "HTTP 4" in line or "HTTP 5" in line:
            # Normalisiere die Fehlermeldung (IDs/Zahlen entfernen)
            normalized = re.sub(r'\d+', 'N', line.strip())
            normalized = normalized[:120]
            error_counts[normalized] = error_counts.get(normalized, 0) + 1

    # Wiederholte Fehler (>3x gleicher Fehler)
    repeated = {k: v for k, v in error_counts.items() if v >= 3}

    if repeated:
        worst = max(repeated, key=repeated.get)
        return {
            "status": "error",
            "message": f"{len(repeated)} wiederholte Fehlermuster erkannt (häufigstes: {repeated[worst]}x)",
            "patterns": [{"pattern": k, "count": v} for k, v in
                         sorted(repeated.items(), key=lambda x: -x[1])[:5]],
        }

    total_errors = sum(error_counts.values())
    if total_errors > 10:
        return {
            "status": "warning",
            "message": f"{total_errors} Fehler in den Logs (keine wiederholten Muster)",
            "patterns": [],
        }

    return {"status": "ok", "message": f"{total_errors} Fehler in den Logs", "patterns": []}


def _check_margin_health(risk_state):
    """Prüfe den Margin-Puffer."""
    if not risk_state:
        return {"status": "ok", "message": "Kein Risk-State verfügbar"}

    margin_pct = risk_state.get("margin_buffer_pct", 100)

    if margin_pct < 10:
        return {
            "status": "error",
            "message": f"KRITISCH: Margin-Puffer nur {margin_pct}% — Margin Call Gefahr",
            "margin_pct": margin_pct,
        }
    elif margin_pct < 20:
        return {
            "status": "warning",
            "message": f"Margin-Puffer niedrig: {margin_pct}% (Min: 20%)",
            "margin_pct": margin_pct,
        }

    return {"status": "ok", "message": f"Margin-Puffer: {margin_pct}%", "margin_pct": margin_pct}


def _check_drawdown(risk_state):
    """Prüfe den aktuellen Drawdown."""
    if not risk_state:
        return {"status": "ok", "message": "Kein Risk-State verfügbar"}

    daily_dd = risk_state.get("daily_pnl_pct", 0)
    weekly_dd = risk_state.get("weekly_pnl_pct", 0)

    if daily_dd < -5 or weekly_dd < -10:
        return {
            "status": "error",
            "message": f"Drawdown-Limit erreicht: Tag {daily_dd:.1f}%, Woche {weekly_dd:.1f}%",
            "daily_pnl_pct": daily_dd,
            "weekly_pnl_pct": weekly_dd,
        }
    elif daily_dd < -3 or weekly_dd < -5:
        return {
            "status": "warning",
            "message": f"Drawdown erhöht: Tag {daily_dd:.1f}%, Woche {weekly_dd:.1f}%",
            "daily_pnl_pct": daily_dd,
            "weekly_pnl_pct": weekly_dd,
        }

    return {
        "status": "ok",
        "message": f"Drawdown OK: Tag {daily_dd:+.1f}%, Woche {weekly_dd:+.1f}%",
        "daily_pnl_pct": daily_dd,
        "weekly_pnl_pct": weekly_dd,
    }


def _check_optimizer_lock():
    """Erkenne stale Optimizer-Locks (Lauf > 60 Min noch 'running' = Prozess-Kill)."""
    try:
        from app.config_manager import load_json
        status = load_json("optimizer_status.json")
        if not status:
            return {"status": "ok", "message": "Kein Optimizer-Lauf aktiv"}

        state = status.get("state")
        if state != "running":
            if state == "error":
                return {
                    "status": "warning",
                    "message": f"Letzter Optimizer-Lauf fehlgeschlagen: {status.get('error', 'unbekannt')}",
                    "state": state,
                }
            return {
                "status": "ok",
                "message": f"Optimizer-Status: {state}",
                "state": state,
            }

        started = status.get("started_at")
        if not started:
            return {"status": "warning", "message": "Optimizer running aber kein started_at"}

        started_dt = datetime.fromisoformat(started)
        age_min = (datetime.now() - started_dt).total_seconds() / 60

        if age_min > 60:
            return {
                "status": "error",
                "message": f"Optimizer haengt seit {int(age_min)} Min (stale lock)",
                "state": state,
                "age_minutes": round(age_min),
            }
        elif age_min > 30:
            return {
                "status": "warning",
                "message": f"Optimizer laeuft bereits {int(age_min)} Min",
                "state": state,
                "age_minutes": round(age_min),
            }
        return {
            "status": "ok",
            "message": f"Optimizer laeuft seit {int(age_min)} Min",
            "state": state,
            "age_minutes": round(age_min),
        }
    except Exception as e:
        return {"status": "ok", "message": f"Optimizer-Check nicht verfuegbar: {e}"}


def format_telegram_alert(diagnostics):
    """Formatiere Diagnostics als Telegram-Nachricht."""
    status_emoji = {"healthy": "✅", "warning": "⚠️", "error": "🚨"}.get(
        diagnostics["status"], "❓")

    msg = f"{status_emoji} <b>InvestPilot Watchdog</b>\n"
    msg += f"Status: <b>{diagnostics['status'].upper()}</b>\n\n"

    if diagnostics["issues"]:
        msg += "<b>Probleme:</b>\n"
        for issue in diagnostics["issues"]:
            msg += f"• {issue}\n"
    else:
        msg += "Alle Checks bestanden ✅\n"

    msg += f"\n<i>{datetime.now():%d.%m.%Y %H:%M}</i>"
    return msg
