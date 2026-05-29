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


# v37h+3 (Sprint-Tag-9, 19.05.2026): Audit-Coverage-Marker
AUDIT_METADATA = {
    "purpose": "Sharpe-Drift-Watchdog: Daily-Check Live-Sharpe vs WFO-OOS-Sharpe, Alert bei >30% Drift",
    "config_section": None,
    "state_files": ["wfo_status.json"],
    "self_tests": [],
    "scheduler_hooks": ["_BG_WFO_DRIFT_S"],
    "health_check": None,
    "added_in": "v37h+2 (17.05.2026)",
}

# Konfigurations-Defaults (override via config.json.wfo_drift_watchdog)
DEFAULT_LOOKBACK_DAYS = 30           # Live-Sharpe ueber letzte 30 Trade-Tage
DEFAULT_DRIFT_THRESHOLD_PCT = 30.0   # > 30% Sharpe-Decay -> Alert
DEFAULT_MIN_TRADES = 10              # weniger Trades = nicht-aussagekraeftig
DEFAULT_ALERT_THROTTLE_HOURS = 24    # max 1 Alert pro Tag
# R-A53 (29.05.2026 Sprint-Tag-18): Min-Clean-Sample-Guard. Eine daily-
# annualisierte Sharpe braucht GENUEGEND DISTINKTE Trading-Tage um aussage-
# kraeftig zu sein — NICHT nur genug Trades. Beispiel 29.05.: 116 Trades
# (> min_trades=10), aber durch Regime-HALT auf wenige Tage konzentriert +
# HALT-gestoertes Fenster → daily-Sharpe statistisch unzuverlaessig. Ohne
# diesen Guard wuerde der Watchdog waehrend der ganzen Soak-Phase taeglich
# -175% Drift melden (Cry-Wolf). 10 distinkte Tage = grobe Mindestbasis fuer
# eine daily-Sharpe-Schaetzung.
DEFAULT_MIN_DISTINCT_DAYS = 10


def _get_wfo_target_sharpe() -> Optional[float]:
    """Liest Mean OOS-Sharpe aus letztem WFO-Run.

    R-A16 (19.05.2026): Schema-Fix. Vorher: sucht w['oos_sharpe'] direkt
    -> liefert immer None weil Schema hat sharpe nested in oos_metrics.
    Bug-Wurzel: Sprint-Tag-7 (17.05.) WFO-Drift-Build basierte auf
    angenommenes Schema, nie gegen echte wfo_status.json verifiziert.
    Entdeckt 19.05. nach manuellem WFO-Trigger durch Carlos.

    Schema-Lookup-Reihenfolge (defensive Fallback-Chain):
      1. window['oos_metrics']['sharpe']  (current schema)
      2. window['oos_score']                (legacy alias)
      3. window['oos_sharpe']               (legacy direct, falls je existierend)
    """
    try:
        from app.config_manager import load_json
        wfo = load_json("wfo_status.json") or {}
    except Exception:
        return None

    windows = wfo.get("windows", []) if isinstance(wfo, dict) else []
    if not windows:
        return None

    oos_sharpes = []
    for w in windows:
        if not isinstance(w, dict):
            continue
        sharpe = None
        # Primary: nested in oos_metrics
        metrics = w.get("oos_metrics")
        if isinstance(metrics, dict):
            sharpe = metrics.get("sharpe")
        # Fallback 1: oos_score (legacy alias)
        if sharpe is None:
            sharpe = w.get("oos_score")
        # Fallback 2: oos_sharpe (legacy direct)
        if sharpe is None:
            sharpe = w.get("oos_sharpe")
        if sharpe is not None:
            try:
                oos_sharpes.append(float(sharpe))
            except (TypeError, ValueError):
                continue

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
    # R-A52: (timestamp, pnl_pct)-Tupel sammeln fuer Daily-Grouping (statt
    # nur pnl-Werte). Timestamp wird fuer die Kalender-Tag-Aggregation gebraucht.
    recent_trade_days = []

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
            recent_trade_days.append((ts, float(pnl_pct)))
        except (TypeError, ValueError):
            continue

    n = len(recent_trade_days)
    if n < 2:
        return None, n

    # R-A52 (29.05.2026 Sprint-Tag-18, Soak-Item WFO-Baseline-Methodik-Review):
    # BUG vor R-A52: Live-Sharpe war rohes per-Trade mean/std OHNE Annualisierung.
    # Verglichen wurde aber gegen den WFO-OOS-Sharpe, der DAILY-annualisiert ist
    # (backtester.py: (daily_mean/daily_std) * sqrt(252)). Aepfel-mit-Birnen:
    # ~16x systematischer Skalen-Offset → Drift-Watchdog feuerte DAUER-False-
    # Positives (z.B. 29.05.: live=-0.31 vs wfo=6.40 = -105.8% "Drift", reines
    # Mess-Artefakt). Der alte Kommentar ("Annualisiert nicht — gleiche
    # Convention") war schlicht falsch: WFO IST annualisiert.
    #
    # Fix: Live-Sharpe auf DIESELBE Daily-annualized Convention bringen.
    # Close-Trades nach Kalender-Tag gruppieren (Summe pnl_pct pro Tag =
    # Tages-Rendite-Approximation, analog backtester daily_contrib), dann
    # (daily_mean / daily_std) * sqrt(252).
    #
    # Bewusste Approximation: backtester verteilt Returns ueber Holding-Days;
    # hier lumpen wir auf den Close-Tag (Live-trade_history hat keine sauberen
    # Entry/Exit-Holding-Spans). Beide sind aber daily * sqrt(252) → gleiche
    # Groessenordnung + gleicher Annualisierungs-Faktor → Vergleich valide.
    daily_returns: dict[str, float] = {}
    for ts, pnl in recent_trade_days:
        day_key = ts.strftime("%Y-%m-%d")
        daily_returns[day_key] = daily_returns.get(day_key, 0.0) + pnl

    daily_vals = list(daily_returns.values())
    if len(daily_vals) < 2:
        return None, n

    daily_mean = sum(daily_vals) / len(daily_vals)
    daily_var = sum((r - daily_mean) ** 2 for r in daily_vals) / (len(daily_vals) - 1)
    if daily_var <= 0:
        return None, n
    daily_std = daily_var ** 0.5
    annualized_sharpe = (daily_mean / daily_std) * (252 ** 0.5)
    return annualized_sharpe, n


def _count_distinct_trading_days(trade_history: list, lookback_days: int) -> int:
    """R-A53 (29.05.2026): Anzahl DISTINKTER Kalender-Tage mit Close-Trades
    im Lookback-Fenster.

    Guard-Metrik gegen unzuverlaessige daily-Sharpe: viele Trades auf wenigen
    Tagen (z.B. Regime-HALT-gestoertes Fenster) ist statistisch nicht
    aussagekraeftig. Pure-Function, testbar.
    """
    if not trade_history:
        return 0
    cutoff = datetime.now() - timedelta(days=lookback_days)
    days: set[str] = set()
    for t in trade_history:
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
        days.add(ts.strftime("%Y-%m-%d"))
    return len(days)


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
    min_distinct_days = int(cfg.get("min_distinct_days", DEFAULT_MIN_DISTINCT_DAYS))  # R-A53
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

    # R-A53 (29.05.2026): Min-Clean-Sample-Guard. Genug Trades reicht nicht —
    # die daily-annualisierte Sharpe braucht genug DISTINKTE Trading-Tage.
    # HALT-gestoerte Fenster (viele Trades, wenige Tage) sind unzuverlaessig.
    # Ohne diesen Guard wuerde -175% Drift taeglich feuern waehrend Soak.
    distinct_days = _count_distinct_trading_days(trade_history, lookback_days)
    result["distinct_days"] = distinct_days
    if distinct_days < min_distinct_days:
        result["live_sharpe"] = round(live_sharpe, 3)
        result["skip_reason"] = (
            f"Zu wenig distinkte Trading-Tage (days={distinct_days}, "
            f"min={min_distinct_days}) — Sample statistisch unzuverlaessig "
            f"(HALT-gestoert?), kein Drift-Alert"
        )
        log.info(
            "WFO-Drift R-A53: skip Alert — nur %d distinkte Tage (<%d), "
            "live_sharpe=%.2f nicht aussagekraeftig",
            distinct_days, min_distinct_days, live_sharpe,
        )
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
