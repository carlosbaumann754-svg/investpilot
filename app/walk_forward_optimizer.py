"""
app/walk_forward_optimizer.py — Walk-Forward-Optimization (E1, vorgezogen aus Q1)

Was:
   Statt einmal auf 4 Jahren historischer Daten zu optimieren (klassisch
   ueberfittet), splitten wir die Historie in viele Rolling-Windows. Pro
   Window: optimiere auf Train-Daten, evaluiere ehrlich auf Test-Daten.
   Aggregiere alle OOS-Scores -> echte Sharpe-Schaetzung.

Warum:
   Der bisherige Optimizer (app/optimizer.py) macht ein 80/20 Train/Test-Split
   EINMAL ueber die ganze Historie. Das fuehrt zu zwei Problemen:
     1. "Lucky Window" Bias - das eine 80%-Train-Window kann zufaellig gut
        sein. Mit 4 Rolling-Windows + 4 OOS-Tests ist der Mittelwert robust.
     2. Regime-Bias - Bull-2023 in Train kann Bear-2024 in Test perfekt
        slappen lassen. Mit Rolling-Windows sehen wir beides ehrlich.

   Real-Money-Cutover ist 28.05.2026. WFO sagt uns *vorher*: ist Sharpe 3.5
   nur Lucky-Backtest oder echte Edge?

Roadmap-Plan:
   Di-Mi (28.-30.04.): Phase 1 - Framework (DIESE DATEI) + Rolling-Windows
   Do-Fr (01.-02.05.): Phase 2 - Pro-Window Backtest-Runs + Aggregations-Stats
   Sa  (03.05.):       Erster vollstaendiger Run gegen historische Daten
   So  (04.05.):       Resultat-Analyse
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

log = logging.getLogger(__name__)


# ============================================================
# OPTION A — DREI HYPOTHESEN (Carlos' Entscheidung 2026-04-28)
# ============================================================
# Schmal-und-ehrlich Grid (24 Kombinationen statt 3'000 im Optimizer).
# Jede Parameter-Achse testet eine konkrete Hypothese:
#
#   stop_loss_pct   "Welche SL-Tiefe haelt die Wahrheit zwischen Whipsaw
#                    und zu spaeten Exits?" -> 3 SL-Werte
#   take_profit_pct "Wo schlagen Insider-Mover am haeufigsten zu? Knapp
#                    oder ausgereizt?"     -> 4 TP-Werte
#   min_scanner_score "Wieviel Scanner-Edge braucht ein Trade minimum?"
#                                          -> 2 Score-Schwellen
#
# Total: 3 * 4 * 2 = 24 Kombinationen pro Window
#        x 6 Windows = 144 Backtests Runtime ~10-15 Min
#
# Why nicht alle Optimizer-Parameter (Option B, 3000 Kombos):
#   Bonferroni-Korrektur — bei 3000 Tests ist die effektive Sharpe-Schwelle
#   um Faktor sqrt(N) hoeher. Sharpe 3.5 in B == Sharpe ~1.8-2.2 ehrliche
#   Schaetzung. In A == Sharpe ~3.0-3.2. Wir wollen die ehrliche Zahl.
#
# Aenderungen NUR via git commit (mit Begruendung) — analog Strategy A
# fuer disabled_symbols. Quant-Disziplin = Edge.
WFO_PARAM_GRID: dict[str, list] = {
    "stop_loss_pct":     [-3.0, -4.0, -5.0],
    "take_profit_pct":   [9, 12, 15, 18],
    "min_scanner_score": [40, 50],
}


def total_param_combinations() -> int:
    """Helper: Anzahl Grid-Kombinationen (fuer Logging + UI)."""
    n = 1
    for vals in WFO_PARAM_GRID.values():
        n *= len(vals)
    return n


# ============================================================
# WINDOW-KONFIGURATION
# ============================================================

@dataclass
class WFOConfig:
    """Konfiguration eines WFO-Laufs.

    Standardwerte folgen Roadmap-Plan: 24 Monate Train, 6 Monate Test, 6 Monate
    Step. Bei 4 Jahren Historie ergibt das ~3 Windows. Bei 5 Jahren ~5 Windows.

    Trade-offs der Standardwerte (Why):
    - 24Mo Train: lang genug fuer min. 1 voller Marktzyklus (Bull/Bear-Wechsel)
      und genug Sample-Size fuer 1300+ Trades. Kuerzer waere zu volatil.
    - 6Mo Test: lang genug um Regime-Wechsel im OOS zu sehen, kurz genug
      um schnell zum naechsten Window zu kommen.
    - 6Mo Step: Non-Overlapping Test-Windows (jeder Test-Bereich wird genau
      einmal geprueft). Verhindert Doppel-Zaehlen.
    """
    train_months: int = 24
    test_months: int = 6
    step_months: int = 6
    min_train_trades: int = 200  # weniger -> Window verworfen (zu wenig Sample)


@dataclass
class Window:
    """Ein einzelnes Rolling-Window (Train + Test Zeitraum)."""
    idx: int                       # 0-basierte Window-Nummer
    train_start: pd.Timestamp
    train_end: pd.Timestamp        # exklusiv
    test_start: pd.Timestamp       # = train_end
    test_end: pd.Timestamp         # exklusiv
    # Resultate werden in Phase 2 befuellt
    best_params: Optional[dict] = None
    is_score: Optional[float] = None     # In-Sample (auf Train) Score
    oos_score: Optional[float] = None    # Out-of-Sample (auf Test) Score
    oos_trades: int = 0
    oos_metrics: dict = field(default_factory=dict)


# ============================================================
# PHASE 1: ROLLING-WINDOWS GENERIEREN
# ============================================================

def build_windows(
    history_start: pd.Timestamp,
    history_end: pd.Timestamp,
    cfg: WFOConfig,
) -> list[Window]:
    """Generiert alle Rolling-Windows zwischen history_start und history_end.

    Logik: starte mit Train-Window beginnend bei history_start. Schiebe um
    step_months vor. Hoere auf wenn Test-Window-Ende > history_end.

    Args:
        history_start: erstes verfuegbares Datum in den Histories
        history_end:   letztes verfuegbares Datum
        cfg:           Window-Groessen-Konfiguration

    Returns:
        Liste von Windows in chronologischer Reihenfolge.
    """
    windows: list[Window] = []
    cursor = history_start
    idx = 0
    train_delta = pd.DateOffset(months=cfg.train_months)
    test_delta = pd.DateOffset(months=cfg.test_months)
    step_delta = pd.DateOffset(months=cfg.step_months)

    while True:
        train_start = cursor
        train_end = train_start + train_delta
        test_start = train_end
        test_end = test_start + test_delta
        if test_end > history_end:
            break
        windows.append(Window(
            idx=idx,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
        ))
        cursor = cursor + step_delta
        idx += 1

    return windows


def slice_histories(
    histories: dict[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, pd.DataFrame]:
    """Schneidet alle Symbol-Histories auf den angegebenen Zeitraum.

    Liefert nur Symbole zurueck die im Zeitraum >= 30 Tage Daten haben
    (verhindert Edge-Cases fuer frisch-gelistete Aktien).
    """
    sliced = {}
    for sym, df in histories.items():
        if df is None or df.empty:
            continue
        # DataFrame-Index ist DatetimeIndex
        try:
            mask = (df.index >= start) & (df.index < end)
            piece = df.loc[mask]
            if len(piece) >= 30:
                sliced[sym] = piece
        except Exception as e:
            log.debug("slice_histories: skip %s (%s)", sym, e)
    return sliced


# ============================================================
# PHASE 2: PRO-WINDOW BACKTEST + AGGREGATION
# ============================================================

import copy
import itertools


def _apply_params_to_config(base_config: dict, params: dict) -> dict:
    """Erstellt eine Config-Variante mit den Grid-Params eingesetzt.

    Wir kopieren die Base-Config tief und ueberschreiben nur die Schluessel
    aus dem Param-Dict. Demo-Trading-Section ist der Hauptort fuer
    stop_loss/take_profit/min_scanner_score.
    """
    cfg = copy.deepcopy(base_config)
    dt = cfg.setdefault("demo_trading", {})
    for key, value in params.items():
        dt[key] = value
    return cfg


def _score_metrics(trades: list, position_sizing: Optional[dict] = None) -> dict:
    """Berechnet Sharpe + Sekundaer-Metriken aus einer Trade-Liste.

    Wrapper um app.backtester.calculate_metrics, gibt None zurueck bei
    leerer Trade-Liste (sonst kollidiert das mit downstream-Vergleichen).
    """
    from app.backtester import calculate_metrics
    if not trades:
        return {"sharpe": None, "trades": 0, "max_dd": None, "pf": None,
                "win_rate": None, "annual_return": None}
    m = calculate_metrics(trades, position_sizing=position_sizing)
    # v37b: calculate_metrics liefert die Felder _pct-suffixed
    # (max_drawdown_pct, win_rate_pct, annual_return_pct). Vorher haben
    # wir die nicht-suffixed Keys gelesen -> immer None.
    return {
        "sharpe": m.get("sharpe_ratio"),
        "trades": m.get("total_trades", 0),
        "max_dd": m.get("max_drawdown_pct"),
        "pf": m.get("profit_factor"),
        "win_rate": m.get("win_rate_pct"),
        "annual_return": m.get("annual_return_pct"),
    }


def _grid_combinations() -> list[dict]:
    """Erweitert WFO_PARAM_GRID zu einer flachen Liste aller Param-Kombinationen."""
    keys = list(WFO_PARAM_GRID.keys())
    value_lists = [WFO_PARAM_GRID[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*value_lists)]


def _run_single_backtest(precomputed: dict, config: dict) -> list:
    """Fuehrt einen einzelnen Backtest mit gegebener Config + Precompute durch."""
    from app.backtester import simulate_trades_fast
    return simulate_trades_fast(precomputed, config=config, use_realistic_filters=True)


def run_window_backtest(
    window: Window,
    histories: dict[str, pd.DataFrame],
    base_config: dict,
    vix_history: Optional[pd.DataFrame] = None,
) -> Window:
    """Optimiert Param-Grid auf Train, evaluiert beste Params auf Test.

    Workflow:
      1. Train-Slice -> precompute -> alle 24 Param-Kombos durchspielen
      2. Beste Kombination per Sharpe waehlen
      3. Test-Slice -> precompute -> Best-Params anwenden -> OOS-Metrics

    Schreibt Resultate direkt in das Window-Objekt zurueck.
    """
    from app.backtester import precompute_grid_data

    log.info("WFO Window %d: train %s..%s -> test %s..%s",
             window.idx,
             window.train_start.date(), window.train_end.date(),
             window.test_start.date(), window.test_end.date())

    # 1. Train-Slice
    train_hist = slice_histories(histories, window.train_start, window.train_end)
    if len(train_hist) < 5:
        log.warning("Window %d: nur %d Symbole im Train -> SKIP",
                    window.idx, len(train_hist))
        window.oos_metrics = {"error": f"only {len(train_hist)} symbols in train"}
        return window
    train_pre = precompute_grid_data(train_hist, vix_history)

    combos = _grid_combinations()
    log.info("Window %d: teste %d Param-Kombos auf %d Train-Symbole",
             window.idx, len(combos), len(train_hist))

    # 2. Grid-Search auf Train
    best = {"sharpe": -999.0, "params": None, "metrics": None}
    for params in combos:
        cfg = _apply_params_to_config(base_config, params)
        trades = _run_single_backtest(train_pre, cfg)
        m = _score_metrics(trades)
        sharpe = m.get("sharpe")
        # Mindest-Sample fuer aussagekraeftige Sharpe (sonst Lucky-Outliers)
        if sharpe is not None and m["trades"] >= 30 and sharpe > best["sharpe"]:
            best = {"sharpe": sharpe, "params": params, "metrics": m}

    if best["params"] is None:
        log.warning("Window %d: KEINE Param-Kombination mit >= 30 Train-Trades",
                    window.idx)
        window.oos_metrics = {"error": "no train combo with sufficient trades"}
        return window

    log.info("Window %d Best-IS: Sharpe %.2f bei %s",
             window.idx, best["sharpe"], best["params"])
    window.best_params = best["params"]
    window.is_score = best["sharpe"]

    # 3. OOS-Eval auf Test
    test_hist = slice_histories(histories, window.test_start, window.test_end)
    if len(test_hist) < 5:
        log.warning("Window %d: nur %d Symbole im Test -> SKIP OOS",
                    window.idx, len(test_hist))
        window.oos_metrics = {"error": f"only {len(test_hist)} symbols in test"}
        return window
    test_pre = precompute_grid_data(test_hist, vix_history)
    cfg_best = _apply_params_to_config(base_config, best["params"])
    test_trades = _run_single_backtest(test_pre, cfg_best)
    test_m = _score_metrics(test_trades)
    window.oos_score = test_m.get("sharpe")
    window.oos_trades = test_m.get("trades", 0)
    window.oos_metrics = test_m
    log.info("Window %d OOS: Sharpe %.2f, %d Trades, MaxDD %.2f%%",
             window.idx, window.oos_score or 0, window.oos_trades,
             test_m.get("max_dd") or 0)
    return window


def aggregate_oos_results(windows: list[Window]) -> dict:
    """Aggregiert die OOS-Resultate ueber alle Windows.

    Liefert die zentralen Kennzahlen die das Dashboard anzeigt:
      - mean_oos_sharpe: Mittelwert ueber alle Windows -> die ehrliche Sharpe-Schaetzung
      - mean_is_sharpe:  Mittelwert der In-Sample-Scores
      - sharpe_decay:    OOS / IS in Prozent (Retention) -> < 70% = Overfitting-Verdacht
      - oos_stability:   Standardabweichung der OOS-Sharpe -> niedrig = robust
      - mean_oos_trades, mean_oos_max_dd
    """
    valid = [w for w in windows if w.oos_score is not None]
    if not valid:
        return {"error": "no valid windows"}

    is_scores = [w.is_score for w in valid if w.is_score is not None]
    oos_scores = [w.oos_score for w in valid]
    oos_trades = [w.oos_trades for w in valid]
    oos_dd = [w.oos_metrics.get("max_dd") for w in valid
              if w.oos_metrics and w.oos_metrics.get("max_dd") is not None]

    def _mean(xs): return sum(xs) / len(xs) if xs else None
    def _std(xs):
        if len(xs) < 2:
            return 0.0
        mu = _mean(xs)
        return (sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5

    mean_is = _mean(is_scores)
    mean_oos = _mean(oos_scores)
    decay = (mean_oos / mean_is * 100) if (mean_is and mean_oos and mean_is > 0) else None

    return {
        "windows_total": len(windows),
        "windows_valid": len(valid),
        "mean_is_sharpe": round(mean_is, 3) if mean_is is not None else None,
        "mean_oos_sharpe": round(mean_oos, 3) if mean_oos is not None else None,
        "sharpe_decay_pct": round(decay, 1) if decay is not None else None,
        "oos_stability_std": round(_std(oos_scores), 3),
        "mean_oos_trades": round(_mean(oos_trades), 0) if oos_trades else 0,
        "mean_oos_max_dd": round(_mean(oos_dd), 2) if oos_dd else None,
    }


# ============================================================
# ORCHESTRATOR
# ============================================================

def run_walk_forward(
    histories: Optional[dict[str, pd.DataFrame]] = None,
    base_config: Optional[dict] = None,
    cfg: Optional[WFOConfig] = None,
    years: int = 5,
) -> dict:
    """Vollstaendiger WFO-Lauf: laedt Daten, generiert Windows, evaluiert,
    aggregiert, persistiert Status.

    Wird aus dem CLI (__main__) UND aus dem API-Trigger /api/wfo/run aufgerufen.
    Erwartet keine Live-Connection (TRADING-NULL-RISK).
    """
    from app.backtester import download_history, download_vix_history
    from app.config_manager import load_config

    if base_config is None:
        base_config = load_config()
    if cfg is None:
        cfg = WFOConfig()

    write_status("running", phase="loading_history",
                 message="Lade historische Kurse...")

    # Histories laden
    if histories is None:
        log.info("WFO: Lade %d Jahre Histories...", years)
        histories = download_history(years=years) or {}
    if not histories:
        write_status("error", error="Keine Histories geladen")
        return read_status()
    vix_history = download_vix_history(years=years)

    # Window-Range bestimmen
    all_starts = [df.index.min() for df in histories.values()
                  if df is not None and not df.empty]
    all_ends = [df.index.max() for df in histories.values()
                if df is not None and not df.empty]
    if not all_starts or not all_ends:
        write_status("error", error="Histories leer")
        return read_status()
    history_start = max(all_starts)  # spaetester Start (alle Symbole haben Daten)
    history_end = min(all_ends)
    log.info("WFO: Effektive Range %s..%s ueber %d Symbole",
             history_start.date(), history_end.date(), len(histories))

    windows = build_windows(history_start, history_end, cfg)
    if not windows:
        write_status("error", error=f"Zu wenig Historie fuer Windows (range {history_start.date()}..{history_end.date()})")
        return read_status()

    # Pro Window optimieren + evaluieren
    write_status("running", phase="backtesting",
                 windows_total=len(windows),
                 current_window=0)
    for w in windows:
        run_window_backtest(w, histories, base_config, vix_history)
        write_status("running", phase="backtesting",
                     windows_total=len(windows),
                     current_window=w.idx + 1,
                     last_completed_window=w.idx)

    # Aggregation
    aggregate = aggregate_oos_results(windows)
    log.info("WFO Aggregate: %s", aggregate)

    # Persist
    windows_payload = [{
        "idx": w.idx,
        "train_start": w.train_start.date().isoformat(),
        "train_end": w.train_end.date().isoformat(),
        "test_start": w.test_start.date().isoformat(),
        "test_end": w.test_end.date().isoformat(),
        "best_params": w.best_params,
        "is_score": w.is_score,
        "oos_score": w.oos_score,
        "oos_trades": w.oos_trades,
        "oos_metrics": w.oos_metrics,
    } for w in windows]
    write_status("done",
                 windows=windows_payload,
                 aggregate=aggregate,
                 phase="completed")
    return read_status()


# ============================================================
# STATUS-FILE FUER DASHBOARD (data/wfo_status.json)
# ============================================================

WFO_STATUS_FILENAME = "wfo_status.json"


def read_status() -> dict:
    """Liest den letzten WFO-Status fuer das Dashboard.

    Returns:
        dict mit 'state' ('idle'/'running'/'done'/'error'), 'last_run',
        'windows', 'aggregate', 'config'. Default: idle wenn nie gelaufen.
    """
    try:
        from app.config_manager import load_json
        data = load_json(WFO_STATUS_FILENAME)
        if data:
            return data
    except Exception as e:
        log.debug("WFO read_status: %s", e)
    return {
        "state": "idle",
        "last_run": None,
        "next_run_planned": "2026-05-03 (Sa, erster vollstaendiger Lauf)",
        "windows": [],
        "aggregate": None,
        "config": {
            "param_grid": WFO_PARAM_GRID,
            "param_combinations": total_param_combinations(),
            "windows_planned": 6,  # bei 5J Historie + 24/6/6 Mo Default-Cfg
            "approach": "Option A (Elite-Pfad, schmal-und-ehrlich, 3 Hypothesen)",
        },
    }


def write_status(state: str, **payload) -> None:
    """Schreibt aktuellen WFO-Status fuer Dashboard-Polling.

    Wird in Phase 2 vom run_window_backtest und aggregate_oos_results
    aufgerufen (state-Transitionen: idle -> running -> done/error).
    """
    try:
        from app.config_manager import save_json
        cur = read_status() or {}
        cur["state"] = state
        cur["last_run"] = datetime.now().isoformat()
        cur.update(payload)
        save_json(WFO_STATUS_FILENAME, cur)
    except Exception as e:
        log.warning("WFO write_status fehlgeschlagen: %s", e)


# ============================================================
# CLI fuer Phase 1 — Smoke-Test des Window-Builders
# ============================================================

if __name__ == "__main__":
    """CLI-Modi:
       python -m app.walk_forward_optimizer            # Smoke (Window-Builder)
       python -m app.walk_forward_optimizer --run      # Vollstaendiger WFO-Lauf
       python -m app.walk_forward_optimizer --run -y 3 # Kurzer Run mit 3J Historie
    """
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if "--run" in sys.argv:
        years = 5
        if "-y" in sys.argv:
            i = sys.argv.index("-y")
            years = int(sys.argv[i + 1])
        print(f"=== WFO RUN starting (years={years}) ===")
        result = run_walk_forward(years=years)
        print("=== WFO RUN done ===")
        agg = result.get("aggregate") or {}
        print(f"Mean IS Sharpe:  {agg.get('mean_is_sharpe')}")
        print(f"Mean OOS Sharpe: {agg.get('mean_oos_sharpe')}")
        print(f"Sharpe Decay:    {agg.get('sharpe_decay_pct')}%")
        print(f"OOS Stability:   {agg.get('oos_stability_std')}")
        print(f"Mean OOS Trades: {agg.get('mean_oos_trades')}")
    else:
        # Smoke-Test: 5 Jahre Historie -> erwartete Window-Anzahl
        cfg = WFOConfig()
        history_start = pd.Timestamp("2021-01-01")
        history_end = pd.Timestamp("2026-04-27")
        windows = build_windows(history_start, history_end, cfg)
        print(f"WFOConfig: train={cfg.train_months}Mo / test={cfg.test_months}Mo / step={cfg.step_months}Mo")
        print(f"History: {history_start.date()} -> {history_end.date()} ({(history_end-history_start).days} Tage)")
        print(f"Windows: {len(windows)}")
        for w in windows:
            print(f"  W{w.idx}: train {w.train_start.date()}..{w.train_end.date()} "
                  f"-> test {w.test_start.date()}..{w.test_end.date()}")
        print(f"Param-Combos: {total_param_combinations()}")
        print(f"WFO_PARAM_GRID: {WFO_PARAM_GRID}")
