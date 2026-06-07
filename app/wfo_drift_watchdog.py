"""v37h+2 (17.05.2026) — Edge-Drift-Watchdog zwischen WFO-Runs.

R-B5 (01.06.2026): PRIMAERE Metrik ist jetzt der PROFIT-FAKTOR (PF), NICHT
mehr Sharpe. Grund (Soak-Investigation #1): der WFO-Sharpe (mean OOS 6.40)
ist ein Artefakt (Return-Glaettung + explosives Compounding + OOS>IS-Anomalie)
-> als absolute Baseline unbrauchbar. PF ist scale-invariant + zuverlaessig.
Sharpe wird weiterhin informativ berechnet. Details: check_wfo_drift /
_get_wfo_target_pf Docstrings.

PROBLEM: WFO laeuft monatlich (1. Sonntag). Zwischen den Runs gibt es
keinen automatischen Sanity-Check ob die Live-Edge-Realitaet zur WFO-
Empfehlung passt. Bei ploetzlichem Edge-Decay (Regime-Shift, Strategy-
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
    "purpose": "Edge-Drift-Watchdog (R-B5: PF-basiert): Daily-Check Live-PF vs WFO-OOS-PF (Sharpe nur informativ), Alert bei Drift > Threshold",
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


# R-B5 (01.06.2026, Soak-Investigation #1): PF-Cap fuer verlustfreie Fenster.
# PF = Brutto-Gewinn/Brutto-Verlust ist mathematisch unendlich wenn keine
# Verluste -> das ist GESUND (kein Decay). Cap auf 10.0 ("aussergewoehnlich
# gut") reicht fuer den Drift-Vergleich und verhindert inf/Division-Probleme.
_LIVE_PF_CAP = 10.0


def _get_wfo_target_pf() -> tuple[Optional[float], Optional[float]]:
    """Liest Mean OOS Profit-Factor + Win-Rate aus letztem WFO-Run.

    R-B5 (01.06.2026, Soak-Investigation #1): PF/Win-Rate als Drift-Baseline
    statt Sharpe. Begruendung: Der Backtest-Sharpe (mean OOS 6.40) ist ein
    ARTEFAKT — (a) backtester glaettet Trade-Returns ueber Haltetage ->
    unterschaetzte Vola, (b) explosives Position-Sizing-Compounding (per-Window
    annual_return bis 5.2 Mrd. %, Sharpe 10.49 BEI 51.7% DD), (c) OOS-Sharpe
    6.40 > IS-Sharpe 4.22 (decay 151.7%) = fuer eine echte Edge statistisch
    unmoeglich. PF ist scale-invariant (Verhaeltnis Brutto-Gewinn/Brutto-
    Verlust) -> von diesen Artefakten NICHT betroffen. WFO-OOS-PF lag
    realistisch bei 1.6-2.97 -> brauchbare Baseline.

    Returns (mean_pf, mean_win_rate) oder (None, None) wenn keine Daten.
    """
    try:
        from app.config_manager import load_json
        wfo = load_json("wfo_status.json") or {}
    except Exception:
        return None, None

    windows = wfo.get("windows", []) if isinstance(wfo, dict) else []
    pfs: list[float] = []
    wrs: list[float] = []
    for w in windows:
        if not isinstance(w, dict):
            continue
        metrics = w.get("oos_metrics")
        if not isinstance(metrics, dict):
            continue
        pf = metrics.get("pf")
        if pf is not None:
            try:
                pfs.append(float(pf))
            except (TypeError, ValueError):
                pass
        wr = metrics.get("win_rate")
        if wr is not None:
            try:
                wrs.append(float(wr))
            except (TypeError, ValueError):
                pass

    mean_pf = sum(pfs) / len(pfs) if pfs else None
    mean_wr = sum(wrs) / len(wrs) if wrs else None
    return mean_pf, mean_wr


def _compute_live_pf(trade_history: list,
                     lookback_days: int) -> tuple[Optional[float], Optional[float], int]:
    """Profit-Factor + Win-Rate aus den Close-Trades der letzten N Tage.

    PF = Brutto-Gewinn / Brutto-Verlust (R-B5 Drift-Baseline-Metrik, scale-
    invariant). Returns (pf, win_rate_pct, n_trades). pf=None wenn < 2 Trades.
    Verlustfreies Fenster -> pf = _LIVE_PF_CAP (gesund, kein Decay).
    """
    if not trade_history:
        return None, None, 0

    cutoff = datetime.now() - timedelta(days=lookback_days)
    pnls: list[float] = []        # gewichtete Netto-Returns aller Closes
    final_pnls: list[float] = []  # nur finale Closes (Win-Rate-Basis, M2)
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
        pnl_pct = t.get("pnl_net_pct", t.get("pnl_pct"))
        if pnl_pct is None:
            continue
        try:
            val = float(pnl_pct)
        except (TypeError, ValueError):
            continue
        # R-B11/M2: Teil-Closes anteilig gewichten — konsistent zur Backtester-
        # Positions-Aggregation (M1). Sonst Live-PF (echte Teil-Closes als volle
        # Trades) vs WFO-PF (Partials gefaltet) = Aepfel/Birnen.
        if "PARTIAL" in action:
            pcp = t.get("partial_close_pct")
            frac = (float(pcp) / 100.0) if pcp else 1.0
            pnls.append(val * frac)
        else:
            pnls.append(val)
            final_pnls.append(val)  # nur finale Closes = unabhaengige Position

    n = len(pnls)
    if n < 2:
        return None, None, n

    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    # Win-Rate auf unabhaengigen Positionen (finale Closes); Partials wuerden die
    # Quote sonst aufblaehen (per Definition Gewinne). Fallback: alle Closes.
    wr_basis = final_pnls if final_pnls else pnls
    wins = sum(1 for p in wr_basis if p > 0)
    win_rate = wins / len(wr_basis) * 100.0

    if gross_loss <= 0:
        pf = _LIVE_PF_CAP if gross_win > 0 else None
    else:
        pf = min(gross_win / gross_loss, _LIVE_PF_CAP)
    return pf, win_rate, n


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
    """Hauptfunktion: prueft Edge-Drift + sendet Pushover-Alert bei Threshold.

    R-B5 (01.06.2026): PRIMAERE Metrik = Profit-Faktor (PF), NICHT Sharpe.
    Der WFO-Sharpe (6.40) ist ein Artefakt (Soak-Investigation #1) -> PF ist
    scale-invariant + zuverlaessig. Sharpe bleibt informativ. Sharpe-Gating
    nur als Fallback wenn WFO keine PF-Daten hat.

    Returns Status-Dict mit:
        drift_metric (str)                "pf" (primaer) oder "sharpe" (Fallback)
        drift_pct (float | None)          (live-wfo)/wfo*100 der Primaer-Metrik
        wfo_pf / live_pf (float | None)   Profit-Faktor Baseline vs Live
        pf_drift_pct (float | None)       PF-Drift (= drift_pct wenn metric=pf)
        wfo_win_rate / live_win_rate      Win-Rate (informativ)
        wfo_sharpe / live_sharpe          Sharpe (informativ, NICHT mehr gating)
        n_trades (int)                    Sample-Size
        distinct_days (int)               R-A53 Clean-Sample-Guard
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

    # R-B5 (01.06.2026, Soak-Investigation #1): PF ist die PRIMAERE Drift-Metrik.
    # Der WFO-Sharpe (mean OOS 6.40) ist ein Artefakt (Return-Glaettung +
    # explosives Compounding + OOS>IS-Anomalie) -> als absolute Baseline
    # unbrauchbar. PF (scale-invariant) ist zuverlaessig. Sharpe wird weiterhin
    # INFORMATIV berechnet/reportet. Fallback auf Sharpe-Gating NUR falls die
    # WFO-Daten keinen PF enthalten (alte wfo_status vor R-B4 / Error-Windows).
    wfo_pf, wfo_win_rate = _get_wfo_target_pf()
    wfo_sharpe = _get_wfo_target_sharpe()
    if wfo_pf is None and wfo_sharpe is None:
        result["skip_reason"] = "Keine WFO-Daten in wfo_status.json"
        return result
    if wfo_pf is not None:
        result["wfo_pf"] = round(wfo_pf, 3)
    if wfo_win_rate is not None:
        result["wfo_win_rate"] = round(wfo_win_rate, 1)
    if wfo_sharpe is not None:
        result["wfo_sharpe"] = round(wfo_sharpe, 3)

    try:
        from app.config_manager import load_json
        trade_history = load_json("trade_history.json") or []
    except Exception:
        result["skip_reason"] = "trade_history.json nicht ladbar"
        return result

    # Live-Metriken: PF + Win-Rate (primaer) + Sharpe (informativ/Fallback)
    live_pf, live_win_rate, n = _compute_live_pf(trade_history, lookback_days)
    live_sharpe, _n_sharpe = _compute_live_sharpe(trade_history, lookback_days)
    result["n_trades"] = n

    # Primaer-Metrik waehlen: PF wenn WFO-PF + Live-PF vorhanden, sonst Sharpe.
    use_pf = wfo_pf is not None and live_pf is not None
    drift_metric = "pf" if use_pf else "sharpe"
    result["drift_metric"] = drift_metric
    live_val = live_pf if use_pf else live_sharpe
    wfo_val = wfo_pf if use_pf else wfo_sharpe

    if live_val is None or n < min_trades:
        result["skip_reason"] = f"Zu wenig Trades (n={n}, min={min_trades})"
        return result

    # R-A53 (29.05.2026): Min-Clean-Sample-Guard. Genug Trades reicht nicht —
    # die Metrik braucht genug DISTINKTE Trading-Tage. HALT-gestoerte Fenster
    # (viele Trades, wenige Tage) sind unzuverlaessig. Ohne diesen Guard wuerde
    # Drift taeglich feuern waehrend Soak.
    distinct_days = _count_distinct_trading_days(trade_history, lookback_days)
    result["distinct_days"] = distinct_days
    if distinct_days < min_distinct_days:
        if live_pf is not None:
            result["live_pf"] = round(live_pf, 3)
        if live_win_rate is not None:
            result["live_win_rate"] = round(live_win_rate, 1)
        if live_sharpe is not None:
            result["live_sharpe"] = round(live_sharpe, 3)
        result["skip_reason"] = (
            f"Zu wenig distinkte Trading-Tage (days={distinct_days}, "
            f"min={min_distinct_days}) — Sample statistisch unzuverlaessig "
            f"(HALT-gestoert?), kein Drift-Alert"
        )
        log.info(
            "WFO-Drift R-A53: skip Alert — nur %d distinkte Tage (<%d), "
            "metric=%s live=%.3f nicht aussagekraeftig",
            distinct_days, min_distinct_days, drift_metric, live_val,
        )
        return result

    if live_pf is not None:
        result["live_pf"] = round(live_pf, 3)
    if live_win_rate is not None:
        result["live_win_rate"] = round(live_win_rate, 1)
    if live_sharpe is not None:
        result["live_sharpe"] = round(live_sharpe, 3)

    # Drift in % — negativ = Decay
    if wfo_val == 0:
        result["skip_reason"] = f"WFO-{drift_metric} = 0, kann nicht dividieren"
        return result
    drift_pct = (live_val - wfo_val) / abs(wfo_val) * 100
    result["drift_pct"] = round(drift_pct, 2)
    if use_pf:
        result["pf_drift_pct"] = round(drift_pct, 2)

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
                _label = "Profit-Faktor" if drift_metric == "pf" else "Sharpe"
                _wr_hint = (f" Win-Rate live={live_win_rate:.0f}%"
                            if live_win_rate is not None else "")
                msg = (
                    f"WFO-DRIFT-ALARM ({_label}): Live {live_val:.2f} vs "
                    f"WFO-Baseline {wfo_val:.2f} ({drift_pct:+.1f}% Decay).{_wr_hint} "
                    f"N={n} Trades letzte {lookback_days}d. "
                    f"Aktion: pruefe ob Regime-Shift -> manueller WFO-Re-Run sinnvoll."
                )
                send_alert(msg, level="WARNING")
                result["alert_triggered"] = True
                state["last_alert_at"] = datetime.now().isoformat()
                state["last_drift_pct"] = round(drift_pct, 2)
                state["last_drift_metric"] = drift_metric
                state["last_live_val"] = round(live_val, 3)
                state["last_wfo_val"] = round(wfo_val, 3)
                _save_alert_state(state)
                log.warning(
                    f"WFO-Drift-Alert gesendet (metric={drift_metric}): "
                    f"live={live_val:.2f}, wfo={wfo_val:.2f}, drift={drift_pct:+.1f}%"
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
