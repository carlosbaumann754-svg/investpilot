"""v37h+2 (17.05.2026) — Sharpe-Drift-Watchdog zwischen WFO-Runs.

PROBLEM: WFO laeuft monatlich (1. Sonntag). Zwischen den Runs gibt es
keinen automatischen Sanity-Check ob die Live-Sharpe-Realitaet zur WFO-
Empfehlung passt. Bei plötzlichem Sharpe-Decay (Regime-Shift, Strategy-
Edge erodiert) wuerde Carlos das 30 Tage lang nicht sehen.

LOESUNG: Taeglicher leichtgewichtiger Drift-Check der:
  1. Live-Trade-Sharpe der letzten N Tage berechnet (aus trade_history.json)
  2. Mit WFO-Empfehlung (Mean OOS-Sharpe) vergleicht
  3. Bei Drift > Threshold -> Pushover-WARNING + Marker fuer Dashboard

KEINE automatische WFO-Re-Trigger (das waere Curve-Fitting-Risiko bei
Mini-Sample). Carlos entscheidet manuell ob WFO-Manual-Run sinnvoll ist.

USAGE:
    from app.wfo_drift_watchdog import check_wfo_drift
    result = check_wfo_drift()
    # result = {"drift_pct": -42.5, "alert_triggered": True, ...}
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# Konfigurations-Defaults (override via config.json.wfo_drift_watchdog)
DEFAULT_LOOKBACK_DAYS = 30           # Live-Sharpe ueber letzte 30 Trade-Tage
DEFAULT_DRIFT_THRESHOLD_PCT = 30.0   # > 30% Sharpe-Decay -> Alert
DEFAULT_MIN_TRADES = 10              # weniger Trades = nicht-aussagekraeftig
DEFAULT_ALERT_THROTTLE_HOURS = 24    # max 1 Alert pro Tag


def _get_wfo_target_sharpe() -> Optional[float]:
    """Liest Mean OOS-Sharpe aus letztem WFO-Run."""
    try:
        from app.config_manager import load_json
        wfo = load_json("wfo_status.json") or {}
    except Exception:
        return None

    windows = wfo.get("windows", []) if isinstance(wfo, dict) else []
    if not windows:
        return None

    oos_sharpes = [w.get("oos_sharpe", 0) for w in windows
                   if isinstance(w, dict) and w.get("oos_sharpe") is not None]
    if not oos_sharpes:
        return None

    return sum(oos_sharpes) / len(oos_sharpes)


def _compute_live_sharpe(trade_history: list, lookback_days: int) -> tuple[Optional[float], int]:
    """Berechnet Sharpe aus den Close-Trades der letzten N Tage.

    Returns (sharpe, n_trades). sharpe=None wenn nicht genug Daten.
    """
    if not trade_history:
        return None, 0

    cutoff = datetime.now() - timedelta(days=lookback_days)
    recent_pnls = []

    for t in trade_history:
        # Nur Close-Trades zaehlen (haben pnl)
        action = (t.get("action") or "").upper()
        if not any(s in action for s in (
                "CLOSE", "STOP_LOSS", "TAKE_PROFIT", "TIME_STOP", "TRAILING")):
            continue

        ts_str = t.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
        except Exception:
            continue
        if ts < cutoff:
            continue

        pnl_pct = t.get("pnl_net_pct", t.get("pnl_pct"))
        if pnl_pct is None:
            continue
        try:
            recent_pnls.append(float(pnl_pct))
        except (TypeError, ValueError):
            continue

    n = len(recent_pnls)
    if n < 2:
        return None, n

    # Sharpe = mean / std (sample). Annualisiert nicht — Vergleich gegen
    # WFO-OOS-Sharpe der gleicher Convention folgt.
    mean = sum(recent_pnls) / n
    var = sum((p - mean) ** 2 for p in recent_pnls) / (n - 1)
    if var <= 0:
        return None, n
    std = var ** 0.5
    return (mean / std), n


def _load_alert_state() -> dict:
    try:
        from app.config_manager import load_json
        return load_json("wfo_drift_alert_state.json") or {}
    except Exception:
        return {}


def _save_alert_state(state: dict) -> None:
    try:
        from app.config_manager import save_json
        save_json("wfo_drift_alert_state.json", state)
    except Exception as e:
        log.warning(f"WFO-Drift-Alert-State save fehlgeschlagen: {e}")


def check_wfo_drift(config: Optional[dict] = None) -> dict:
    """Hauptfunktion: prueft Sharpe-Drift + sendet Pushover-Alert bei Threshold.

    Returns Status-Dict mit:
        wfo_sharpe (float | None)         WFO-Empfehlung
        live_sharpe (float | None)        Live-Realitaet letzte N Tage
        drift_pct (float | None)          (live-wfo)/wfo*100 (negativ = Decay)
        n_trades (int)                    Sample-Size
        alert_triggered (bool)            Wurde Pushover gesendet?
        skip_reason (str | None)          Falls geskipped (zu wenig Daten etc.)
    """
    if config is None:
        try:
            from app.config_manager import load_config
            config = load_config() or {}
        except Exception:
            config = {}

    cfg = (config or {}).get("wfo_drift_watchdog", {}) or {}
    lookback_days = int(cfg.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
    threshold_pct = float(cfg.get("drift_threshold_pct", DEFAULT_DRIFT_THRESHOLD_PCT))
    min_trades = int(cfg.get("min_trades", DEFAULT_MIN_TRADES))
    throttle_h = float(cfg.get("alert_throttle_hours", DEFAULT_ALERT_THROTTLE_HOURS))
    enabled = bool(cfg.get("enabled", True))  # Default ON

    result = {
        "wfo_sharpe": None,
        "live_sharpe": None,
        "drift_pct": None,
        "n_trades": 0,
        "alert_triggered": False,
        "skip_reason": None,
        "checked_at": datetime.now().isoformat(),
    }

    if not enabled:
        result["skip_reason"] = "Feature disabled (config.wfo_drift_watchdog.enabled=False)"
        return result

    wfo_sharpe = _get_wfo_target_sharpe()
    if wfo_sharpe is None:
        result["skip_reason"] = "Keine WFO-Daten in wfo_status.json"
        return result
    result["wfo_sharpe"] = round(wfo_sharpe, 3)

    try:
        from app.config_manager import load_json
        trade_history = load_json("trade_history.json") or []
    except Exception:
        result["skip_reason"] = "trade_history.json nicht ladbar"
        return result

    live_sharpe, n = _compute_live_sharpe(trade_history, lookback_days)
    result["n_trades"] = n
    if live_sharpe is None or n < min_trades:
        result["skip_reason"] = f"Zu wenig Trades (n={n}, min={min_trades})"
        return result
    result["live_sharpe"] = round(live_sharpe, 3)

    # Drift in % — negativ = Decay
    if wfo_sharpe == 0:
        result["skip_reason"] = "WFO-Sharpe = 0, kann nicht dividieren"
        return result
    drift_pct = (live_sharpe - wfo_sharpe) / abs(wfo_sharpe) * 100
    result["drift_pct"] = round(drift_pct, 2)

    # Pushover-Alert bei Decay > Threshold (drift_pct < -threshold)
    if drift_pct < -threshold_pct:
        # Throttle: max 1 Alert pro Tag
        state = _load_alert_state()
        last_alert_ts = state.get("last_alert_at")
        skip_alert = False
        if last_alert_ts:
            try:
                last_alert = datetime.fromisoformat(str(last_alert_ts).replace("Z", "+00:00"))
                if last_alert.tzinfo is not None:
                    last_alert = last_alert.replace(tzinfo=None)
                if (datetime.now() - last_alert).total_seconds() < throttle_h * 3600:
                    skip_alert = True
            except Exception:
                pass

        if not skip_alert:
            try:
                from app.alerts import send_alert
                msg = (
                    f"WFO-DRIFT-ALARM: Live-Sharpe {live_sharpe:.2f} vs "
                    f"WFO-Empfehlung {wfo_sharpe:.2f} ({drift_pct:+.1f}%% Decay). "
                    f"N={n} Trades letzte {lookback_days}d. "
                    f"Aktion: pruefe ob Regime-Shift -> manueller WFO-Re-Run sinnvoll."
                )
                send_alert(msg, level="WARNING")
                result["alert_triggered"] = True
                state["last_alert_at"] = datetime.now().isoformat()
                state["last_drift_pct"] = round(drift_pct, 2)
                state["last_live_sharpe"] = round(live_sharpe, 3)
                state["last_wfo_sharpe"] = round(wfo_sharpe, 3)
                _save_alert_state(state)
                log.warning(
                    f"WFO-Drift-Alert gesendet: live={live_sharpe:.2f}, "
                    f"wfo={wfo_sharpe:.2f}, drift={drift_pct:+.1f}%%"
                )
            except Exception as e:
                log.warning(f"WFO-Drift-Alert konnte nicht gesendet werden: {e}")
        else:
            result["skip_reason"] = f"Alert gethrottlet (letzter < {throttle_h}h her)"
    else:
        # Healthy — clear any stale alert state
        state = _load_alert_state()
        if state.get("last_alert_at"):
            log.info(
                f"WFO-Drift gesund (drift={drift_pct:+.1f}%%, threshold "
                f"-{threshold_pct}%%) — Alert-State bleibt fuer Audit-Trail"
            )

    return result
